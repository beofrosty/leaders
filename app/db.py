import sqlite3, json, uuid
from datetime import datetime, timezone
from flask import current_app

def _db_path():
    return current_app.config["DB_PATH"]

def init_db():
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT,
            full_name TEXT,
            form_data TEXT,
            commission_comment TEXT,
            commission_status TEXT,
            test_score INTEGER,
            test_answers TEXT
        )''')
        conn.commit()

def ensure_user_columns():
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in c.fetchall()}
        to_add = []
        if 'password_hash' not in cols:
            to_add.append("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if 'is_verified' not in cols:
            to_add.append("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 1")
        if 'created_at' not in cols:
            to_add.append("ALTER TABLE users ADD COLUMN created_at TEXT")
        if 'role' not in cols:
            to_add.append("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        for sql in to_add:
            c.execute(sql)
        conn.commit()

def get_user_by_email(email: str):
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
        return c.fetchone()

def ensure_applications_table():
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            form_data TEXT,
            commission_comment TEXT,
            commission_status TEXT,
            test_score INTEGER,
            test_answers TEXT,
            created_at TEXT,
            UNIQUE(user_id) -- одна заявка на пользователя (если нужно несколько — убери UNIQUE)
        )
        """)
        # индексы для админки/поиска
        c.execute("CREATE INDEX IF NOT EXISTS idx_app_user ON applications(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_app_created ON applications(created_at)")
        conn.commit()
    ensure_applications_columns()

def ensure_applications_columns():
    """Добавляет недостающие колонки в applications."""
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(applications)")
        cols = {row[1] for row in c.fetchall()}

        to_add = []
        if 'test_link' not in cols:
            to_add.append("ALTER TABLE applications ADD COLUMN test_link TEXT")

        # если хочешь — можно хранить и флаг прохождения
        # if 'test_passed' not in cols:
        #     to_add.append("ALTER TABLE applications ADD COLUMN test_passed INTEGER DEFAULT 0")

        for sql in to_add:
            c.execute(sql)
        conn.commit()


def migrate_users_formdata_to_applications():
    """
    Разовая миграция старых заявок из users.form_data в applications.
    Запусти один раз и можешь удалить/закомментировать.
    """
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT id AS user_id, form_data, commission_comment, commission_status,
                 test_score, test_answers, created_at
          FROM users
          WHERE form_data IS NOT NULL
        """)
        rows = c.fetchall()
        for r in rows:
            # пропускаем, если уже есть заявка на этого пользователя
            c.execute("SELECT 1 FROM applications WHERE user_id=?", (r["user_id"],))
            if c.fetchone():
                continue
            c.execute("""
              INSERT OR IGNORE INTO applications
              (id, user_id, form_data, commission_comment, commission_status,
               test_score, test_answers, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(uuid.uuid4()),
                r["user_id"],
                r["form_data"],
                r["commission_comment"],
                r["commission_status"],
                r["test_score"],
                r["test_answers"],
                r["created_at"]
            ))
        conn.commit()

# вверху файла уже есть: sqlite3, json, uuid, current_app

def ensure_tests_tables():
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            duration_minutes INTEGER,
            questions TEXT,               -- JSON: [{q, options:[...], correct_index}]
            created_at TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS test_attempts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            test_id TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            score INTEGER,
            answers TEXT                  -- JSON: [0,2,1,...] (индексы выбранных вариантов)
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_user ON test_attempts(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_attempt_test ON test_attempts(test_id)")
        conn.commit()

def ensure_password_resets_table():
    """Создаёт таблицу одноразовых ссылок сброса пароля."""
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,      -- ISO-UTC, как в маршруте
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_user ON password_resets(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pr_expires ON password_resets(expires_at)")
        conn.commit()
def prune_password_resets():
    """Удаляет использованные и просроченные записи."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM password_resets WHERE used=1 OR expires_at < ?", (now_iso,))
        conn.commit()
def ensure_users_index_email():
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        conn.commit()
def user_has_application(user_id: str) -> bool:
    with sqlite3.connect(_db_path()) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM applications WHERE user_id=? LIMIT 1", (user_id,))
        return c.fetchone() is not None

def get_user_applications(user_id: str):
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM applications WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        return c.fetchall()

