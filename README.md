# Kaffeepause

AI Harness for busy devs.

Takes a small feature or bug fix from a Linear issue and turns it into a reviewed pull request. Move an issue to **Agent Ready**, and three Claude agents take it from there: a tester writes failing tests, a coder makes them pass, and a security reviewer checks the change. Worker code, not a model, decides what runs next.

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

## How a run works

```
setup + baseline tests ── red ──▶ BASELINE_RED            (no tokens spent)
tester: failing tests ──▶ red check ── fails twice ──▶ RED_CHECK_FAILED
coder: implementation ──▶ dispute? ──▶ back to the tester
                      ──▶ green check (tests + lint + scope)
tester: gap review (once) ──▶ new tests must pass
security: scanners + review ──▶ blocking finding? ──▶ back to the coder
                                                   ──▶ ready-for-review PR
```

- Each return to the coder (a dispute, a failed green check, a failing gap test, a blocking finding) uses one of **2 feedback returns**. With none left, the PR opens as a **draft** that lists the open items.
- **Red check:** every new test fails with `<failure>` (not `<error>`), and its message contains the text the tester promised. Every baseline test still passes.
- **Green check:** all tests pass, every test the run relies on still runs, and lint exits 0.
- **Scope:** the tester writes only `tester_owned` files, and the coder never changes them. Nobody touches protected paths. Hooks deny out-of-scope writes, and the worker checks again on the commits themselves. A violation fails the run (`TESTER_FILES_CHANGED`, `PROTECTED_PATH`).
- **Budget:** one budget per run (`RUN_BUDGET_USD`, default $10). Each session gets what is left, and the run fails with `BUDGET_EXCEEDED` when it runs out. Turn caps: tester 60, coder 80, security 30.
- Issues need acceptance criteria: an "Acceptance criteria" list, or checkboxes. Bugs (label `Bug`) may use "Expected behaviour" instead. Without them the run fails with `NO_CRITERIA`.

Code map: `app/pipeline.py` (routing), `app/checks.py` and `app/junit.py` (checks), `app/agents/` (roles, hooks, verdict schemas, the in-container session, skills in `plugin/skills/`), `app/orchestrator.py` (clone, push, PR).

### Repository contract: `.kaffeepause.yml`

Each target repository needs this file on its base branch. JUnit XML is required, because exit codes can't tell a failed assertion from a crash.

```yaml
version: 1
stack: django                 # picks the security-review pack: django, express, rails, spring, go-net-http
setup: ["uv sync"]
test:                         # one command, or named layers
  unit: "uv run pytest -m unit --junitxml=$KP_JUNIT/unit.xml"
  integration: "uv run pytest -m integration --junitxml=$KP_JUNIT/integration.xml"
junit: "$KP_JUNIT/*.xml"
lint: ["uv run ruff check", "uv run ruff format --check"]
tester_owned: ["**/tests/**", "**/test_*.py", "**/conftest.py"]
protected: ["compose.yaml"]   # plus .kaffeepause.yml, .github/** and .claude/**, always
base_branch: main
timeout_seconds: 900          # per command
```

`environment` and `services` are accepted but not used yet. Until the injected runtime lands, commands run in the Kaffeepause image, so the repo's toolchain has to work there.

## Development

```sh
uv sync
uv run pre-commit install
uv run pytest
```
