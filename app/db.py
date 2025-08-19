# app/db.py
import os, uuid, json
from datetime import datetime, timezone
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

# ---------- Пул соединений ----------
_pool: ConnectionPool | None = None

def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        # sslmode=require для Render Managed Postgres
        _pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=5, kwargs={"sslmode": "require"})
    return _pool

@contextmanager
def get_conn():
    with get_pool().connection() as conn:
        yield conn

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- Инициализация схемы ----------

def init_db():
    """Создаёт базовые таблицы, если их нет."""
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
        # индексы
        cur.execute("create index if not exists idx_users_email on users (email);")

        # applications (по умолчанию одна заявка на пользователя)
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

# ---------- ensure_* эквиваленты ----------

def ensure_user_columns():
    """В PostgreSQL лучше задать схему сразу в init_db(); этот хелпер оставлен на случай доработок."""
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

def ensure_users_index_email():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("create index if not exists idx_users_email on users(email);")
        conn.commit()

def ensure_applications_table():
    # Вся логика уже в init_db(); оставляем на совместимость
    init_db()

def ensure_tests_tables():
    # Уже создаются в init_db()
    init_db()

def ensure_password_resets_table():
    # Уже создаётся в init_db()
    init_db()

# ---------- Прикладные функции (эквиваленты sqlite-версий) ----------

def get_user_by_email(email: str):
    q = (email or "").strip().lower()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select id, email, full_name, form_data, commission_comment, commission_status, "
                    "test_score, test_answers, password_hash, is_verified, created_at, role "
                    "from users where email = %s",
                    (q,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))

def migrate_users_formdata_to_applications():
    """Переносит старые заявки из users.form_data в applications (один раз)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            select id as user_id, form_data, commission_comment, commission_status,
                   test_score, test_answers, created_at
            from users
            where form_data is not null
        """)
        rows = cur.fetchall()
        for (user_id, form_data, commission_comment, commission_status,
             test_score, test_answers, created_at) in rows:
            # есть ли уже заявка?
            cur.execute("select 1 from applications where user_id=%s limit 1", (user_id,))
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
                json.dumps(form_data) if isinstance(form_data, str) else form_data,
                commission_comment,
                commission_status,
                test_score,
                json.dumps(test_answers) if isinstance(test_answers, str) else test_answers,
                created_at,
            ))
        conn.commit()

def prune_password_resets():
    """Удаляет использованные и просроченные записи."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("delete from password_resets where used = true or expires_at < now()")
        conn.commit()

def user_has_application(user_id: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select 1 from applications where user_id=%s limit 1", (user_id,))
        return cur.fetchone() is not None

def get_user_applications(user_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select id, user_id, form_data, commission_comment, commission_status, "
                    "test_score, test_answers, created_at "
                    "from applications where user_id=%s order by created_at desc", (user_id,))
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
