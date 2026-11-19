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
   - **worker:** `.venv/bin/celery -A app.celery worker --concurrency=1`. Mount `/var/run/docker.sock`. The worker shares no folder with run containers: files go in and out through the Docker API.
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
- **Trust boundary:** the run container never holds the GitHub token, and it shares no folder with the worker. Before pushing, the worker refuses a change whose diff or commit messages contain a credential the container held (`SECRET_IN_CHANGE`), and it redacts those values and defuses `@mentions` in agent-written PR text.
- **Scope:** the tester writes only `tester_owned` files, and the coder never changes them. Nobody touches protected paths. Hooks deny out-of-scope writes, and the worker checks again on the commits themselves. A violation fails the run (`TESTER_FILES_CHANGED`, `PROTECTED_PATH`).
- **Budget:** one budget per run (`RUN_BUDGET_USD`, default $10). Each session gets what is left, and the run fails with `BUDGET_EXCEEDED` when it runs out. Turn caps: tester 60, coder 80, security 30.
- Issues need acceptance criteria: an "Acceptance criteria" list, or checkboxes. Bugs (label `Bug`) may use "Expected behaviour" instead. Without them the run fails with `NO_CRITERIA`.

Code map: `app/pipeline.py` (routing), `app/checks.py` and `app/junit.py` (checks), `app/scanners.py`, `app/agents/` (roles, hooks, the step contract, verdict schemas, the in-container session, skills in `plugin/skills/`), `app/container.py` (the run container and its file API), `app/git.py`, `app/report.py` (PR body), `app/orchestrator.py` (clone, push, PR).

### Repository contract: `.kaffeepause.yml`

The file lives in each **target repository** (the one the agents open pull requests against), not in this repo. Put it at the repository root, on the base branch.

- The worker always reads it from the base branch, never from the agent's branch, so an agent can't change the rules of its own run.
- The file is always protected: an agent that edits it fails the run with `PROTECTED_PATH`.
- If the file is missing or invalid, the run fails with `CONFIG_INVALID`, and the error names the field.

```yaml
version: 1                     # required, must be 1
stack: django                  # picks the security-review pack
setup: ["uv sync"]             # run once, before the baseline tests
test:                          # required: one command, or named layers
  unit: "uv run pytest -m unit --junitxml=$KP_JUNIT/unit.xml"
  integration: "uv run pytest -m integration --junitxml=$KP_JUNIT/integration.xml"
junit: "$KP_JUNIT/*.xml"       # required: where the test commands write JUnit XML
lint: ["uv run ruff check", "uv run ruff format --check"]
tester_owned: ["**/tests/**", "**/test_*.py", "**/conftest.py"]
protected: ["compose.yaml"]    # added to the always-protected paths
base_branch: main
timeout_seconds: 900           # per command
```

| Field | Required | What it does | Default |
| --- | --- | --- | --- |
| `version` | yes | Must be `1`. | — |
| `test` | yes | A test command, or a map of layer name to command. Used for the baseline, red, green and gap-review checks. Layers are reported separately. | — |
| `junit` | yes | A glob for the JUnit XML the test commands write. It must start with `$KP_JUNIT/`; the worker sets `KP_JUNIT` to a fresh folder for every run. JUnit is required because exit codes can't tell a failed assertion from a crash. | — |
| `stack` | no | Picks the security-review pack: `django`, `express`, `rails`, `spring` or `go-net-http`. | general checklist only |
| `setup` | no | Commands run before the baseline tests. A failure ends the run with `BASELINE_RED`. | none |
| `lint` | no | Commands that must exit 0 for the green check to pass. | none |
| `tester_owned` | no | Files only the tester may write. The coder may never change them. | `**/test/**`, `**/tests/**`, `**/__tests__/**`, `**/*_test.*`, `**/*.test.*`, `**/*_spec.*`, `**/test_*.*` |
| `protected` | no | Paths no agent may touch. `.kaffeepause.yml`, `.github/**` and `.claude/**` are always protected. | just those three |
| `base_branch` | no | The branch the config is read from and pull requests target. | `main` |
| `timeout_seconds` | no | Time limit for each command. Hitting it counts as a failure. | `900` (15 minutes) |
| `environment`, `services` | no | Accepted but not used yet (see below). | — |

Any other key is rejected.

Until the injected runtime lands (PRD build step 3), commands run in the Kaffeepause image (Python 3.14 with `uv`), not the repo's own image, and `environment` and `services` are ignored. A Python repo works as is. A Node, Go or other repo's commands won't find their toolchain yet.

## Development

```sh
uv sync
uv run pre-commit install
uv run pytest
```
