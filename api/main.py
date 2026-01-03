from contextlib import asynccontextmanager
from fastapi import FastAPI
import ast
from dotenv import load_dotenv
import json
import os
import logging
import pandas as pd
from datetime import date
from gspread.utils import ValueRenderOption
from secure_eval import avaliar_expressao_segura
from database.supabase_client import supabase
from utils import (
    conectar_google_sheets,
    normalizar_string,
    )
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters)
from openai import OpenAI

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

application = (
      ApplicationBuilder()
      .updater(None)
      .token(TELEGRAM_TOKEN)
      .build()
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await application.bot.setWebhook(WEBHOOK_URL)
    async with application:
        await application.start()
        yield
        await application.stop()

app = FastAPI(lifespan=lifespan)


CHAT_ID_LIST = ast.literal_eval(os.getenv("CHAT_ID_LIST", "[]"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


client = OpenAI(api_key=OPENAI_API_KEY)


# Filtro customizado para validação de chat_id
class AuthorizedOnlyFilter(filters.MessageFilter):
    def filter(
            self, update: Update) -> bool:
        if not update or not update.from_user:
            return False
        chat_id = update.from_user.id
        return chat_id in CHAT_ID_LIST


# Instanciar uma única vez
authorized_only = AuthorizedOnlyFilter()


def abrir_planilha():
    """
    Abre a planilha de finanças pessoais no Google Sheets.
    Retorna o objeto da planilha.
    """
    cliente = conectar_google_sheets()
    try:
        planilha = cliente.open("Minhas Finanças Pessoais")
        sheet = planilha.worksheet("Transações")
        return sheet
    except Exception as e:
        logger.exception(f"Erro ao abrir a planilha: {e}")
        raise RuntimeError("Não foi possível abrir a planilha de finanças.")


def gerar_resumo_financeiro(df: pd.DataFrame) -> dict:
    """
    Espera colunas: ['Data','Descrição','Categoria','Tipo','Valor', ...]
    Tipo é 'Receita' ou 'Despesa' (case-insensitive)
    """
    df = df.copy()
    # garantir colunas mínimas
    required = ['Data', 'Descrição', 'Categoria', 'Tipo', 'Valor']
    # tentar mapear colunas sem acento
    # assume que o usuário usou cabeçalhos corretos
    for col in required:
        if col not in df.columns:
            raise ValueError(
                f"A coluna obrigatória '{col}' não foi encontrada na planilha."
                )
    # converter tipos
    df['Valor'] = pd.to_numeric(df['Valor'], errors='coerce').fillna(0.0)
    df['Tipo'] = df['Tipo'].astype(str).str.strip().str.capitalize()
    # garantir datas
    try:
        df['Data'] = pd.to_datetime(df['Data'], dayfirst=True, errors='coerce')
    except Exception:
        df['Data'] = pd.to_datetime(df['Data'], errors='coerce')

    # calcular métricas básicas
    receitas = df.loc[df['Tipo'] == 'Receita', 'Valor'].sum()
    despesas = df.loc[df['Tipo'] == 'Despesa', 'Valor'].sum()
    saldo = receitas - despesas
    taxa_poupanca_pct = (saldo / receitas * 100) if receitas != 0 else 0.0

    # distribuição por categoria (somente despesas)
    despesas_por_cat = (
        df.loc[df['Tipo'] == 'Despesa']
        .groupby('Categoria')['Valor']
        .sum()
        .sort_values(ascending=False)
        .to_dict()
    )

    # dívida detectada: categoria chamada 'Dívidas' ou 'Dividas'
    dividas = 0.0
    for key in ['Dívidas', 'Dividas', 'Divida', 'Dívida']:
        if key in df['Categoria'].unique():
            dividas += df.loc[
                (df['Categoria'] == key) & (df['Tipo'] == 'Despesa'), 'Valor'
                ].sum()

    resumo = {
        "receitas": round(float(receitas), 2),
        "despesas": round(float(despesas), 2),
        "saldo": round(float(saldo), 2),
        "taxa_poupanca_pct": round(float(taxa_poupanca_pct), 2),
        "despesas_por_categoria":
            {str(k): float(v) for k, v in despesas_por_cat.items()},
        "dividas": round(float(dividas), 2),
        "periodo_inicio": str(df['Data'].min()) if
            not df['Data'].isnull().all() else None,
        "periodo_fim": str(df['Data'].max()) if
            not df['Data'].isnull().all() else None,
    }
    return resumo


def montar_prompt_para_openai(resumo: dict) -> str:
    # Template em PT-BR para o modelo receber e gerar diagnóstico
    prompt = f"""
    Você é um especialista em finanças pessoais. Analise o resumo financeiro
    abaixo e gere um relatório de diagnóstico completo, claro e motivador.
    Divida o relatório em: Visão geral, Principais pontos de atenção,
    Oportunidades de economia, Plano de ação (3 a 5 passos) e Recomendação
    de produtos/contas para reserva de emergência.
    Seja prático e dê números concretos (valores em reais e percentuais).

    Resumo financeiro (auto-gerado):
    - Período: {resumo.get('periodo_inicio')} até {resumo.get('periodo_fim')}
    - Receitas totais: R$ {resumo.get('receitas'):.2f}
    - Despesas totais: R$ {resumo.get('despesas'):.2f}
    - Saldo: R$ {resumo.get('saldo'):.2f}
    - Taxa de poupança (% sobre a receita):
        {resumo.get('taxa_poupanca_pct'):.2f}%
    - Dívidas identificadas (valor): R$ {resumo.get('dividas'):.2f}
    - Distribuição das maiores categorias de despesa:
        {resumo.get('despesas_por_categoria')}

    Dê recomendações específicas com valores (ex.: "reduza X na categoria Y,
    isso economiza R$ Z por mês") e proponha metas (ex.: reserva de emergência
    equivalente a N meses de despesas).
    """
    return prompt


async def get_or_create_user(telegram_user):
    response = (
        supabase.table("user_sheets")
        .select("*")
        .eq("telegram_id", telegram_user.id)
        .execute()
    )

    if not response.data:
        # Criar usuário BÁSICO
        response = supabase.table("user_sheets").insert(
            {
                "telegram_id": telegram_user.id,
                "created_at": date.today().isoformat(),
                "username": telegram_user.username,
                "sheet_url": "https://docs.google.com/spreadsheets/d/EXAMPLE",
            }
        ).execute()

    return response.data[0] if response.data else None


# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user = await get_or_create_user(update.effective_user)

    await update.message.reply_text(
        (f"Seja bemvindo ao Finance Bot! {user['username']}\n"
         "Use /help para ver os comandos disponíveis.")
    )


async def help_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Comandos disponíveis:\n"
        "/start - Iniciar o bot\n"
        "/help - Mostrar esta mensagem de ajuda\n"
        "/save - Salvar um lançamento na planilha\n"
        "   /save [valor/categoria/tipo/descrição] (descrição é opcional)\n"
        "   Tipo deve ser 'Despesa' ou 'Receita'\n"
        "   Exemplo: /save 50,00/Alimentação/Despesa/Jantar com amigos\n"
        "/calc - Calcular uma expressão matemática\n"
        "   /calc [expressão]\n"
        "   Exemplo: /calc (3 + 5) * 2\n"
        "/last - Mostrar as últimas transações\n"
        "   /last [número] (padrão 5)\n"
        "/delete - Deletar uma transação pelo ID\n"
        "   /delete [ID da transação]\n"
        "/summary - Mostrar resumo financeiro atual\n"
        "/diagnostic - Gerar diagnóstico financeiro via IA\n"
        "Mensagens de texto livres também são aceitas para registro de "
        "transações, ex.: 'Gastei 30 reais em transporte hoje'."
    )
    await update.message.reply_text(help_text)


async def save_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            ("Por favor, forneça informações a registrar separadas por /."
             "Digite no formato: valor/categoria/tipo/descrição."
             "Descrição é opcional."
             )
        )
        return
    dados = context.args[0].split("/")
    if len(dados) < 3:
        await update.message.reply_text(
            ("Formato inválido. Use: valor/categoria/tipo/descrição"
             )
        )
        return
    valor = dados[0].replace(",", ".")
    try:
        float(valor)
    except ValueError:
        await update.message.reply_text(
            "O valor deve ser um número. Por favor, tente novamente."
        )
        return

    sheet = abrir_planilha()
    sheet.append_row(
        [
            date.today().strftime("%d/%m/%Y"),
            dados[3] if len(dados) > 3 else "",
            normalizar_string(dados[1]).capitalize(),
            float(valor),
            normalizar_string(dados[2]).capitalize(),
        ]
    )
    await update.message.reply_text(
        f"""
        Você registrou: {dados[0]} para {dados[1]}
        no dia {date.today().strftime('%d/%m/%Y')}.
        """
    )


async def calc_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Por favor, forneça uma expressão matemática para calcular."
        )
        return
    expressao = " ".join(context.args).replace(",", ".")
    try:
        resultado = avaliar_expressao_segura(expressao)
        await update.message.reply_text(
            f"O resultado de '{expressao}' é: {resultado:.2f}"
        )
    except Exception as e:
        logger.exception("Erro ao avaliar expressão")
        await update.message.reply_text(
            f"Erro ao avaliar a expressão: {e}"
        )


