# app/migrations.py
from datetime import timedelta
import os
from psycopg import errors

from .db import get_conn, bootstrap_schema

MIGRATION_LOCK_ID = 764392  # любое фиксированное число проекта

def run_migrations():
    # Можно на лету ограничить таймауты, чтобы не зависнуть
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SET lock_timeout TO '5s'")
        c.execute("SET statement_timeout TO '600s'")
        # Сериализация между несколькими инстансами, если вдруг поднимутся параллельно
        c.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            # ВАЖНО: выполняем внутри того же conn
            bootstrap_schema(conn=conn)
            # bootstrap_schema уже делает commit(conn)
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))

if __name__ == "__main__":
    run_migrations()
    print("Migrations applied: OK")
