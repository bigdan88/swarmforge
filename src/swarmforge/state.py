"""Deterministic workflow state machine.

LLMs never set states. Only orchestrator code calls `StateMachine.transition`, which
rejects any transition not listed in TRANSITIONS.
"""
from __future__ import annotations

from enum import StrEnum


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


TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.BLOCKED,
            RunState.CANCELLED, RunState.BUDGET_EXCEEDED}

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
            {_S.FAILED, _S.BLOCKED, _S.CANCELLED, _S.BUDGET_EXCEEDED})


class InvalidTransition(RuntimeError):
    pass


class RunAborted(Exception):
    """Raised to stop a run and move it to a terminal state (budget, cancel, block)."""

    def __init__(self, state: "RunState", reason: str):
        super().__init__(reason)
        self.state = state
        self.reason = reason


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"   # agent finished and its changes were committed
    INTEGRATED = "INTEGRATED"  # merged into the integration branch
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"        # a dependency failed
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
        fields = {"state": new.value, "state_reason": reason or None}
        if new in TERMINAL:
            import time
            fields["finished_at"] = time.time()
        self.store.update_run(self.run_id, **fields)
        self.store.event(self.run_id, "state", f"{old} -> {new}" + (f": {reason}" if reason else ""),
                         data={"from": old.value, "to": new.value, "reason": reason})

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL
