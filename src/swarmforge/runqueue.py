"""Run queue: line up tasks and let SwarmForge work through them one after another.

* Items run strictly in order, one at a time, by a single background worker process
  (`swarmforge queue work`, started from the dashboard's Queue page).
* Chaining (default on): an item starts from the RESULT of the previous item on the same
  repository, so a series of improvements builds up without you merging in between:
    - local repository: the previous run delivered branch `swarmforge/task-…` into your repo;
      the next run clones your repo at that branch and delivers its own new branch on top;
    - GitHub repository: the previous run's branch was pushed (draft PR); the next run starts
      from it and its draft PR targets that branch (stacked PRs).
  Nothing is ever merged into your main branch; you review and merge the final branch (which
  contains every step) or each step separately.
* On failure the queue pauses (default) so later items don't build on a broken result; choose
  "continue" to run independent items anyway.
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Store

WORKER_KEY = "queue_worker_pid"
PAUSED_KEY = "queue_paused"
POLICY_KEY = "queue_on_failure"  # "stop" | "continue"


# ------------------------------------------------------------------ backlog files
@dataclass
class BacklogItem:
    title: str
    body: str


def parse_backlog(text: str) -> list[BacklogItem]:
    """Parse a Markdown backlog: sections that contain `**Title:** …` and `**Description:**`
    followed by the description (up to a `---` line or the next `## ` heading)."""
    items = []
    sections = re.split(r"(?m)^## ", text)
    for sec in sections:
        m = re.search(r"\*\*Title:\*\*\s*(.+)", sec)
        d = re.search(r"\*\*Description:\*\*\s*\n(.*)", sec, re.S)
        if not m or not d:
            continue
        body = re.split(r"(?m)^---\s*$", d.group(1))[0].strip()
        if body:
            items.append(BacklogItem(m.group(1).strip(), body))
    return items


# ------------------------------------------------------------------ worker
def worker_pid(store: Store) -> int | None:
    from .launcher import pid_alive
    v = store.kv_get(WORKER_KEY)
    if v and pid_alive(int(v)):
        return int(v)
    return None


def _same_repo(a: str, b: str) -> bool:
    return a.strip().rstrip("/\\").lower() == b.strip().rstrip("/\\").lower()


def resolve_base(store: Store, item: dict) -> tuple[str | None, str | None]:
    """(base_branch, note) for an item: the previous same-repo item's result when chaining."""
    if not item["chain"]:
        return None, None
    prev = [i for i in store.queue_items()
            if i["position"] < item["position"] and _same_repo(i["repo"], item["repo"])
            and i["status"] in ("done", "failed", "cancelled", "skipped")]
    if not prev:
        return None, None
    last = prev[-1]
    if last["status"] == "done" and last["result_branch"]:
        return last["result_branch"], f"continues from #{last['id']} ({last['result_branch']})"
    if last["status"] == "done":
        return None, (f"previous item #{last['id']} finished but its result is not reachable (no branch was "
                      "delivered or pushed), so this item starts from the default branch")
    return None, f"previous item #{last['id']} {last['status']}; this item starts from the default branch"


def result_branch_of(run: dict) -> str | None:
    """A branch later items can start from: delivered to the local repo, or pushed with a PR."""
    if run["state"] != "COMPLETED" or not run["integration_branch"]:
        return None
    reason = run["state_reason"] or ""
    if run["pr_url"] or "delivered to branch" in reason:
        return run["integration_branch"]
    return None


