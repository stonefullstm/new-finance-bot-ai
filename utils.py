import gspread
import unicodedata
import os
import ast
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()
CHAT_ID_LIST = ast.literal_eval(os.getenv("CHAT_ID_LIST"))


def validar_chat_id(chat_id: int) -> bool:
    if chat_id not in CHAT_ID_LIST:
        return False
    return True


def conectar_google_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_file(
        "credenciais.json", scopes=scopes)

    client = gspread.authorize(creds)
    return client


def normalizar_string(s: str) -> str:
    normalized = unicodedata.normalize('NFKD', s)
    folded = "".join([c for c in normalized if not unicodedata.combining(c)])
    return folded.casefold()
