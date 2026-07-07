import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Ollama — локальная LLM
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.2")

    # Gmail OAuth
    CREDENTIALS_DIR: str = os.path.join(os.path.dirname(__file__), "credentials")
    OAUTH_CREDENTIALS_FILE: str = os.path.join(CREDENTIALS_DIR, "gmail_oauth.json")
    TOKEN_FILE: str = os.path.join(CREDENTIALS_DIR, "token.json")

    # Gmail email для IMAP IDLE
    GMAIL_EMAIL: str = os.getenv("GMAIL_EMAIL", "")

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Интервал проверки
    CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

    # Белый / чёрный список доменов (через запятую)
    AUTO_DELETE_DOMAINS: list[str] = [
        d.strip().lower()
        for d in os.getenv("AUTO_DELETE_DOMAINS", "").split(",")
        if d.strip()
    ]
    AUTO_KEEP_DOMAINS: list[str] = [
        d.strip().lower()
        for d in os.getenv("AUTO_KEEP_DOMAINS", "").split(",")
        if d.strip()
    ]

    # Умные Gmail-метки
    LABELS_ENABLED: bool = os.getenv("LABELS_ENABLED", "true").lower() in ("1", "true", "yes")

    # Порог уверенности для review
    CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))

    # Время отправки дайджеста (час локального времени, 0-23)
    DIGEST_HOUR: int = int(os.getenv("DIGEST_HOUR", "21"))

    @classmethod
    def validate(cls):
        errors = []
        if not os.path.isfile(cls.OAUTH_CREDENTIALS_FILE):
            errors.append(
                f"Файл OAuth-учётных данных не найден:\n"
                f"  {cls.OAUTH_CREDENTIALS_FILE}\n"
            )
        # Проверяем, что Ollama доступен
        try:
            import requests
            resp = requests.get(f"{cls.OLLAMA_URL}/api/tags", timeout=2)
            if resp.status_code != 200:
                errors.append(f"Ollama не отвечает по адресу {cls.OLLAMA_URL}")
        except Exception:
            errors.append(
                f"Ollama не запущен. Запусти: ollama serve"
            )
        return errors