async def delete_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Por favor, forneça o ID da transação a ser deletada."
        )
        return
    try:
        row_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "O ID da transação deve ser um número inteiro."
        )
        return

    sheet = abrir_planilha()
    try:
        sheet.delete_rows(row_id)
        await update.message.reply_text(
            f"Transação com ID {row_id} deletada com sucesso."
        )
    except Exception as e:
        logger.exception("Erro ao deletar transação")
        await update.message.reply_text(
            f"Erro ao deletar a transação: {e}"
        )


async def print_summary(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sheet = abrir_planilha()
    registros = sheet.get_all_records(
      value_render_option=ValueRenderOption.unformatted)
    if not context.args:
        df = pd.DataFrame(registros)
    else:
        mes = int(context.args[0])
        ano = (int(context.args[1])
               if len(context.args) > 1
               else date.today().year)
        df = pd.DataFrame(registros)
        df = df[(df['Data'].dt.month == mes) &
                (df['Data'].dt.year == ano)]
    try:
        resumo = gerar_resumo_financeiro(df)
    except Exception as e:
        logger.exception("Erro no resumo financeiro")
        await update.message.reply_text(f"Erro ao processar os dados: {e}")
        return
    mensagem = (
        f"Resumo Financeiro: {mes}/{ano}\n"
        f"Receitas: R$ {resumo['receitas']}\n"
        f"Despesas: R$ {resumo['despesas']}\n"
        f"Saldo: R$ {resumo['saldo']}\n"
        f"Taxa de Poupança: {resumo['taxa_poupanca_pct']}%\n"
        f"Dívidas: R$ {resumo['dividas']}\n"
        f"Período: {resumo['periodo_inicio']} a {resumo['periodo_fim']}\n"
        f"Despesas por Categoria:\n"
    )
    for categoria, valor in resumo['despesas_por_categoria'].items():
        mensagem += f" - {categoria}: R$ {valor}\n"
    await update.message.reply_text(mensagem)


async def print_last_transactions(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sheet = abrir_planilha()
    registros = sheet.get_all_records(
      value_render_option=ValueRenderOption.unformatted
    )
    registros_com_indice = [
          # Adiciona índice baseado na posição na lista + 2 (cabeçalho)
          {**dicionario, 'id': indice + 2}
          for indice, dicionario in enumerate(registros)
        ]

    if not context.args:
        num_transacoes = 5
    else:
        try:
            num_transacoes = int(context.args[0])
        except ValueError:
            num_transacoes = 5
    if len(registros) == 0:
        await update.message.reply_text("Nenhuma transação registrada.")
        return
    elif num_transacoes > len(registros):
        num_transacoes = len(registros)
    ultimos = registros_com_indice[-num_transacoes:]  # Últimas transações
    mensagem = f"Últimas {num_transacoes} transações:\n"
    for registro in ultimos:
        mensagem += (
            f"{registro['id']}. "
            f"{registro['Data']}: {registro['Tipo']} de "
            f"{registro['Valor']} em {registro['Categoria']} - "
            f"{registro['Descrição']}\n"
        )
    await update.message.reply_text(mensagem)


async def diagnostic_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sheet = abrir_planilha()
    registros = sheet.get_all_records(
      value_render_option=ValueRenderOption.unformatted
    )
    df = pd.DataFrame(registros)
    try:
        resumo = gerar_resumo_financeiro(df)
    except Exception as e:
        logger.exception("Erro no resumo financeiro")
        await update.message.reply_text(f"Erro ao processar os dados: {e}")
        return
    prompt = montar_prompt_para_openai(resumo)
    # chamada básica para OpenAI (Chat Completions)
    try:
        # Ajuste de acordo com a SDK usada; aqui usamos a
        # API REST via client.chat.completions
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system",
                 "content":
                     """Você é um assistente especialista
                     em finanças pessoais."""},
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.3)
        texto_relatorio = response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception(f"Erro chamando OpenAI {e}")
        await update.message.reply_text(
            f"Erro ao gerar o relatório via OpenAI {e}")
        texto_relatorio = (
            "Relatório automático (fallback):\n\n"
            f"Receitas: R$ {resumo['receitas']}\n"
            f"Despesas: R$ {resumo['despesas']}\n"
            f"Saldo: R$ {resumo['saldo']}\n"
            f"Taxa poupança: {resumo['taxa_poupanca_pct']}%\n"
        )

    # enviar resultados
    # primeiro um resumo curto
    resumo_msg = (
        f"✅ Diagnóstico pronto!\n\n"
        f"Receitas: R$ {resumo['receitas']:.2f}\n"
        f"Despesas: R$ {resumo['despesas']:.2f}\n"
        f"Saldo: R$ {resumo['saldo']:.2f}\n"
        f"Taxa de poupança: {resumo['taxa_poupanca_pct']:.2f}%\n\n"
        "Relatório completo em anexo (PDF) e abaixo em texto."
        )
    await update.message.reply_text(resumo_msg)
    await update.message.reply_text(texto_relatorio)


