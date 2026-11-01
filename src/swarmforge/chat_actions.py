"""What the chat's buttons do: launch suggested tasks, retry or dismiss failed tasks, save lessons.

Everything goes through the normal paths (the queue, the launcher, the knowledge vault), so a task
started from the chat behaves exactly like one added on the New task page.
"""
from __future__ import annotations

import time

from .config import Config
from .db import Store

GUIDANCE_HEADER = "## Guidance for this attempt"


def default_repo(store: Store) -> str | None:
    try:
        from .selfupdate import own_repo
        own = own_repo()
        if own:
            return own
    except Exception:  # noqa: BLE001
        pass
    recent = store.recent_repos()
    return recent[0] if recent else None


def _source(repo: str) -> str:
    from .repos import normalize_repo
    return normalize_repo(repo).source


def task_body(task: dict) -> str:
    body = task["description"].strip()
    if task.get("why"):
        body += f"\n\n_Why (from the improvement chat): {task['why'].strip()}_"
    return body


def launch_task(cfg: Config, store: Store, task: dict, *, how: str = "queue", repo: str | None = None,
                queue: str = "main", approval: str = "yes", budget: float | None = None,
                time_limit_h: float | None = None, start_run=None, start_worker=None) -> str:
    """how: "queue" (add to a queue and make sure the worker runs) or "now" (start immediately)."""
    repo = (repo or task.get("repo") or default_repo(store) or "").strip()
    if not repo:
        raise ValueError("which repository? none is known yet")
    src = _source(repo)
    title, body = task["title"].strip(), task_body(task)
    runtime = int(time_limit_h * 3600) if time_limit_h else None
    if how == "now":
        from .launcher import start_run as _start_run
        run_id, _ = (start_run or _start_run)(cfg, repo=src, issue_title=title, issue_body=body, budget=budget,
                                         approval=approval, max_runtime_s=runtime)
        store.remember_repo(src)
        return f"Started “{title}” (run {run_id})."
    from . import runqueue as rq
    iid = store.queue_add(title, body, src, chain=0, approval=approval, budget=budget, max_runtime_s=runtime,
                          queue=queue or "main")
    store.remember_repo(src)
    if not rq.lane_paused(store, queue or "main") and store.kv_get(rq.PAUSED_KEY) != "1":
        (start_worker or rq.start_worker)(cfg, store)
    return f"Queued “{title}” as task #{iid} in {queue or 'main'}."


def guidance(run: dict, fail: dict | None, instructions: str) -> str:
    why = (fail or {}).get("root_cause") or run.get("state_reason") or ""
    return (f"\n\n{GUIDANCE_HEADER}\nA previous attempt (run {run['id']}) did not finish"
            + (f": {why.strip()[:600]}" if why else ".") + f"\n\nWhat to do differently:\n{instructions.strip()}\n")


def retry_failure(cfg: Config, store: Store, run_id: str, instructions: str, *, time_limit_h: float | None = None,
                  budget: float | None = None, max_cycles: int | None = None, continue_code: bool = False,
                  start_run=None, start_worker=None) -> str:
    """Try a failed task again with Dan's guidance (and optionally more time/budget, or from its code)."""
    from . import runqueue as rq
    from .postmortem import resumable
    from .report import request_text
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"run {run_id} not found")
    if not instructions.strip():
        raise ValueError("write what the next attempt should do differently, or pick a suggestion")
    fail = store.failure(run["id"])
    seed = run["id"] if continue_code and resumable(store, run["id"]) else None
    runtime = int(time_limit_h * 3600) if time_limit_h else None
    item = next((q for q in store.queue_items() if q.get("run_id") == run["id"]), None)
    if item:
        base_body = item["body"].split("\n\n" + GUIDANCE_HEADER)[0]
        kw = {k: v for k, v in dict(mode=item["mode"], budget=budget or item["budget"],
                                    max_cycles=max_cycles if max_cycles is not None else item["max_cycles"],
                                    chain=item["chain"], approval=item["approval"],
                                    max_runtime_s=runtime or item["max_runtime_s"],
                                    heal_attempts=item.get("heal_attempts"), seed_run_id=seed).items() if v is not None}
        new_id, created = rq.requeue(store, {**item, "body": base_body + guidance(run, fail, instructions)}, **kw)
        if store.kv_get(rq.PAUSED_KEY) != "1":
            (start_worker or rq.start_worker)(cfg, store)
        msg = f"Queued again as task #{new_id}" + (" (continuing from its code)" if seed else "") + "."
    else:
        from .launcher import start_run as _start_run
        body = (run.get("issue_body") or request_text(run) or run.get("issue_title") or "").split(
            "\n\n" + GUIDANCE_HEADER)[0]
        new_run, _ = (start_run or _start_run)(cfg, repo=run.get("repo"), issue_title=run.get("issue_title") or "Retry",
                                          issue_body=body + guidance(run, fail, instructions), budget=budget,
                                          max_cycles=max_cycles, max_runtime_s=runtime, seed_run_id=seed)
        msg = f"Started again as run {new_run}" + (" (continuing from its code)" if seed else "") + "."
    if fail:
        store.failure_update(run["id"], outcome=f"retried from chat at {time.strftime('%H:%M')}: "
                                                f"{instructions.strip()[:120]}")
    store.record_intervention(run["id"], "chat_retry", instructions.strip()[:500])
    return msg


def dismiss_failure(store: Store, run_id: str) -> str:
    if store.failure(run_id):
        store.failure_update(run_id, outcome="dismissed from chat (not retried)")
    store.record_intervention(run_id, "chat_dismiss", "dismissed from the chat")
    return "Dismissed: it won't be suggested again."


def save_lesson(cfg: Config, title: str, text: str) -> str:
    from .knowledge import Vault
    note = Vault(cfg.vault_path).write(title.strip()[:120] or "Lesson from chat", text.strip(), type="user-note",
                                       tags=["chat"], source="user")
    return f"Saved to the knowledge base as “{note.title}”."
