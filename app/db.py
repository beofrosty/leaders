# app/db.py — PostgreSQL (psycopg3) + пул подключений
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

try:
    # current_app доступен только внутри контекста Flask
    from flask import current_app
except Exception:  # на случай запуска утилит вне Flask
    current_app = None  # type: ignore


# ---------- Глобальные объекты пула/DSN ----------

_pool: Optional[ConnectionPool] = None
_dsn_cache: Optional[str] = None


# ---------- DSN и соединения ----------

def _resolve_dsn(app=None) -> str:
    """
    Приоритет источников DSN:
      1) app.config["DB_DSN"] или app.config["DATABASE_URL"] (если app передан)
      2) current_app.config["DB_DSN"] / ["DATABASE_URL"] (если есть контекст)
      3) переменные окружения DB_DSN / DATABASE_URL
    Формат DSN: postgresql://user:pass@host:5432/dbname
    """
    # 1) из явно переданного app
    if app is not None:
        d = app.config.get("DB_DSN") or app.config.get("DATABASE_URL")
        if d:
            return d

    # 2) из текущего Flask-приложения
    try:
        if current_app:  # type: ignore
            d = current_app.config.get("DB_DSN") or current_app.config.get("DATABASE_URL")
            if d:
                return d
    except Exception:
        pass

    # 3) из окружения
    d = os.getenv("DB_DSN") or os.getenv("DATABASE_URL")
    if d:
        return d

    raise RuntimeError("DSN не задан. Установи DB_DSN или DATABASE_URL.")


def init_pool(app=None) -> None:
    global _pool, _dsn_cache
    if _pool is not None:
        return
    dsn = _resolve_dsn(app)
    _dsn_cache = dsn

    # короче живём и чаще обновляем соединения, чтобы не протухали
    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=int(os.getenv("PG_MIN_POOL", "1")),
        max_size=int(os.getenv("PG_MAX_POOL", "10")),
        max_lifetime=300,   # секунды: пересоздавать соединения каждые ~5 минут
        max_idle=60,        # держать неиспользуемые не дольше минуты
        kwargs={
            "row_factory": dict_row,
            # агрессивные keepalive'ы
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            # sslmode=require уже гарантируется _augment_conninfo()
        },
    )

    # подождать, пока пул наполнится, иначе первый запрос словит PoolTimeout
    try:
        _pool.wait(timeout=10)
    except Exception:
        # не падаем на старте — просто дадим приложению попытаться ещё раз
        pass



def get_conn():
    if _pool is not None:
        return _pool.connection()
    dsn = _augment_conninfo(_dsn_cache or _resolve_dsn())
    return psycopg.connect(
        dsn,
        row_factory=dict_row,
        connect_timeout=10,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        options="-c statement_timeout=15000",
    )


# ---------- Инициализация схемы ----------