async def interpretar(update, context):
    mensagem = update.message.text

    # Chama IA para extrair informação
    try:
        resposta = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": """Você é um sistema de categorização de
                        transações financeiras. Responda APENAS em formato
                        JSON válido, sem explicações adicionais."""
                },
                {
                    "role": "user",
                    "content": f"""Extraia da frase abaixo:
                        - valor (float, usar . como separador decimal)
                        - tipo: "Receita" ou "Despesa"
                        - categoria (uma palavra)
                        - data (DD/MM/YYYY; se não informado,
                            usar {date.today().strftime("%d/%m/%Y")})

                    Frase: "{mensagem}"

                    Retorne APENAS JSON, exemplo:
                        {{"valor": 58,.0, "tipo": "Despesa",
                            "categoria": "Alimentacao",
                            "data": "28/11/2025"}}"""
                }
            ],
            max_tokens=200,
            temperature=0.3
        )
        texto = resposta.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("Erro chamando OpenAI")
        await update.message.reply_text(f"Erro na API de IA: {e}")
        return

    # Extrair JSON do texto
    try:
        start = texto.find("{")
        end = texto.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("Nenhum JSON encontrado na resposta")

        json_text = texto[start:end]
        dados = json.loads(json_text)

        valor = float(dados["valor"])
        tipo = dados["tipo"].capitalize()
        categoria = dados["categoria"].capitalize()
        data_str = dados.get("data", date.today().isoformat()).strip()

        # Validar e normalizar data
        try:
            data_obj = pd.to_datetime(
                    data_str, format="%d/%m/%Y", errors='coerce')
            if pd.isna(data_obj):
                data = date.today().strftime("%d/%m/%Y")
            else:
                data = data_obj.strftime("%d/%m/%Y")
        except Exception as e:
            logger.exception("Erro ao parsear data: %s", e)
            data = date.today().strftime("%d/%m/%Y")

    except Exception as e:
        logger.exception("Erro ao parsear resposta da IA")
        await update.message.reply_text(
            f"Não consegui interpretar. Erro: {e}\nResposta: {texto[:500]}"
        )
        return

    try:
        sheet = abrir_planilha()
        # Adiciona uma nova linha na planilha com os dados extraídos
        sheet.append_row(
            [
                data,
                "",
                categoria,
                valor,
                tipo,
            ]
        )
        await update.message.reply_text(
            f"📌 Registrado!\n\n"
            f"Tipo: {tipo}\n"
            f"Categoria: {categoria}\n"
            f"Valor: R$ {valor:,.2f}\n"
            f"Data: {data}"
        )
    except Exception as e:
        logger.exception("Erro ao salvar na planilha")
        await update.message.reply_text(f"Erro ao salvar na planilha: {e}")


application.add_handler(
    CommandHandler("start", start, filters=authorized_only))
application.add_handler(
    CommandHandler("help", help_command, filters=authorized_only))
application.add_handler(
    CommandHandler("save", save_command, filters=authorized_only))
application.add_handler(
    CommandHandler("delete", delete_command, filters=authorized_only))
application.add_handler(
    CommandHandler("calc", calc_command, filters=authorized_only))
application.add_handler(
    CommandHandler(
       "last", print_last_transactions, filters=authorized_only))
application.add_handler(
    CommandHandler(
        "diagnostic", diagnostic_command, filters=authorized_only))
application.add_handler(
    CommandHandler(
        "summary", print_summary, filters=authorized_only))
application.add_handler(
    MessageHandler(
        authorized_only & filters.TEXT & ~filters.COMMAND, interpretar))


@app.get("/")
def hello_world():
    return {"mensagem": "Bot rodando"}


# ---- Webhook ----
@app.post("/webhook")
async def webhook(json_data: dict = None):
    try:
        update = Update.de_json(json_data, application.bot)
        await application.process_update(update)
        return {"mensagem": "Ok"}
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return {"mensagem": "Erro no webhook"}, 500
