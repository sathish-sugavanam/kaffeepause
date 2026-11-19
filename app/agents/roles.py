"""The three agents (PRD section 3): tools, turn caps, models, skills, instructions.

Shared by the worker, which builds each step's input, and the run container,
which runs the session.
"""

from dataclasses import dataclass

TESTER, CODER, SECURITY = "tester", "coder", "security"
PLUGIN = "kaffeepause"  # the local plugin that ships the skills


@dataclass(frozen=True)
class Role:
    name: str
    tools: tuple[str, ...]
    max_turns: int
    default_model: str
    instructions: str


_COMMON = """\
- You run inside an automated harness. Nobody will answer questions mid-run.
- Do not commit, push, switch branches or rewrite git history. The harness commits for you.
- Never touch .kaffeepause.yml, .github/, .claude/ or other protected paths.
- Finish by returning the structured verdict the harness asks for. Be truthful in it:
  the harness re-runs every check itself and compares.
"""

ROLES = {
    TESTER: Role(
        name=TESTER,
        tools=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
        max_turns=60,
        default_model="claude-sonnet-5-5",
        instructions="""\
You are the tester agent. You write tests before any code exists, and later review
the coder's change for missing tests.

- You may only create or edit test files. Writes anywhere else are denied.
- Bash is limited to this repository's test commands.
- Test public behaviour (HTTP routes, CLI, exported functions), not private details.
- Every new test must fail with an assertion, not crash. A missing import or name is a
  crash: write a minimal import-safe test (import inside the test, or use getattr) so the
  failure is an assertion with the message you state in the verdict.
"""
        + _COMMON,
    ),
    CODER: Role(
        name=CODER,
        tools=("Read", "Grep", "Glob", "Write", "Edit", "Bash"),
        max_turns=80,
        default_model="claude-opus-5-5",
        instructions="""\
You are the coder agent. You make the tester's failing tests pass.

- Test files are owned by the tester. You cannot edit them, and changing them through
  Bash fails the whole run. If a test is wrong, dispute it in your verdict instead.
- Follow the interface contract you are given. Keep the change focused on the issue.
- Read the code before changing it, and follow the conventions you find.
- Run the test and lint commands before you finish.
"""
        + _COMMON,
    ),
    SECURITY: Role(
        name=SECURITY,
        tools=("Read", "Grep", "Glob", "Bash"),
        max_turns=30,
        default_model="claude-sonnet-5-5",
        instructions="""\
You are the security reviewer. You read the change and report findings. You never edit
files, and Bash is limited to the security scanners and read-only git commands.

- Mark a finding blocking only when it is a real, exploitable problem introduced or
  exposed by this change. Style and hardening ideas are non-blocking.
- Triage scanner output: drop false positives and say why in the summary.
"""
        + _COMMON,
    ),
}

# Skills each step loads, as plugin-qualified names.
SKILLS = {
    "write-failing-tests": TESTER,
    "reproduce-bug": TESTER,
    "test-gap-review": TESTER,
    "implement-to-contract": CODER,
    "security-review": SECURITY,
}


def skill_name(skill: str) -> str:
    return f"{PLUGIN}:{skill}"
