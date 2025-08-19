# app/db.py
import os
import uuid
import json
import time
from datetime import datetime, timezone
from contextlib import contextmanager

from psycopg_pool import ConnectionPool
from psycopg import OperationalError
from psycopg.rows import dict_row

# ---------- Конфиг пула соединений ----------

_pool: ConnectionPool | None = None

def _dsn_with_ssl() -> str:
    """
    Берём DATABASE_URL и гарантируем sslmode=require.
    Поддерживает URL вида postgresql://user:pass@host:port/db?param=...
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    # Если sslmode не указан в строке, добавим его.
    if "sslmode=" not in dsn.lower():
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslmode=require"
    return dsn

def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_dsn_with_ssl(),
            min_size=1,
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            kwargs={
                "connect_timeout": 10,
                # keepalives (важно для облачных БД/прокси)
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )
    return _pool

@contextmanager
def get_conn():
    # Каждый вызов получает отдельное соединение из пула
    with get_pool().connection(timeout=15) as conn:
        yield conn

def _retry(fn, *args, **kwargs):
    """
    Короткий retry только на OperationalError (сетевые/SSL обрывы).
    """
    tries = 2
    delay = 0.2
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except OperationalError:
            if i == tries - 1:
                raise
            time.sleep(delay)
            delay *= 2

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- Инициализация схемы ----------

def init_db():
    """Создаёт базовые таблицы, если их нет."""
    def _exec():
        with get_conn() as conn, conn.cursor() as cur:
            # users
            cur.execute("""
            create table if not exists users (
                id uuid primary key,
                email text unique,
                full_name text,
                form_data jsonb,
                commission_comment text,
                commission_status text,
                test_score integer,
                test_answers jsonb,
                password_hash text,
                is_verified boolean default true,
                created_at timestamptz,
                role text default 'user'
            );
            """)
            cur.execute("create index if not exists idx_users_email on users (email);")

            # applications
            cur.execute("""
            create table if not exists applications (
                id uuid primary key,
                user_id uuid not null references users(id) on delete cascade,
                form_data jsonb,
                commission_comment text,
                commission_status text,
                test_score integer,
                test_answers jsonb,
                created_at timestamptz,
                constraint uq_app_user unique(user_id)
            );
            """)
            cur.execute("create index if not exists idx_app_user on applications(user_id);")
            cur.execute("create index if not exists idx_app_created on applications(created_at);")

            # tests
            cur.execute("""
            create table if not exists tests (
                id uuid primary key,
                title text not null,
                description text,
                duration_minutes integer,
                questions jsonb,            -- [{q, options:[...], correct_index}]
                created_at timestamptz
            );
            """)
            cur.execute("""
            create table if not exists test_attempts (
                id uuid primary key,
                user_id uuid not null references users(id) on delete cascade,
                test_id uuid not null references tests(id) on delete cascade,
                started_at timestamptz,
                finished_at timestamptz,
                score integer,
                answers jsonb                -- [0,2,1,...]
            );
            """)
            cur.execute("create index if not exists idx_attempt_user on test_attempts(user_id);")
            cur.execute("create index if not exists idx_attempt_test on test_attempts(test_id);")

            # password_resets
            cur.execute("""
            create table if not exists password_resets (
                id uuid primary key,
                user_id uuid not null references users(id) on delete cascade,
                token text not null unique,
                expires_at timestamptz not null,
                used boolean not null default false,
                created_at timestamptz not null default now()
            );
            """)
            cur.execute("create index if not exists idx_pr_user on password_resets(user_id);")
            cur.execute("create index if not exists idx_pr_expires on password_resets(expires_at);")

            conn.commit()
    return _retry(_exec)

# ---------- ensure_* эквиваленты ----------

def ensure_user_columns():
    """В PostgreSQL лучше задать схему сразу в init_db(); хелпер на случай доработок."""
    def _exec():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
            do $$
            begin
                if not exists (
                   select 1 from information_schema.columns
                    where table_name='users' and column_name='password_hash'
                ) then alter table users add column password_hash text; end if;

                if not exists (
                   select 1 from information_schema.columns
                    where table_name='users' and column_name='is_verified'
                ) then alter table users add column is_verified boolean default true; end if;

                if not exists (
                   select 1 from information_schema.columns
                    where table_name='users' and column_name='created_at'
                ) then alter table users add column created_at timestamptz; end if;

                if not exists (
                   select 1 from information_schema.columns
                    where table_name='users' and column_name='role'
                ) then alter table users add column role text default 'user'; end if;

                if not exists (
                   select 1 from information_schema.columns
                    where table_name='users' and column_name='form_data'
                ) then alter table users add column form_data jsonb; end if;

                if not exists (
                   select 1 from information_schema.columns
                    where table_name='users' and column_name='test_answers'
                ) then alter table users add column test_answers jsonb; end if;
            end $$;
            """)
            conn.commit()
    return _retry(_exec)

def ensure_users_index_email():
    def _exec():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("create index if not exists idx_users_email on users(email);")
            conn.commit()
    return _retry(_exec)

def ensure_applications_table():
    return init_db()

def ensure_tests_tables():
    return init_db()

def ensure_password_resets_table():
    return init_db()

# ---------- Прикладные функции ----------

def db_ping() -> bool:
    def _exec():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()
            return True
    return _retry(_exec)

def get_user_by_email(email: str):
    """
    Возвращает словарь пользователя по email или None.
    """
    q = (email or "").strip().lower()

    def _exec():
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                select id, email, full_name, form_data, commission_comment, commission_status,
                       test_score, test_answers, password_hash, is_verified, created_at, role
                from users where email = %s
            """, (q,))
            return cur.fetchone()  # dict или None
    return _retry(_exec)

def migrate_users_formdata_to_applications():
    """
    Переносит старые заявки из users.form_data в applications (один раз).
    Не «передампит» JSON: если form_data/test_answers строки — попробуем распарсить.
    """
    def _maybe_parse_json(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value  # оставим как есть
        return value

    def _exec():
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                select id as user_id, form_data, commission_comment, commission_status,
                       test_score, test_answers, created_at
                from users
                where form_data is not null
            """)
            rows = cur.fetchall() or []
            for r in rows:
                user_id = r["user_id"]
                form_data = _maybe_parse_json(r["form_data"])
                test_answers = _maybe_parse_json(r["test_answers"])

                # Есть ли уже заявка?
                cur.execute("select 1 as one from applications where user_id=%s limit 1", (user_id,))
                if cur.fetchone():
                    continue

                cur.execute("""
                    insert into applications
                    (id, user_id, form_data, commission_comment, commission_status,
                     test_score, test_answers, created_at)
                    values (%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict do nothing
                """, (
                    str(uuid.uuid4()),
                    user_id,
                    form_data,
                    r["commission_comment"],
                    r["commission_status"],
                    r["test_score"],
                    test_answers,
                    r["created_at"],
                ))
            conn.commit()
    return _retry(_exec)

def prune_password_resets():
    """Удаляет использованные и просроченные записи."""
    def _exec():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("delete from password_resets where used = true or expires_at < now()")
            conn.commit()
    return _retry(_exec)

def user_has_application(user_id: str) -> bool:
    def _exec():
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("select 1 from applications where user_id=%s limit 1", (user_id,))
            return cur.fetchone() is not None
    return _retry(_exec)

def get_user_applications(user_id: str):
    def _exec():
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                select id, user_id, form_data, commission_comment, commission_status,
                       test_score, test_answers, created_at
                from applications
                where user_id=%s
                order by created_at desc
            """, (user_id,))
            return cur.fetchall() or []
    return _retry(_exec)
