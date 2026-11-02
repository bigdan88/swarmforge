"""Deterministic workflow state machine.

Repair-task generation supports optional scope filtering: findings whose target
file lies outside the original plan's task file set are excluded from repair
tasks (see `out_of_scope_findings`).

LLMs never set states. Only orchestrator code calls `StateMachine.transition`, which
rejects any transition not listed in TRANSITIONS.
"""

from __future__ import annotations

from enum import StrEnum

from .plan import ID_RE, Task

REPAIR_TASK_PREFIX = "R"
DEFAULT_BLOCKING_SEVERITIES = ("critical", "major")


def normalize_scope_path(p: object) -> str:
    """Normalize a path for scope comparison. Deterministic, no I/O.

    Backslashes become '/', leading './' segments and leading '/' are stripped.
    Returns '' for None/empty/whitespace input.
    """
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _blocking_findings(findings: list[dict], severities: set[str] | None) -> list[dict]:
    """Filter to blocking findings, deduped on (severity, file, title).

    Keeps the first occurrence and preserves input order.
    """
    blocking = (
        severities if severities is not None else set(DEFAULT_BLOCKING_SEVERITIES)
    )
    seen: set[tuple[str, str, str]] = set()
    kept: list[dict] = []
    for f in findings:
        sev = (f.get("severity") or "").lower()
        if sev not in blocking:
            continue
        key = (sev, str(f.get("file")), str(f.get("title")))
        if key in seen:
            continue
        seen.add(key)
        kept.append(f)
    return kept


def _partition_findings(
    findings: list[dict],
    severities: set[str] | None,
    scope_files: set[str] | tuple[str, ...] | None,
) -> tuple[list[dict], list[dict]]:
    """Single-pass partition of blocking findings into (in_scope, out_of_scope).

    Applies the `_blocking_findings` severity filter first, then splits the
    kept list using `normalize_scope_path` prefix rules: an empty scope set or
    an empty/missing finding file means in-scope; a finding file equal to or
    under a scope path is in-scope; anything else is out-of-scope.
    """
    kept = _blocking_findings(findings, severities)
    if scope_files is None:
        return kept, []
    scope = {normalize_scope_path(f) for f in scope_files if str(f or "").strip()}
    if not scope:
        return kept, []
    in_scope: list[dict] = []
    out_of_scope: list[dict] = []
    for f in kept:
        fp = normalize_scope_path(f.get("file"))
        if not fp or (
            fp in scope
            or any(fp.startswith(s + "/") for s in scope)
            or any(s.endswith("/") and fp.startswith(s) for s in scope)
        ):
            in_scope.append(f)
        else:
            out_of_scope.append(f)
    return in_scope, out_of_scope


def out_of_scope_findings(
    findings: list[dict],
    scope_files: set[str] | tuple[str, ...] | None,
    severities: set[str] | None = None,
) -> list[dict]:
    """Return blocking findings whose target file is outside the scope set.

    Findings without a file are considered in scope (never returned). An empty
    or None scope disables filtering entirely (returns []).
    """
    return _partition_findings(findings, severities, scope_files)[1]


def repair_tasks_from_findings(
    findings: list[dict],
    roles: list[str],
    cycle: int,
    severities: set[str] | None = None,
    scope_files: set[str] | None = None,
) -> list[Task]:
    """Convert blocking reviewer findings into mandatory developer repair tasks.

    Deterministic, LLM-free: each unique blocking finding becomes one Task with
    id R<cycle>F<n>, no dependencies, and acceptance criteria quoting the
    reviewer's exact text so the finding cannot be skipped.

    When `scope_files` is given (and non-empty), findings whose file lies outside
    that scope are excluded from repair tasks.
    """
    kept, _ = _partition_findings(findings, severities, scope_files)
    if not roles:
        if kept:
            raise ValueError("roles must not be empty when blocking findings exist")
        return []

    tasks: list[Task] = []
    for i, f in enumerate(kept, start=1):
        tid = f"{REPAIR_TASK_PREFIX}{cycle}F{i}"
        assert ID_RE.match(tid), f"repair task id '{tid}' must match {ID_RE.pattern}"
        sev = (f.get("severity") or "").lower()
        title = str(f.get("title") or "")[:120]
        file_ = f.get("file")
        line = f.get("line")
        loc = (
            f"File: {file_}:{line}"
            if file_ is not None and line is not None
            else (f"File: {file_}" if file_ is not None else "File: unknown")
        )
        description = str(f.get("description") or "")
        rec = f.get("recommendation")
        desc = (
            f"MANDATORY repair for reviewer finding (cycle {cycle}). "
            "This task cannot be skipped.\n\n"
            f"{loc}\n\n"
            f"Reviewer finding (severity: {sev}):\n{description}\n"
        )
        if rec:
            desc += f"\nRecommendation: {rec}\n"
        criterion = (
            f'Reviewer said: "{description}" — resolve this finding '
            f"({sev}): {f.get('title')} ({file_}:{line})"
        )
        tasks.append(
            Task(
                id=tid,
                title=title,
                description=desc,
                role=roles[(i - 1) % len(roles)],
                depends_on=[],
                acceptance_criteria=[criterion],
                expected_files=[file_] if file_ is not None else [],
            )
        )
    return tasks


