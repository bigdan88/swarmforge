"""Human approval channels for gated actions (pushing a branch / opening a draft PR)."""
from __future__ import annotations

import sys
import time
from typing import Callable


def prompt_approver(question: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        print(f"{question} -> no interactive terminal; declining. Use --approval dashboard or --yes.")
        return False
    return input(f"\n>>> {question} [y/N] ").strip().lower() in ("y", "yes")


def dashboard_approver(store, run_id_getter: Callable[[], str], timeout_s: int,
                       poll_s: float = 2.0) -> Callable[[str], bool]:
    """Create an approval request in the DB and wait until someone decides in the dashboard."""
    def approve(question: str) -> bool:
        run_id = run_id_getter()
        aid = store.request_approval(run_id, question)
        store.event(run_id, "approval", f"waiting for approval in the dashboard: {question}", level="warn")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            row = store.approval(aid)
            if row and row["status"] != "pending":
                return row["status"] == "approved"
            if store.cancel_requested(run_id):
                break
            time.sleep(poll_s)
        store.decide_approval(aid, False, by="timeout")
        return False
    return approve
