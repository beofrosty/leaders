# app/services/users.py
import sqlite3
from flask import current_app

def get_user_by_email(email: str):
    # Путь к БД берём из конфигурации; иначе fallback
    db_path = current_app.config.get('DB', 'database.db')
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM users WHERE email = ?", (email,))
        return cur.fetchone()  # Row или None