class RunState(StrEnum):
    ISSUE_RECEIVED = "ISSUE_RECEIVED"
    REPOSITORY_INSPECTION = "REPOSITORY_INSPECTION"
    PLANNING = "PLANNING"
    TASK_VALIDATION = "TASK_VALIDATION"
    TASK_EXECUTION = "TASK_EXECUTION"
    INTEGRATION = "INTEGRATION"
    TESTING = "TESTING"
    CODE_REVIEW = "CODE_REVIEW"
    FIX_REQUIRED = "FIX_REQUIRED"
    APPROVED = "APPROVED"
    PULL_REQUEST_CREATION = "PULL_REQUEST_CREATION"
    COMPLETED = "COMPLETED"
    # terminal / exceptional
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


TERMINAL = {
    RunState.COMPLETED,
    RunState.FAILED,
    RunState.BLOCKED,
    RunState.CANCELLED,
    RunState.BUDGET_EXCEEDED,
}

_S = RunState
TRANSITIONS: dict[RunState, set[RunState]] = {
    _S.ISSUE_RECEIVED: {_S.REPOSITORY_INSPECTION},
    _S.REPOSITORY_INSPECTION: {_S.PLANNING},
    _S.PLANNING: {_S.TASK_VALIDATION},
    _S.TASK_VALIDATION: {_S.TASK_EXECUTION, _S.PLANNING},
    _S.TASK_EXECUTION: {_S.INTEGRATION},
    _S.INTEGRATION: {_S.TESTING},
    _S.TESTING: {_S.CODE_REVIEW},
    _S.CODE_REVIEW: {_S.FIX_REQUIRED, _S.APPROVED},
    _S.FIX_REQUIRED: {_S.TASK_VALIDATION},
    # APPROVED -> COMPLETED is allowed when PR creation is disabled/unavailable
    _S.APPROVED: {_S.PULL_REQUEST_CREATION, _S.COMPLETED},
    _S.PULL_REQUEST_CREATION: {_S.COMPLETED},
}
# Any non-terminal state can drop into an exceptional terminal state.
for _st in list(RunState):
    if _st not in TERMINAL:
        TRANSITIONS.setdefault(_st, set()).update(
            {_S.FAILED, _S.BLOCKED, _S.CANCELLED, _S.BUDGET_EXCEEDED}
        )


class InvalidTransition(RuntimeError):
    pass


class RunAborted(Exception):
    """Raised to stop a run and move it to a terminal state (budget, cancel, block)."""

    def __init__(self, state: RunState, reason: str):
        super().__init__(reason)
        self.state = state
        self.reason = reason


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"  # agent finished and its changes were committed
    INTEGRATED = "INTEGRATED"  # merged into the integration branch
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"  # a dependency failed
    CANCELLED = "CANCELLED"


class StateMachine:
    def __init__(self, store, run_id: str, initial: RunState = RunState.ISSUE_RECEIVED):
        self.store = store
        self.run_id = run_id
        self.state = initial

    def transition(self, new: RunState, reason: str = "") -> None:
        if new not in TRANSITIONS.get(self.state, set()):
            raise InvalidTransition(f"{self.state} -> {new} is not allowed")
        old, self.state = self.state, new
        fields: dict[str, object] = {"state": new.value, "state_reason": reason or None}
        if new in TERMINAL:
            import time

            fields["finished_at"] = time.time()
        self.store.update_run(self.run_id, **fields)
        self.store.event(
            self.run_id,
            "state",
            f"{old} -> {new}" + (f": {reason}" if reason else ""),
            data={"from": old.value, "to": new.value, "reason": reason},
        )

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL
