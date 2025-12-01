from dotenv import load_dotenv
import json
import os
import logging
import pandas as pd
from datetime import date
from utils import (
    conectar_google_sheets,
    normalizar_string,
    validar_chat_id)
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters)
from openai import OpenAI


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Filtro customizado para validação de chat_id
class AuthorizedOnly(filters.BaseFilter):
    async def filter(self, update: Update) -> bool:
        return validar_chat_id(update.message.chat.id)


authorized_only = AuthorizedOnly()


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


# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        ("Seja bemvindo ao Finance Bot! "
         "Use /help para ver os comandos disponíveis.")
    )


async def help_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Comandos disponíveis:\n"
        "/start - Iniciar o bot\n"
        "/help - Mostrar esta mensagem de ajuda\n"
        "/save - Salvar um lançamento na planilha\n"
        "   /save valor/categoria/tipo/descrição (descrição é opcional)\n"
        "   Tipo deve ser 'Despesa' ou 'Receita'\n"
        "   Exemplo: /save 50,00/Alimentação/Despesa/Jantar com amigos\n"
        "/last - Mostrar as últimas 5 transações\n"
        "/diagnostic - Analisar a planilha e fornecer insights\n"
    )
    await update.message.reply_text(help_text)


async def save_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Verifica se o usuário forneceu argumentos
    if not context.args:
        await update.message.reply_text(
            ("Por favor, forneça informações a registrar separadas por /."
             "Digite no formato: valor/categoria/tipo/descrição."
             "Descrição é opcional."
             )
        )
        return
    # Obtém os dados fornecidos pelo usuário e separa-os
    dados = context.args[0].split("/")
    # Verifica se há pelo menos 3 partes (valor, categoria, tipo)
    if len(dados) <= 3:
        await update.message.reply_text(
            ("Formato inválido. Use: valor/categoria/tipo/descrição"
             )
        )
        return
    # Valida o valor fornecido
    valor = dados[0].replace(",", ".")
    try:
        float(valor)
    except ValueError:
        await update.message.reply_text(
            "O valor deve ser um número. Por favor, tente novamente."
        )
        return
    cliente = conectar_google_sheets()
    planilha = cliente.open("Minhas Finanças Pessoais")
    sheet = planilha.worksheet("Transações")
    # Adiciona uma nova linha na planilha com os dados fornecidos
    sheet.append_row(
        [
            date.today().strftime("%d/%m/%Y"),
            dados[3] if len(dados) > 3 else "",
            normalizar_string(dados[1]).capitalize(),
            float(valor),
            normalizar_string(dados[2]).capitalize(),
        ]
    )
    # Dá feedback ao usuário
    await update.message.reply_text(
        f"""
        Você registrou: {dados[0]} para {dados[1]}
        no dia {date.today().strftime('%d/%m/%Y')}.
        """
    )


async def print_last_transactions(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cliente = conectar_google_sheets()
    planilha = cliente.open("Minhas Finanças Pessoais")
    sheet = planilha.worksheet("Transações")
    registros = sheet.get_all_records()
    ultimos = registros[-5:]  # Últimas 5 transações
    mensagem = "Últimas 5 transações:\n"
    for registro in ultimos:
        mensagem += (
            f"{registro['Data']}: {registro['Tipo']} de "
            f"{registro['Valor']} em {registro['Categoria']} - "
            f"{registro['Descrição']}\n"
        )
    await update.message.reply_text(mensagem)


async def diagnostic_command(
        update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cliente = conectar_google_sheets()
    planilha = cliente.open("Minhas Finanças Pessoais")
    sheet = planilha.worksheet("Transações")
    registros = sheet.get_all_records()
    df = pd.DataFrame(registros)
    try:
        resumo = gerar_resumo_financeiro(df)
    except Exception as e:
        logger.exception("Erro no resumo financeiro")
        await update.message.reply_text(f"Erro ao processar os dados: {e}")
        return
    # mensagem = (
    #     f"Resumo Financeiro:\n"
    #     f"Receitas: {resumo['receitas']}\n"
    #     f"Despesas: {resumo['despesas']}\n"
    #     f"Saldo: {resumo['saldo']}\n"
    #     f"Taxa de Poupança: {resumo['taxa_poupanca_pct']}%\n"
    #     f"Dívidas: {resumo['dividas']}\n"
    #     f"Período: {resumo['periodo_inicio']} a {resumo['periodo_fim']}\n"
    #     f"Despesas por Categoria:\n"
    # )
    # for categoria, valor in resumo['despesas_por_categoria'].items():
    #     mensagem += f" - {categoria}: {valor}\n"
    # await update.message.reply_text(mensagem)
    prompt = montar_prompt_para_openai(resumo)
    # chamada básica para OpenAI (Chat Completions)
    try:
        # Ajuste de acordo com a SDK usada; aqui usamos a
        # API REST via client.chat.completions
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "developer",
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
                        - data (YYYY-MM-DD; se não informado,
                            usar {date.today().isoformat()})

                    Frase: "{mensagem}"

                    Retorne APENAS JSON, exemplo:
                        {{"valor": 58.0, "tipo": "Despesa",
                            "categoria": "Alimentacao",
                            "data": "2025-11-28"}}"""
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
                    data_str, format="%Y-%m-%d", errors='coerce')
            if pd.isna(data_obj):
                data = date.today().isoformat()
            else:
                data = data_obj.strftime("%Y-%m-%d")
        except Exception as e:
            logger.exception("Erro ao parsear data: %s", e)
            data = date.today().isoformat()

    except Exception as e:
        logger.exception("Erro ao parsear resposta da IA")
        await update.message.reply_text(
            f"Não consegui interpretar. Erro: {e}\nResposta: {texto[:500]}"
        )
        return

    try:
        cliente = conectar_google_sheets()
        planilha = cliente.open("Minhas Finanças Pessoais")
        sheet = planilha.worksheet("Transações")
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


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Defina as variáveis de ambiente TELEGRAM_TOKEN.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(
        CommandHandler("start", start, filters=authorized_only))
    app.add_handler(
        CommandHandler("help", help_command, filters=authorized_only))
    app.add_handler(
        CommandHandler("save", save_command, filters=authorized_only))
    app.add_handler(
        CommandHandler(
            "last", print_last_transactions, filters=authorized_only))
    app.add_handler(
        CommandHandler(
            "diagnostic", diagnostic_command, filters=authorized_only))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, interpretar))
    logger.info("Bot iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()
