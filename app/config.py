# config.py
import os
from datetime import timedelta

def env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, str(default))).strip().lower() in {"1", "true", "t", "yes", "y", "on"}

class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-prod")
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.getenv("SESSION_DAYS", "30")))

    # === Database ===
    # Для PostgreSQL укажи:
    #   export DB_DSN="postgresql://USER:PASS@HOST:5432/leaders"
    # или используй стандартное имя DATABASE_URL
    DB_DSN = os.getenv("DB_DSN") or os.getenv("DATABASE_URL")
    # Оставляем для обратной совместимости, если где-то ещё упоминается:
    DB_PATH = os.getenv("DB_PATH", "database.db")

    # === Mail ===
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = env_bool("MAIL_USE_TLS", True)
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = (
        os.getenv("MAIL_SENDER_NAME", "Комиссия"),
        os.getenv("MAIL_USERNAME")
    )

    # Прочее
    ADMIN_INVITE_CODE = os.getenv("ADMIN_INVITE_CODE")
