"""`swarmforge watch`: hand GitHub issues to the swarm by labelling them.

Add the label (default `swarmforge`) to an issue in a watched repository. The watcher
  1. queues it (the run reads the issue from GitHub, so the PR says "Addresses #N"),
  2. swaps the label for `swarmforge:queued` and comments with the queue position,
  3. sets `swarmforge:working` when the run starts, and
  4. when it ends, comments with the run summary and the draft PR link and sets
     `swarmforge:done` or `swarmforge:failed`.
Re-adding the trigger label later queues the issue again. Nothing is ever merged.
"""
from __future__ import annotations

import time
from typing import Callable

from .config import Config
from .db import Store
from .github import GitHubClient, GitHubError, parse_issue_url

STATE_LABELS = ("swarmforge:queued", "swarmforge:working", "swarmforge:done", "swarmforge:failed")
SEEN = "watch:seen:{repo}#{num}"


def _repo(spec: str) -> tuple[str, str]:
    s = spec.strip().removeprefix("https://github.com/").removeprefix("http://github.com/").rstrip("/")
    s = s.removesuffix(".git")
    owner, _, name = s.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"watch.repos entries must look like owner/name, got {spec!r}")
    return owner, name


def _set_state(gh: GitHubClient, owner: str, name: str, num: int, state: str, drop: list[str]) -> None:
    for lab in drop:
        try:
            gh.remove_label(owner, name, num, lab)
        except GitHubError:
            pass
    gh.add_labels(owner, name, num, [state])


def poll_once(cfg: Config, store: Store, gh: GitHubClient, echo: Callable[[str], None] | None = print) -> list[int]:
    """Queue every newly labelled issue in the watched repositories. Returns new queue item ids."""
    w = cfg.watch
    added: list[int] = []
    for spec in w.repos:
        owner, name = _repo(spec)
        try:
            issues = gh.labeled_issues(owner, name, w.label)
        except GitHubError as e:
            if echo:
                echo(f"watch: {owner}/{name}: {e}")
            continue
        for issue in issues:
            key = SEEN.format(repo=f"{owner}/{name}".lower(), num=issue.number)
            prev = store.kv_get(key)
            if prev:
                item = store.queue_item(int(prev))
                if item and item["status"] in ("queued", "running"):
                    continue          # already in the queue; the label just wasn't swapped yet
            item_id = store.queue_add(
                issue.title, issue.body or "(no description)", f"https://github.com/{owner}/{name}",
                issue_url=issue.url or f"https://github.com/{owner}/{name}/issues/{issue.number}",
                approval=w.approval, chain=0, budget=w.budget_usd, max_cycles=w.max_cycles, queue=w.queue)
            store.kv_set(key, str(item_id))
            added.append(item_id)
            if echo:
                echo(f"watch: queued {owner}/{name}#{issue.number} '{issue.title}' as task #{item_id}")
            try:
                _set_state(gh, owner, name, issue.number, "swarmforge:queued", [w.label, *STATE_LABELS])
                if w.comment:
                    waiting = sum(1 for i in store.queue_items(include_finished=False) if i["status"] == "queued")
                    gh.comment(owner, name, issue.number,
                               f"🤖 SwarmForge queued this issue as task #{item_id} ({waiting} task(s) waiting). "
                               f"It will open a **draft** pull request when it's done; nothing is merged "
                               f"automatically.")
            except GitHubError as e:
                if echo:
                    echo(f"watch: queued, but could not update the issue (token needs Issues: write): {e}")
    return added


def on_run_start(cfg: Config, item: dict, gh: GitHubClient | None = None) -> None:
    if not item.get("issue_url"):
        return
    owner, name, num = parse_issue_url(item["issue_url"])
    gh = gh or GitHubClient(cfg.github)
    _set_state(gh, owner, name, num, "swarmforge:working", list(STATE_LABELS))


def on_run_end(cfg: Config, store: Store, item: dict, run_id: str, gh: GitHubClient | None = None) -> None:
    if not item.get("issue_url"):
        return
    from .report import build_summary
    owner, name, num = parse_issue_url(item["issue_url"])
    gh = gh or GitHubClient(cfg.github)
    run = store.get_run(run_id)
    ok = run and run["state"] == "COMPLETED"
    _set_state(gh, owner, name, num, "swarmforge:done" if ok else "swarmforge:failed", list(STATE_LABELS))
    if cfg.watch.comment and run:
        head = (f"🤖 SwarmForge finished: draft PR {run['pr_url']}" if run.get("pr_url") else
                f"🤖 SwarmForge run `{run_id}` ended: **{run['state']}**")
        summary = build_summary(store, run_id)
        summary = summary.split("### What was done", 1)[-1]          # the issue already has the request
        gh.comment(owner, name, num, f"{head}\n\n### What was done{summary}\n\n"
                                     f"_Add the `{cfg.watch.label}` label again to retry._")


def watch(cfg: Config, store: Store, *, once: bool = False, interval_s: int | None = None,
          start_worker: bool = True, echo: Callable[[str], None] = print,
          gh: GitHubClient | None = None, sleep=time.sleep) -> int:
    if not cfg.watch.repos:
        echo("nothing to watch: add `watch: {repos: [owner/name]}` to swarmforge.yaml")
        return 2
    gh = gh or GitHubClient(cfg.github)
    if not gh.token:
        echo(f"warning: no GitHub token ({cfg.github.token_env}); labels and comments can't be updated "
             "and private repositories can't be read")
    every = int(interval_s or cfg.watch.interval_s)
    echo(f"watching {', '.join(cfg.watch.repos)} for issues labelled '{cfg.watch.label}' (every {every}s)")
    while True:
        added = poll_once(cfg, store, gh, echo)
        if added and start_worker:
            from .runqueue import PAUSED_KEY
            from .runqueue import start_worker as start
            if store.kv_get(PAUSED_KEY) == "1":
                echo("watch: the queue is paused; resume it to start the new task(s)")
            else:
                echo(f"watch: queue worker running (pid {start(cfg, store)})")
        if once:
            return 0
        sleep(every)