async def run_item(cfg: Config, store: Store, item: dict, *, echo=None, llm_transport=None,
                   sandbox=None) -> dict:
    from .approvals import dashboard_approver
    from .orchestrator import Orchestrator, new_run_id
    c = copy.deepcopy(cfg)
    if item["budget"]:
        c.limits.budget_usd = float(item["budget"])
    if item["max_cycles"] is not None:
        c.limits.max_repair_cycles = int(item["max_cycles"])
    if item.get("max_runtime_s"):
        c.limits.max_runtime_s = int(item["max_runtime_s"])
    base, note = resolve_base(store, item)
    run_id = new_run_id()
    issues = c.workdir / "issues"
    issues.mkdir(parents=True, exist_ok=True)
    issue_file = issues / f"{run_id}.md"
    issue_file.write_text(f"# {item['title']}\n\n{item['body']}\n", encoding="utf-8")
    store.queue_update(item["id"], status="running", run_id=run_id, started_at=time.time(),
                       base_branch=base, note=note)
    issue_url = item.get("issue_url")
    src = {"issue_url": issue_url} if issue_url else {"issue_file": str(issue_file), "repo": item["repo"]}
    if issue_url:
        _hook("on_run_start", cfg, item, echo=echo)
    def make(cc, seed, ctx=None, heal_of=None, attempt=None):
        orch = Orchestrator(cc, store, mode=item["mode"], echo=echo, llm_transport=llm_transport,
                            sandbox=sandbox, seed_run_id=seed, heal_of=heal_of, heal_attempt=attempt,
                            heal_context=ctx)
        if item["approval"] == "yes":
            orch.approve = lambda q: True
        else:
            orch.approve = dashboard_approver(store, lambda: orch.run_id, cc.approval.timeout_s)
        return orch
    await make(c, item.get("seed_run_id")).execute(**src, base_branch=base, run_id=run_id)

    async def launch(cc, seed, ctx, heal_of, attempt):
        rid = new_run_id()
        store.queue_update(item["id"], run_id=rid, note=(note + "; " if note else "")
                           + f"self-healing attempt {attempt} after {heal_of}")
        return await make(cc, seed, ctx, heal_of, attempt).execute(**src, base_branch=base, run_id=rid)
    from .postmortem import heal
    run_id = await heal(c, store, run_id, launch, attempts=item.get("heal_attempts"), echo=echo)
    run = store.get_run(run_id)
    status = {"COMPLETED": "done", "CANCELLED": "cancelled"}.get(run["state"], "failed")
    rb = result_branch_of(run)
    store.queue_update(item["id"], status=status, finished_at=time.time(), result_branch=rb,
                       note=(note + "; " if note else "")
                       + (f"after self-healing attempt {run['heal_attempt']}; " if run.get("heal_attempt") else "")
                       + f"{run['state']}: {(run['state_reason'] or '')[:300]}")
    if issue_url:
        _hook("on_run_end", cfg, store, item, run_id, echo=echo)
    return store.queue_item(item["id"])


def _hook(name: str, *args, echo=None) -> None:
    """Update the GitHub issue a watched task came from; never let that break the queue."""
    from . import watch
    try:
        getattr(watch, name)(*args)
    except Exception as e:  # noqa: BLE001
        if echo:
            echo(f"watch: could not update the GitHub issue ({name}): {type(e).__name__}: {e}")


async def work(cfg: Config, store: Store, *, echo=print, llm_transport=None, sandbox=None,
               max_items: int | None = None) -> int:
    """Process queued items in order until the queue is empty or paused. Returns items processed."""
    other = worker_pid(store)
    if other and other != os.getpid():
        if echo:
            echo(f"another queue worker is already running (pid {other})")
        return 0
    store.kv_set(WORKER_KEY, str(os.getpid()))
    done = 0
    try:
        # recover items left 'running' by a crashed worker
        for i in store.queue_items(include_finished=False):
            if i["status"] == "running":
                store.queue_update(i["id"], status="failed", finished_at=time.time(),
                                   note="worker stopped while this item was running")
        while True:
            if store.kv_get(PAUSED_KEY) == "1":
                if echo:
                    echo("queue paused")
                break
            pending = [i for i in store.queue_items(include_finished=False) if i["status"] == "queued"]
            if not pending or (max_items is not None and done >= max_items):
                break
            item = pending[0]
            if echo:
                echo(f"queue: starting #{item['id']} {item['title']}")
            try:
                result = await run_item(cfg, store, item, echo=echo, llm_transport=llm_transport, sandbox=sandbox)
            except Exception as e:  # never let one item kill the worker silently
                store.queue_update(item["id"], status="failed", finished_at=time.time(),
                                   note=f"worker error: {type(e).__name__}: {e}")
                result = store.queue_item(item["id"])
            done += 1
            if echo:
                echo(f"queue: #{item['id']} -> {result['status']}")
            if result["status"] != "done" and (store.kv_get(POLICY_KEY) or "stop") == "stop":
                store.kv_set(PAUSED_KEY, "1")
                if echo:
                    echo("queue paused because an item did not complete (policy: stop on failure)")
                break
    finally:
        if store.kv_get(WORKER_KEY) == str(os.getpid()):
            store.kv_set(WORKER_KEY, None)
    return done


def start_worker(cfg: Config, store: Store) -> int | None:
    """Start the background worker process (no-op if one is running). Returns its pid."""
    import subprocess
    import sys
    pid = worker_pid(store)
    if pid:
        return pid
    store.kv_set(PAUSED_KEY, "0")
    logs = cfg.workdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / "queue-worker.log", "a", encoding="utf-8")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                    "cwd": str(cfg.source_path.parent),
                    "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    p = subprocess.Popen([sys.executable, "-m", "swarmforge.cli", "-c", str(cfg.source_path), "queue", "work"],
                         **kwargs)
    log.close()
    return p.pid
