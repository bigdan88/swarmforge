"""Run queues: line up tasks and let SwarmForge work through them.

* There can be several queues ("piles"), e.g. one per project. Each queue runs its items strictly in
  order, one at a time; different queues run side by side (up to `limits.max_parallel_queues`), all
  in one background worker process (`swarmforge queue work`, started from the dashboard).
* Chaining (default on): an item starts from the RESULT of the previous item on the same
  repository, so a series of improvements builds up without you merging in between:
    - local repository: the previous run delivered branch `swarmforge/task-…` into your repo;
      the next run clones your repo at that branch and delivers its own new branch on top;
    - GitHub repository: the previous run's branch was pushed (draft PR); the next run starts
      from it and its draft PR targets that branch (stacked PRs).
  Nothing is ever merged into your main branch; you review and merge the final branch (which
  contains every step) or each step separately.
* On failure that queue pauses (default) so later items don't build on a broken result; the other
  queues keep going. Choose "continue" to run independent items anyway.
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
LANE_PAUSED = "queue_paused:{}"   # per queue ("pile")
DEFAULT_QUEUE = "main"


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


RESTARTED = "restarted as task #"


def requeue(store: Store, item: dict, **kw) -> tuple[int, bool]:
    """Queue a finished/failed item again at the front of the waiting line. Returns (item id, created).
    If a copy is already waiting or running, that copy is returned instead of adding another one."""
    for q in store.queue_items(include_finished=False):
        if q["id"] != item["id"] and q["status"] in ("queued", "running") and \
                q["title"] == item["title"] and q["body"] == item["body"] and q["repo"] == item["repo"]:
            return q["id"], False
    extra = {k: item.get(k) for k in ("issue_url", "queue") if item.get(k) and k not in kw}
    new_id = store.queue_add(item["title"], item["body"], item["repo"], **extra, **kw)
    mine = lane(store.queue_item(new_id))
    waiting = [q for q in store.queue_items() if q["status"] == "queued" and q["id"] != new_id and lane(q) == mine]
    if waiting:
        store.queue_update(new_id, position=waiting[0]["position"] - 0.5)
    set_lane_paused(store, mine, False)
    note = (item.get("note") or "").split(" · " + RESTARTED)[0]
    store.queue_update(item["id"], note=f"{note} · {RESTARTED}{new_id}".lstrip(" ·"))
    store.kv_set(PAUSED_KEY, "0")
    return new_id, True


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


def lane(item: dict) -> str:
    return (item.get("queue") or DEFAULT_QUEUE).strip() or DEFAULT_QUEUE


def lane_paused(store: Store, name: str) -> bool:
    return store.kv_get(LANE_PAUSED.format(name)) == "1"


def set_lane_paused(store: Store, name: str, paused: bool) -> None:
    store.kv_set(LANE_PAUSED.format(name), "1" if paused else None)


def resume_all(store: Store) -> None:
    store.kv_set(PAUSED_KEY, "0")
    for name in store.queue_names():
        set_lane_paused(store, name, False)


async def _run_safe(cfg, store, item, *, echo, llm_transport, sandbox) -> dict:
    try:
        return await run_item(cfg, store, item, echo=echo, llm_transport=llm_transport, sandbox=sandbox)
    except Exception as e:  # never let one item kill the worker silently
        store.queue_update(item["id"], status="failed", finished_at=time.time(),
                           note=f"worker error: {type(e).__name__}: {e}")
        return store.queue_item(item["id"])


async def work(cfg: Config, store: Store, *, echo=print, llm_transport=None, sandbox=None,
               max_items: int | None = None, poll_s: float = 2.0) -> int:
    """Work through every queue ("pile"). Each queue runs its items strictly in order, one at a time;
    different queues run side by side (at most `limits.max_parallel_queues`). Returns items processed.
    The global pause stops new starts everywhere; a failed item (policy "stop") pauses only its queue."""
    other = worker_pid(store)
    if other and other != os.getpid():
        if echo:
            echo(f"another queue worker is already running (pid {other})")
        return 0
    store.kv_set(WORKER_KEY, str(os.getpid()))
    done = started = 0
    running: dict[str, tuple[dict, asyncio.Task]] = {}
    try:
        # recover items left 'running' by a crashed worker
        for i in store.queue_items(include_finished=False):
            if i["status"] == "running":
                store.queue_update(i["id"], status="failed", finished_at=time.time(),
                                   note="worker stopped while this item was running")
                run = store.get_run(i["run_id"]) if i.get("run_id") else None
                if run and run["state"] not in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"):
                    store.update_run(run["id"], state="FAILED", finished_at=time.time(),
                                     state_reason="the queue worker stopped while this run was going")
        limit = max(1, int(getattr(cfg.limits, "max_parallel_queues", 3) or 1))
        while True:
            for name, (item, task) in list(running.items()):
                if not task.done():
                    continue
                del running[name]
                result = task.result()
                done += 1
                if echo:
                    echo(f"queue[{name}]: #{item['id']} -> {result['status']}")
                if result["status"] != "done" and (store.kv_get(POLICY_KEY) or "stop") == "stop":
                    set_lane_paused(store, name, True)
                    if echo:
                        echo(f"queue[{name}] paused because #{item['id']} did not complete (policy: stop on failure)")
            paused = store.kv_get(PAUSED_KEY) == "1"
            can_start = not paused and (max_items is None or started < max_items)
            startable = []
            if can_start:
                seen = set(running)
                for item in store.queue_items(include_finished=False):
                    name = lane(item)
                    if item["status"] != "queued" or name in seen or lane_paused(store, name):
                        continue
                    seen.add(name)
                    startable.append(item)
                for item in startable:
                    if len(running) >= limit or (max_items is not None and started >= max_items):
                        break
                    name = lane(item)
                    if echo:
                        echo(f"queue[{name}]: starting #{item['id']} {item['title']}")
                    running[name] = (item, asyncio.ensure_future(_run_safe(
                        cfg, store, item, echo=echo, llm_transport=llm_transport, sandbox=sandbox)))
                    started += 1
            if not running:
                if paused and echo:
                    echo("queue paused")
                if paused or not startable or (max_items is not None and started >= max_items):
                    break
                continue
            await asyncio.wait([t for _, t in running.values()], timeout=poll_s,
                               return_when=asyncio.FIRST_COMPLETED)
    finally:
        for _, task in running.values():          # worker cancelled (Ctrl+C): stop the runs it owns
            task.cancel()
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
