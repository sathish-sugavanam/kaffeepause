# Kaffeepause

AI Harness for busy devs.

Takes a small feature or bug fix from a Linear issue and create into a pull request. Move an issue to **Agent Ready**, and a Claude agent makes the change. 

## Setup

1. Copy `.env.example` to `.env` and fill it in.

2. Create the database, then apply the migrations in order:

   ```sh
   psql -U postgres \
     -c "CREATE ROLE agents LOGIN PASSWORD 'CHANGE_ME'" \
     -c "CREATE DATABASE agents OWNER agents"

   for f in app/migrations/*.sql; do
     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
   done
   ```

3. Build the image. The worker starts run containers from this tag:

   ```sh
   docker build -t kaffeepause .
   ```

4. Run the three services with `.env`:

   - **webhook:** `.venv/bin/python -m app.webhook` on port 5001. Point the Linear webhook at `/webhooks/linear`.
   - **worker:** `.venv/bin/celery -A app.celery worker --concurrency=1`. Mount `/var/run/docker.sock`, and mount a volume named `agent_runs` at `/runs`.
   - **beat:** `.venv/bin/celery -A app.celery beat`. Run only one.

## Development

```sh
uv sync
uv run pre-commit install
uv run pytest
```
