# Docker quick start

## 1) Prereqs
- Docker and Docker Compose installed

## 2) Files created
- `Dockerfile` – builds the Flask app image (`forum.run:app` served by gunicorn on :8000)
- `docker-compose.yml` – brings up the app + Postgres + MailHog
- `.dockerignore` – trims build context
- `.env.example` – copy to `.env` and adjust values

## 3) First run
```bash
cp .env.example .env
docker compose up --build
```
- App: http://localhost:8000
- MailHog UI: http://localhost:8025 (captures outbound email)

## 4) Environment notes
- The app reads `DB_DSN` (or `DATABASE_URL`) as Postgres connection string.
- `run.py` uses `python-dotenv` to load `.env` locally; the Docker image installs it explicitly.

## 5) Common actions
- Rebuild after changing requirements:
  ```bash
  docker compose build --no-cache web
  ```
- Run a one-off shell inside the app container:
  ```bash
  docker compose run --rm web bash
  ```
- Check Postgres service logs:
  ```bash
  docker compose logs -f db
  ```
