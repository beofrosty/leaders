# entrypoint.sh (в корне проекта или /app/entrypoint.sh внутри образа)
#!/usr/bin/env bash
set -e

# (опционально) дождаться БД
# until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" -U "${DB_USER:-postgres}"; do
#   echo "Waiting for Postgres..."; sleep 1
# done

# 1) миграции один раз
python -m app.migrations

# 2) веб
exec gunicorn run:app -b 0.0.0.0:8000 --workers "${WEB_CONCURRENCY:-4}" --threads 4
