# create_provisioner.py
import os, uuid, argparse, sys, json
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash

def parse_args():
    p = argparse.ArgumentParser(description="Create/Update provisioner account")
    p.add_argument("-e", "--email", required=True, help="E-mail (login)")
    p.add_argument("-p", "--password", required=True, help="Temporary password (will be hashed)")
    p.add_argument("--name", default="Provisioning Account", help="Full name")
    p.add_argument("--access-until", default=None, help="ISO datetime (e.g. 2025-12-31T23:59:59Z)")
    return p.parse_args()

DDL_USERS = [
    # добавляем недостающие колонки (идемпотентно)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS access_expires_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS inn TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT",
]

def ensure_users_columns(c):
    for stmt in DDL_USERS:
        c.execute(stmt)

def main():
    dsn = os.getenv("DB_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: set DB_DSN or DATABASE_URL env var", file=sys.stderr)
        sys.exit(1)

    args = parse_args()
    email = args.email.strip().lower()
    full_name = args.name.strip()
    pwd_hash = generate_password_hash(args.password)

    expires = None
    if args.access_until:
        s = args.access_until.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        expires = dt

    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as c:
            # самовосстановление схемы
            ensure_users_columns(c)

            # есть ли такой пользователь?
            c.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
            row = c.fetchone()

            if row:
                if expires:
                    c.execute("""
                        UPDATE users
                           SET password_hash=%s,
                               role='provisioner',
                               must_change_password=TRUE,
                               is_active=TRUE,
                               access_expires_at=%s,
                               full_name=COALESCE(NULLIF(%s,''), full_name)
                         WHERE id=%s
                    """, (pwd_hash, expires, full_name, row["id"]))
                else:
                    c.execute("""
                        UPDATE users
                           SET password_hash=%s,
                               role='provisioner',
                               must_change_password=TRUE,
                               is_active=TRUE,
                               full_name=COALESCE(NULLIF(%s,''), full_name)
                         WHERE id=%s
                    """, (pwd_hash, full_name, row["id"]))
                user_id = row["id"]; action = "updated"
            else:
                user_id = str(uuid.uuid4())
                if expires:
                    c.execute("""
                        INSERT INTO users (
                          id, email, full_name, password_hash, is_verified, created_at,
                          role, must_change_password, is_active, access_expires_at
                        ) VALUES (%s,%s,%s,%s, TRUE, NOW(),
                                 'provisioner', TRUE, TRUE, %s)
                    """, (user_id, email, full_name, pwd_hash, expires))
                else:
                    c.execute("""
                        INSERT INTO users (
                          id, email, full_name, password_hash, is_verified, created_at,
                          role, must_change_password, is_active
                        ) VALUES (%s,%s,%s,%s, TRUE, NOW(),
                                 'provisioner', TRUE, TRUE)
                    """, (user_id, email, full_name, pwd_hash))
                action = "created"

            conn.commit()

        print(f"Provisioner {action}:")
        print(f"  id: {user_id}")
        print(f"  email: {email}")
        print(f"  role: provisioner")
        print(f"  must_change_password: TRUE")
        if expires:
            print(f"  access_expires_at: {expires.isoformat()}")

    except Exception as e:
        # покажем полную причину
        print("FAILED:", repr(e), file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