def bootstrap_schema() -> None:
    """
    Создаёт недостающие таблицы/колонки/индексы. Идемпотентно.
    Типы полей оставлены максимально близкими к SQLite-версии:
      - JSON-поля как TEXT (строки), чтобы не ломать json.loads(...) в существующих роутерах.
      - время — TIMESTAMPTZ.
    """
    with get_conn() as conn, conn.cursor() as c:
        # users
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            full_name TEXT,
            form_data TEXT,                -- JSON-строка (как было)
            commission_comment TEXT,
            commission_status TEXT,
            test_score INTEGER,
            test_answers TEXT,             -- JSON-строка
            password_hash TEXT,
            is_verified BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            role TEXT DEFAULT 'user'
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users (LOWER(email))")

        # applications
        c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            form_data TEXT,                -- JSON-строка
            commission_comment TEXT,
            commission_status TEXT,
            test_score INTEGER,
            test_answers TEXT,             -- JSON-строка
            created_at TIMESTAMPTZ DEFAULT NOW(),
            test_link TEXT
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_app_user ON applications (user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_app_created ON applications (created_at DESC)")

        # tests
        c.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            duration_minutes INTEGER,
            questions TEXT,                -- JSON-строка
            created_at TIMESTAMPTZ DEFAULT NOW(),
            is_published BOOLEAN NOT NULL DEFAULT FALSE
        )
        """)
        # ВАЖНО: сначала гарантируем наличие колонки в старых схемах,
        # потом создаём индекс по этой колонке
        c.execute("ALTER TABLE tests ADD COLUMN IF NOT EXISTS is_published BOOLEAN NOT NULL DEFAULT FALSE")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tests_created ON tests (created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tests_published ON tests (is_published)")

        # test_attempts
        c.execute("""
        CREATE TABLE IF NOT EXISTS test_attempts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            test_id TEXT NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            score INTEGER,
            answers TEXT                   -- JSON-строка
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_user ON test_attempts (user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_test ON test_attempts (test_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_finished ON test_attempts (finished_at DESC)")

        # password_resets
        c.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            used BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_user ON password_resets (user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_expires ON password_resets (expires_at)")

        # На случай устаревших схем — гарантируем наличие новых колонок
        c.execute("ALTER TABLE tests        ADD COLUMN IF NOT EXISTS is_published BOOLEAN NOT NULL DEFAULT FALSE")
        c.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS test_link TEXT")
        c.execute("ALTER TABLE users        ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'")
        c.execute("ALTER TABLE users        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
        c.execute("ALTER TABLE users        ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT TRUE")
        c.execute("ALTER TABLE users        ADD COLUMN IF NOT EXISTS password_hash TEXT")
        c.execute("ALTER TABLE users        ADD COLUMN IF NOT EXISTS form_data TEXT")
        c.execute("ALTER TABLE users        ADD COLUMN IF NOT EXISTS test_answers TEXT")

        conn.commit()


# Совместимость со старым кодом: init_db() вызывает bootstrap_schema()
def init_db() -> None:
    bootstrap_schema()


# ---------- ensure_* (оставлены для совместимости) ----------

def ensure_user_columns() -> None:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT TRUE")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'")
        conn.commit()


def ensure_users_index_email() -> None:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users (LOWER(email))")
        conn.commit()


def ensure_applications_table() -> None:
    # для совместимости со старым вызовом
    init_db()


def ensure_applications_columns() -> None:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS test_link TEXT")
        conn.commit()


def ensure_tests_tables() -> None:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("ALTER TABLE tests ADD COLUMN IF NOT EXISTS is_published BOOLEAN NOT NULL DEFAULT FALSE")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tests_created ON tests (created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tests_published ON tests (is_published)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_user ON test_attempts (user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_test ON test_attempts (test_id)")
        conn.commit()


def ensure_password_resets_table() -> None:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_user ON password_resets (user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_expires ON password_resets (expires_at)")
        conn.commit()


# ---------- Утилиты для кода приложения ----------

def get_user_by_email(email: str):
    """
    Ищет пользователя по e-mail (case-insensitive). Возвращает dict или None.
    """
    e = (email or "").strip()
    if not e:
        return None
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(%s)", (e,))
        return c.fetchone()


def user_has_application(user_id: str) -> bool:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT 1 FROM applications WHERE user_id = %s LIMIT 1", (user_id,))
        return c.fetchone() is not None


def get_user_applications(user_id: str):
    """
    Возвращает список заявок пользователя (list[dict]).
    """
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
            SELECT id, user_id, form_data, commission_comment, commission_status,
                   test_score, test_answers, created_at, test_link
              FROM applications
             WHERE user_id = %s
             ORDER BY created_at DESC NULLS LAST
        """, (user_id,))
        return c.fetchall()


def migrate_users_formdata_to_applications() -> None:
    """
    Разовая миграция users.form_data -> applications.
    Создаёт одну заявку, если у пользователя ещё нет.
    """
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT id AS user_id, form_data, commission_comment, commission_status,
                 test_score, test_answers, created_at
            FROM users
           WHERE form_data IS NOT NULL
        """)
        rows = c.fetchall() or []
        for r in rows:
            c.execute("SELECT 1 FROM applications WHERE user_id = %s LIMIT 1", (r["user_id"],))
            if c.fetchone():
                continue
            c.execute("""
              INSERT INTO applications
                (id, user_id, form_data, commission_comment, commission_status,
                 test_score, test_answers, created_at)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(uuid.uuid4()),
                r["user_id"],
                r["form_data"],
                r["commission_comment"],
                r["commission_status"],
                r["test_score"],
                r["test_answers"],
                r["created_at"],   # если была строка-ISO — Postgres сам попробует распарсить
            ))
        conn.commit()


def prune_password_resets() -> None:
    """
    Удаляет использованные и просроченные токены сброса пароля.
    """
    with get_conn() as conn, conn.cursor() as c:
        c.execute("DELETE FROM password_resets WHERE used = TRUE OR expires_at < NOW()")
        conn.commit()
def _augment_conninfo(dsn: str) -> str:
    # добавляем sslmode=require, если не указан
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn

