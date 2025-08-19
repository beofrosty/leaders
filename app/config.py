import os
from datetime import timedelta

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "my_super_secret_key_1234567890")
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    DB_PATH = os.getenv("DB_PATH", "database.db")
    DATABASE_URL = os.getenv("DATABASE_URL")

    # Mail
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", 587))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True") == "True"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "aku03082015@gmail.com")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "srguaheersflckcu")
    MAIL_DEFAULT_SENDER = (os.getenv("MAIL_SENDER_NAME", "Комиссия"),
                           os.getenv("MAIL_USERNAME", "aku03082015@gmail.com"))

    ADMIN_INVITE_CODE = os.getenv("ADMIN_INVITE_CODE", "12345")
