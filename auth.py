"""
Авторизация в Gmail через OAuth 2.0.
При первом запуске открывает браузер для входа в Google.
После авторизации токен сохраняется в credentials/token.json.
"""

import json
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from config import Config

# Права: Gmail API (читать, удалять, метки) + IMAP/SMTP (IDLE) + Calendar
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",        # для IMAP IDLE
    "https://www.googleapis.com/auth/calendar.events",  # для создания событий
]


def get_gmail_credentials() -> Credentials | None:
    """Возвращает готовые credentials для Gmail API."""
    creds = None

    # Пробуем загрузить сохранённый токен
    if os.path.isfile(Config.TOKEN_FILE):
        with open(Config.TOKEN_FILE) as f:
            token_data = json.load(f)
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    # Если токен протух — обновляем
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    # Если токена нет — запускаем OAuth-flow в браузере
    if not creds or not creds.valid:
        if not os.path.isfile(Config.OAUTH_CREDENTIALS_FILE):
            print(f"❌ Файл не найден: {Config.OAUTH_CREDENTIALS_FILE}")
            print("   Инструкция: https://developers.google.com/gmail/api/quickstart/python")
            return None

        flow = InstalledAppFlow.from_client_secrets_file(
            Config.OAUTH_CREDENTIALS_FILE, SCOPES
        )
        creds = flow.run_local_server(port=0)  # открывает браузер

        # Сохраняем токен
        os.makedirs(Config.CREDENTIALS_DIR, exist_ok=True)
        with open(Config.TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"✅ Токен сохранён в {Config.TOKEN_FILE}")

    return creds
