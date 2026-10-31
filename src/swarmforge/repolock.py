"""One active run per repository.

Runs that work on the same repository at the same time compete for the model provider's rate limit,
the machine's CPU and Docker, and usually edit the same files, so their pull requests conflict. A run
therefore waits (without using its time limit) until every earlier, still-active run on the same
repository has finished. Order is first come, first served by creation time.
"""
from __future__ import annotations

import re
from pathlib import Path

from .state import TERMINAL

GITHUB_PREFIX = re.compile(r"^(https?://(www\.)?github\.com/|git@github\.com:|github\.com/)", re.I)
TERMINAL_NAMES = {str(s) for s in TERMINAL}


def repo_key(repo: str | None, issue_url: str | None = None) -> str | None:
    """Normalized identity of a repository: 'owner/name' for GitHub, a resolved path for local repos."""
    s = (repo or "").strip()
    if not s and issue_url:
        m = re.match(r"^https?://(www\.)?github\.com/([^/]+)/([^/]+)/issues/\d+", issue_url.strip(), re.I)
        if m:
            s = f"{m.group(2)}/{m.group(3)}"
    if not s:
        return None
    if GITHUB_PREFIX.match(s) or re.match(r"^[\w.-]+/[\w.-]+$", s):
        s = GITHUB_PREFIX.sub("", s)
        s = re.split(r"/(tree|blob|issues|pull)/", s)[0]
        s = s.rstrip("/").removesuffix(".git")
        return s.lower()
    try:
        p = Path(s).expanduser()
        if p.exists():
            return str(p.resolve()).rstrip("/\\").lower()
    except OSError:
        pass
    return s.rstrip("/\\").removesuffix(".git").lower()


def blocking_runs(store, run_id: str, key: str, alive=None) -> list[dict]:
    """Earlier runs on the same repository that are still active (not finished, process alive)."""
    if alive is None:
        from .launcher import pid_alive as alive
    me = store.get_run(run_id)
    if not me:
        return []
    mine = (me["created_at"], run_id)
    out = []
    for r in store.list_runs(limit=500):
        if r["id"] == run_id or r["state"] in TERMINAL_NAMES:
            continue
        if (r["created_at"], r["id"]) >= mine:
            continue
        if repo_key(r.get("repo"), r.get("issue_url")) != key:
            continue
        if not alive(r.get("pid")):
            continue
        out.append(r)
    return out
