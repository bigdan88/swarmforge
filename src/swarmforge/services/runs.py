"""Run actions shared by CLI and dashboard; no UI dependencies."""
from __future__ import annotations

from ..db import Store


def request_cancel(store: Store, run_id: str) -> bool:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"run not found: {run_id}")
    if run["state"] in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}:
        return False
    store.request_cancel(run_id)
    return True
