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
    """
    Инициализирует пул подключений. Вызывается из create_app().
    """
    global _pool, _dsn_cache
    if _pool is not None:
        return
    dsn = _resolve_dsn(app)
    _dsn_cache = dsn
    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=int(os.getenv("PG_MIN_POOL", "1")),
        max_size=int(os.getenv("PG_MAX_POOL", "10")),
        kwargs={"row_factory": dict_row},
    )


def get_conn():
    """
    Возвращает подключение к БД.
      - если пул инициализирован → pooled connection (context manager)
      - иначе одиночное соединение (fallback)
    Пример:
        with get_conn() as conn, conn.cursor() as c:
            c.execute("SELECT 1")
    """
    if _pool is not None:
        return _pool.connection()

    dsn = _dsn_cache or _resolve_dsn()
    return psycopg.connect(dsn, row_factory=dict_row)


# ---------- Инициализация схемы ----------

def _apply_bootstrap_sql(c) -> None:
    """
    Вся DDL-логика вынесена в отдельную функцию, принимающую курсор.
    Никаких commit/rollback внутри — транзакцией управляет вызывающий код.
    """
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
    # --- авто-номер заявки (public_no) ---
    c.execute("CREATE SEQUENCE IF NOT EXISTS applications_public_no_seq")

    # колонка
    c.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS public_no INTEGER")
    # дефолт из последовательности
    c.execute("""
      ALTER TABLE applications
      ALTER COLUMN public_no SET DEFAULT nextval('applications_public_no_seq')
    """)

    # проставить значения там, где NULL
    # синхронизуем последовательность с текущим максимумом public_no
    c.execute("""
      SELECT setval(
        'applications_public_no_seq',
        COALESCE((SELECT MAX(public_no) FROM applications), 0) + 1,
        false
      )
    """)
    c.execute("UPDATE applications SET public_no = nextval('applications_public_no_seq') WHERE public_no IS NULL")

    # индексы/ограничения
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_app_public_no ON applications (public_no)")
    c.execute("ALTER TABLE applications ALTER COLUMN public_no SET NOT NULL")

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

    # --- новые поля users ---
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS access_expires_at TIMESTAMPTZ")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS inn TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS position TEXT")  # Должность
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS priority TEXT")

    # --- журнал действий внутренних пользователей ---
    c.execute("""
    CREATE TABLE IF NOT EXISTS internal_user_logs (
        id TEXT PRIMARY KEY,
        actor_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        target_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,     -- 'create' | 'extend' | 'delete'
        meta TEXT,                -- JSON-строка
        ip INET,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS iul_actor_idx   ON internal_user_logs (actor_user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS iul_target_idx  ON internal_user_logs (target_user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS iul_created_idx ON internal_user_logs (created_at DESC)")

    # --- журнал решений комиссии ---
    c.execute("""
    CREATE TABLE IF NOT EXISTS commission_logs (
        id          TEXT PRIMARY KEY,
        app_id      TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        admin_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
        action      TEXT NOT NULL,          -- 'update_status' | 'decision'
        old_status  TEXT,
        new_status  TEXT,
        comment     TEXT,
        ip_addr     TEXT,
        user_agent  TEXT,
        meta        TEXT,                    -- JSON-строка для доп.данных
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_comm_logs_app     ON commission_logs (app_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comm_logs_created ON commission_logs (created_at DESC)")

    # --- нормализация статусов в коды (approved/rejected) ---

    # 1) Функция: строка -> код
    c.execute("""
    CREATE OR REPLACE FUNCTION to_status_code(s text)
    RETURNS text
    LANGUAGE plpgsql
    AS $$
    DECLARE v text;
    BEGIN
      IF s IS NULL OR btrim(s) = '' THEN
        RETURN NULL;
      END IF;

      v := lower(btrim(s));

      -- approved
      IF v LIKE 'одобр%%' OR v IN ('approve','approved','ok','accept','accepted','да','true','1') THEN
        RETURN 'approved';
      END IF;

      -- rejected
      IF v LIKE 'отклон%%' OR v IN ('reject','rejected','deny','denied','cancel','нет','false','0') THEN
        RETURN 'rejected';
      END IF;

      -- иначе оставим как есть (на случай кастомных статусов)
      RETURN s;
    END;
    $$;
    """)

    # 2) Триггеры: нормализуем значения при вставке/обновлении
    c.execute("""
    CREATE OR REPLACE FUNCTION trg_applications_status_norm()
    RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      NEW.commission_status := to_status_code(NEW.commission_status);
      RETURN NEW;
    END $$;
    """)
    c.execute("DROP TRIGGER IF EXISTS trg_applications_status_norm ON applications")
    c.execute("""
    CREATE TRIGGER trg_applications_status_norm
    BEFORE INSERT OR UPDATE OF commission_status ON applications
    FOR EACH ROW EXECUTE FUNCTION trg_applications_status_norm();
    """)

    c.execute("""
    CREATE OR REPLACE FUNCTION trg_commission_logs_status_norm()
    RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      NEW.old_status := to_status_code(NEW.old_status);
      NEW.new_status := to_status_code(NEW.new_status);
      RETURN NEW;
    END $$;
    """)
    c.execute("DROP TRIGGER IF EXISTS trg_commission_logs_status_norm ON commission_logs")
    c.execute("""
    CREATE TRIGGER trg_commission_logs_status_norm
    BEFORE INSERT OR UPDATE OF old_status, new_status ON commission_logs
    FOR EACH ROW EXECUTE FUNCTION trg_commission_logs_status_norm();
    """)

    # 3) Миграция существующих строк -> коды
    c.execute("""
    UPDATE applications
       SET commission_status = to_status_code(commission_status)
     WHERE commission_status IS NOT NULL
    """)
    c.execute("""
    UPDATE commission_logs
       SET old_status = to_status_code(old_status),
           new_status = to_status_code(new_status)
     WHERE old_status IS NOT NULL OR new_status IS NOT NULL
    """)

    # 3.1) Саницация: всё, что не распознали, обнуляем (чтобы прошёл CHECK)
    c.execute("""
    UPDATE applications
       SET commission_status = NULL
     WHERE commission_status IS NOT NULL
       AND commission_status NOT IN ('approved','rejected')
    """)
    c.execute("""
    UPDATE commission_logs
       SET old_status = NULL
     WHERE old_status IS NOT NULL
       AND old_status NOT IN ('approved','rejected')
    """)
    c.execute("""
    UPDATE commission_logs
       SET new_status = NULL
     WHERE new_status IS NOT NULL
       AND new_status NOT IN ('approved','rejected')
    """)

    # 4) CHECK-ограничения (разрешим только 'approved'/'rejected' или NULL)
    #    Сначала дропнем на случай пере-запуска
    c.execute("ALTER TABLE applications DROP CONSTRAINT IF EXISTS chk_app_commission_status_codes")
    c.execute("ALTER TABLE commission_logs DROP CONSTRAINT IF EXISTS chk_commission_logs_status_codes")
    c.execute("""
    ALTER TABLE applications
      ADD CONSTRAINT chk_app_commission_status_codes
      CHECK (commission_status IN ('approved','rejected') OR commission_status IS NULL)
    """)
    c.execute("""
    ALTER TABLE commission_logs
      ADD CONSTRAINT chk_commission_logs_status_codes
      CHECK (
        (old_status IN ('approved','rejected') OR old_status IS NULL) AND
        (new_status IN ('approved','rejected') OR new_status IS NULL)
      )
    """)

    # 5) Индексы для быстрых фильтров в админке
    c.execute("CREATE INDEX IF NOT EXISTS idx_comm_logs_action      ON commission_logs (action)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comm_logs_new_status  ON commission_logs (new_status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_app_status            ON applications (commission_status)")


def bootstrap_schema(conn=None) -> None:
    """
    Выполняет _apply_bootstrap_sql внутри переданного соединения (если дано),
    либо создаёт своё соединение и коммитит.
    """
    if conn is None:
        with get_conn() as _conn, _conn.cursor() as c:
            _apply_bootstrap_sql(c)
            _conn.commit()
    else:
        with conn.cursor() as c:
            _apply_bootstrap_sql(c)
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
