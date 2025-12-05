from flask import Flask, request
import asyncio
import ast
from datetime import date
import json
import os
import logging
from dotenv import load_dotenv

import pandas as pd

from utils import (
    conectar_google_sheets,
    normalizar_string,
)

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI


# ==========================================================
# CONFIGURAÇÕES
# ==========================================================

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_LIST = ast.literal_eval(os.getenv("CHAT_ID_LIST", "[]"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


# ==========================================================
# FLASK APP
# ==========================================================

app = Flask(__name__)

# ==========================================================
# TELEGRAM APP
# ==========================================================

application = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .build()
)


# ==========================================================
# FILTRO DE CHAT AUTORIZADO
# ==========================================================

class AuthorizedOnlyFilter(filters.MessageFilter):
    def filter(self, update: Update) -> bool:
        if not update or not update.from_user:
            return False
        return update.from_user.id in CHAT_ID_LIST


authorized_only = AuthorizedOnlyFilter()


# ==========================================================
# FUNÇÕES DO BOT
# ==========================================================

def abrir_planilha():
    try:
        cliente = conectar_google_sheets()
        planilha = cliente.open("Minhas Finanças Pessoais")
        sheet = planilha.worksheet("Transações")
        return sheet
    except Exception as e:
        logger.exception(f"Erro ao abrir a planilha: {e}")
        raise RuntimeError("Não foi possível abrir a planilha.")


# ----------------------- HANDLERS -----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Seja bem-vindo ao Finance Bot! Use /help.")


async def help_command(update, context):
    txt = (
        "Comandos disponíveis:\n"
        "/save valor/categoria/tipo/descrição\n"
        "/last (n) — mostra últimas transações\n"
    )
    await update.message.reply_text(txt)


async def save_command(update, context):
    if not context.args:
        await update.message.reply_text(
            "Use: valor/categoria/tipo/descrição (descrição opcional)."
        )
        return

    dados = context.args[0].split("/")
    if len(dados) < 3:
        await update.message.reply_text("Formato inválido.")
        return

    valor_str = dados[0].replace(",", ".")
    try:
        valor = float(valor_str)
    except:
        await update.message.reply_text("Valor inválido.")
        return

    categoria = normalizar_string(dados[1]).capitalize()
    tipo = normalizar_string(dados[2]).capitalize()
    descricao = dados[3] if len(dados) > 3 else ""

    sheet = abrir_planilha()
    sheet.append_row([
        date.today().strftime("%d/%m/%Y"),
        descricao,
        categoria,
        valor,
        tipo,
    ])

    await update.message.reply_text("Registro salvo com sucesso!")


async def print_last_transactions(update, context):
    sheet = abrir_planilha()
    registros = sheet.get_all_records()
    num = int(context.args[0]) if context.args else 5
    num = min(num, len(registros))

    ultimos = registros[-num:]
    msg = "Últimas transações:\n\n"
    for r in ultimos:
        msg += (
            f"{r['Data']} — {r['Tipo']} R$ {r['Valor']} em "
            f"{r['Categoria']} ({r['Descrição']})\n"
        )

    await update.message.reply_text(msg)


async def interpretar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text

    # Chama IA para extrair dados
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system",
                 "content": "Extraia informações financeiras e responda apenas JSON."},
                {"role": "user",
                 "content": f"""
                    Frase: "{texto}"
                    Extraia:
                    - valor
                    - tipo (Receita/Despesa)
                    - categoria
                    - data (DD/MM/YYYY ou hoje)
                 """}
            ],
            temperature=0.3
        )

        content = resp.choices[0].message.content.strip()
        json_str = content[content.find("{"):content.rfind("}") + 1]
        dados = json.loads(json_str)

    except Exception as e:
        await update.message.reply_text(f"Erro ao interpretar: {e}")
        return

    # Normalização
    valor = float(dados["valor"])
    tipo = dados["tipo"].capitalize()
    categoria = dados["categoria"].capitalize()

    try:
        data_obj = pd.to_datetime(dados.get("data"), format="%d/%m/%Y")
        data_fmt = data_obj.strftime("%d/%m/%Y")
    except:
        data_fmt = date.today().strftime("%d/%m/%Y")

    # Salva na planilha
    try:
        sheet = abrir_planilha()
        sheet.append_row([data_fmt, "", categoria, valor, tipo])
    except Exception as e:
        await update.message.reply_text(f"Erro ao salvar: {e}")
        return

    await update.message.reply_text(
        f"📌 Registrado!\n"
        f"Tipo: {tipo}\n"
        f"Categoria: {categoria}\n"
        f"Valor: R$ {valor:.2f}\n"
        f"Data: {data_fmt}"
    )


# ==========================================================
# REGISTRO DOS HANDLERS
# ==========================================================

application.add_handler(CommandHandler("start", start, filters=authorized_only))
application.add_handler(CommandHandler("help", help_command, filters=authorized_only))
application.add_handler(CommandHandler("save", save_command, filters=authorized_only))
application.add_handler(CommandHandler("last", print_last_transactions, filters=authorized_only))

application.add_handler(
    MessageHandler(authorized_only & filters.TEXT & ~filters.COMMAND, interpretar)
)



# ==========================================================
# FLASK ROUTES
# ==========================================================

@app.route("/")
def home():
    return "Bot is running."


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)

        # processa o update dentro do loop global
        loop = asyncio.get_event_loop()
        loop.create_task(application.process_update(update))

        return "ok"
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return "erro", 500


# ==========================================================
# APP LOCAL
# ==========================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
