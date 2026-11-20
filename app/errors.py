"""The closed list of run error codes (PRD section 4), and the exception that carries one."""

from enum import StrEnum


class ErrorCode(StrEnum):
    NO_CRITERIA = "NO_CRITERIA"
    CONFIG_INVALID = "CONFIG_INVALID"
    BASELINE_RED = "BASELINE_RED"
    RED_CHECK_FAILED = "RED_CHECK_FAILED"
    TESTER_FILES_CHANGED = "TESTER_FILES_CHANGED"
    PROTECTED_PATH = "PROTECTED_PATH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    BAD_VERDICT = "BAD_VERDICT"
    CANNOT_REPRODUCE = "CANNOT_REPRODUCE"
    NO_WORKER = "NO_WORKER"
    LEASE_LOST = "LEASE_LOST"
    PUSH_FAILED = "PUSH_FAILED"
    # Not in the PRD's list: the change or PR text contains a credential the
    # run container held. Checked by exact value before anything is pushed.
    SECRET_IN_CHANGE = "SECRET_IN_CHANGE"
    # Not in the PRD's list: Jev judged the issue's criteria untestable or the
    # change too large (app.intake). Checked before any tokens are spent.
    ISSUE_NOT_READY = "ISSUE_NOT_READY"
    UPSTREAM_API = "UPSTREAM_API"
    UNKNOWN = "UNKNOWN"


class RunFailed(RuntimeError):
    """Ends a run with one error code. The message must never contain a secret."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else str(code))
