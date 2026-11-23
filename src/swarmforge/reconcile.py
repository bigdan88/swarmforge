"""Reconciliation: find out whether an external operation actually happened, and keep the evidence.

An external write (push, open a PR, merge, close) records its intent before it runs. If the process
dies, or the call fails in a way that leaves the outcome unknown, the record stays open and nothing is
retried automatically. Reconciliation asks GitHub what really happened and settles the record:

    done      it happened: the operation's result is recovered and later replays return it
    not_done  it did not happen: doing it again is safe
    conflict  something else is there (for example a branch at a different commit): a human decides
    unknown   GitHub could not be asked or the answer is ambiguous: the record stays open

Every verdict is stored in `reconciliations` with the evidence it rests on (HTTP status, the remote
commit, the PR state), whichever way it went. Model calls are never reconciled: a provider cannot be
asked whether it billed a request, so an uncertain call stays charged (see docs/EXECUTION.md).

Network calls happen here, never inside a database transaction: look first, then settle in a short
transaction that only changes a record that is still open.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .db import Store

DONE, NOT_DONE, CONFLICT, UNKNOWN = "done", "not_done", "conflict", "unknown"
OPEN = ("pending", "uncertain")
STALE_REQUEST_S = 600.0  # a web action still "pending" after this long was interrupted

SCHEMA = """
CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL,
    outcome TEXT NOT NULL, evidence TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS reconciliations_key ON reconciliations(source, key, id);
"""


@dataclass
class Verdict:
    outcome: str
    result: Any = None
    evidence: dict = field(default_factory=dict)
    note: str = ""


def ensure_tables(store: Store) -> None:
    store.ensure_schema(SCHEMA)


def local_head(repo_dir, branch: str) -> str | None:
    """The commit a push is about to send, kept as intent so a later check can recognize it."""
    try:
        from .workspace import git_rc
        rc, out = git_rc(repo_dir, "rev-parse", branch)
    except Exception:  # noqa: BLE001 - missing checkout: the check falls back to "the branch exists"
        return None
    return out.strip() if rc == 0 and out.strip() else None


# --------------------------------------------------------------------------- asking GitHub
def _get(gh, path: str, params: dict | None = None):
    """GET from GitHub; returns (status, json or None). Network errors become status 0."""
    import httpx
    try:
        r = gh._client.get(path, params=params)
    except httpx.HTTPError as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


def _unknown(status: int, what: str, body: Any = None) -> Verdict:
    detail = body.get("message") or body.get("error") if isinstance(body, dict) else None
    return Verdict(UNKNOWN, evidence={"http_status": status, "detail": (detail or "")[:300]},
                   note=f"Could not ask GitHub about {what} (HTTP {status or 'unreachable'})")


def _branch(gh, repo: str, branch: str, sha: str | None) -> Verdict:
    status, body = _get(gh, f"/repos/{repo}/git/ref/heads/{branch}")
    if status == 404 or (status == 200 and isinstance(body, list)):  # a list means only prefix matches
        return Verdict(NOT_DONE, evidence={"http_status": 404, "branch": branch}, note=f"{branch} does not exist on {repo}")
    if status != 200 or not isinstance(body, dict):
        return _unknown(status, f"branch {branch}", body)
    remote = (body.get("object") or {}).get("sha")
    evidence = {"http_status": 200, "branch": branch, "remote_sha": remote, "expected_sha": sha}
    if sha and remote and remote != sha:
        return Verdict(CONFLICT, evidence=evidence,
                       note=f"{branch} exists on {repo} at {remote[:12]}, not the pushed commit {sha[:12]}")
    return Verdict(DONE, None, evidence, f"{branch} is on {repo} at {(remote or '?')[:12]}")


def _pr_from(gh, repo: str, branch: str) -> Verdict:
    owner = repo.split("/", 1)[0]
    status, body = _get(gh, f"/repos/{repo}/pulls", {"head": f"{owner}:{branch}", "state": "all", "per_page": 5})
    if status != 200 or not isinstance(body, list):
        return _unknown(status, f"pull requests from {branch}", body)
    if not body:
        return Verdict(NOT_DONE, evidence={"http_status": 200, "head": branch, "found": 0},
                       note=f"No pull request from {branch} on {repo}")
    pr = sorted(body, key=lambda p: p.get("number", 0))[0]
    return Verdict(DONE, pr.get("html_url"), {"http_status": 200, "head": branch, "number": pr.get("number"),
                                              "state": pr.get("state"), "url": pr.get("html_url")},
                   f"Pull request #{pr.get('number')} from {branch} exists")


def _pull(gh, repo: str, number: int):
    status, body = _get(gh, f"/repos/{repo}/pulls/{number}")
    if status != 200 or not isinstance(body, dict):
        return None, _unknown(status, f"{repo} #{number}", body)
    return body, None


def _merged(gh, repo: str, number: int, expected_sha: str | None = None) -> Verdict:
    pr, failed = _pull(gh, repo, number)
    if failed:
        return failed
    evidence = {"http_status": 200, "number": number, "state": pr.get("state"), "merged": bool(pr.get("merged")),
                "merge_commit_sha": pr.get("merge_commit_sha"), "head_sha": (pr.get("head") or {}).get("sha"),
                "merged_at": pr.get("merged_at")}
    if pr.get("merged"):
        if expected_sha and evidence["head_sha"] and evidence["head_sha"] != expected_sha:
            return Verdict(CONFLICT, evidence=evidence | {"expected_sha": expected_sha},
                           note=f"#{number} was merged, but at {evidence['head_sha'][:12]}, not the reviewed {expected_sha[:12]}")
        return Verdict(DONE, pr.get("merge_commit_sha"), evidence, f"#{number} was merged ({pr.get('merged_at')})")
    return Verdict(NOT_DONE, evidence=evidence, note=f"#{number} is {pr.get('state')} and not merged")


def _closed(gh, repo: str, number: int) -> Verdict:
    pr, failed = _pull(gh, repo, number)
    if failed:
        return failed
    evidence = {"http_status": 200, "number": number, "state": pr.get("state"), "closed_at": pr.get("closed_at")}
    if pr.get("state") == "closed":
        return Verdict(DONE, None, evidence, f"#{number} is closed")
    return Verdict(NOT_DONE, evidence=evidence, note=f"#{number} is still open")


def _pr_url(url: str) -> tuple[str, int] | None:
    parts = url.rstrip("/").split("/")
    if len(parts) >= 5 and parts[-2] == "pull" and parts[-1].isdigit():
        return f"{parts[-4]}/{parts[-3]}", int(parts[-1])
    return None


def check_external(gh, name: str, intent: dict | None = None) -> Verdict:
    """Verdict for an `external_once` operation, by its name ("push:owner/repo:branch", ...)."""
    intent = intent or {}
    kind, _, rest = name.removeprefix("external:").partition(":")
    if kind in ("push", "pr"):
        repo, _, branch = rest.partition(":")
        if "/" not in repo or not branch:
            return Verdict(UNKNOWN, note=f"Cannot read the repository and branch from {name}")
        return _branch(gh, repo, branch, intent.get("sha")) if kind == "push" else _pr_from(gh, repo, branch)
    if kind == "merge":
        repo, _, number = rest.rpartition(":")
        if not number.isdigit():
            return Verdict(UNKNOWN, note=f"Cannot read the pull request from {name}")
        return _merged(gh, repo, int(number), intent.get("head_sha"))
    if kind == "close":
        parsed = _pr_url(rest)
        if not parsed:
            return Verdict(UNKNOWN, note=f"Cannot read the pull request from {name}")
        return _closed(gh, *parsed)
    return Verdict(UNKNOWN, note=f"{kind or name} cannot be checked from GitHub; inspect it by hand")


def check_request(gh, operation: str, target: str, args: dict) -> Verdict:
    """Verdict for an interrupted web action, where GitHub can tell."""
    repo, _, number = target.rpartition("#")
    if operation in ("merge_pr", "update_pr") and repo and number.isdigit():
        if operation == "merge_pr":
            return _merged(gh, repo, int(number), args.get("expected_sha"))
        pr, failed = _pull(gh, repo, int(number))
        if failed:
            return failed
        head = (pr.get("head") or {}).get("sha")
        evidence = {"http_status": 200, "number": int(number), "head_sha": head, "expected_sha": args.get("expected_sha")}
        if head and head != args.get("expected_sha"):
            return Verdict(DONE, None, evidence, f"#{number} has new commits since the request (branch updated)")
        return Verdict(NOT_DONE, evidence=evidence, note=f"#{number} still has the reviewed head commit")
    return Verdict(UNKNOWN, note=f"{operation} cannot be checked from GitHub; inspect its effect by hand")


# --------------------------------------------------------------------------- settling records
def _record(db, source: str, key: str, verdict: Verdict) -> None:
    db.execute("INSERT INTO reconciliations(source,key,ts,outcome,evidence,note) VALUES (?,?,?,?,?,?)",
               (source, key, time.time(), verdict.outcome, json.dumps(verdict.evidence, default=str), verdict.note[:1000]))


def settle_checkpoint(store: Store, execution_id: str, name: str, verdict: Verdict) -> bool:
    """Apply a verdict to an open checkpoint. Returns True if the record changed."""
    ensure_tables(store)
    with store.transaction() as db:
        row = db.execute("SELECT status,detail FROM execution_checkpoints WHERE execution_id=? AND name=?",
                         (execution_id, name)).fetchone()
        _record(db, "checkpoint", f"{execution_id}|{name}", verdict)
        if not row or row["status"] not in OPEN or verdict.outcome == UNKNOWN:
            return False
        if verdict.outcome == DONE:
            status, detail = "completed", json.dumps(verdict.result, default=str)
        else:
            previous = _loads(row["detail"])
            detail = json.dumps({**(previous if isinstance(previous, dict) else {}), "reconciled": verdict.note}, default=str)
            status = "not_done" if verdict.outcome == NOT_DONE else "conflict"
        db.execute("UPDATE execution_checkpoints SET status=?,detail=?,ts=? WHERE execution_id=? AND name=? AND status IN ('pending','uncertain')",
                   (status, detail, time.time(), execution_id, name))
        return True


def settle_request(store: Store, key: str, verdict: Verdict) -> dict | None:
    """Apply a verdict to an interrupted web action; returns its now-final result, if any."""
    ensure_tables(store)
    with store.transaction() as db:
        row = db.execute("SELECT * FROM dashboard_requests WHERE key=?", (key,)).fetchone()
        _record(db, "request", key, verdict)
        if not row or row["status"] not in OPEN or verdict.outcome == UNKNOWN:
            return None
        if verdict.outcome == DONE:
            result = {"ok": True, "message": f"Done (confirmed on GitHub after the request was interrupted): {verdict.note}"}
            status = "done"
        elif verdict.outcome == NOT_DONE:
            result = {"ok": False, "message": f"Not done (confirmed on GitHub): {verdict.note}. You can do it again."}
            status = "rejected"
        else:
            result = {"ok": False, "message": f"Needs you: {verdict.note}"}
            status = "conflict"
        db.execute("UPDATE dashboard_requests SET status=?,result=? WHERE key=? AND status IN ('pending','uncertain')",
                   (status, json.dumps(result), key))
        return result


def _loads(text: str | None) -> Any:
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


# --------------------------------------------------------------------------- finding open records
def _owner_active(db, execution_id: str, run_id: str) -> bool:
    from .launcher import pid_alive
    row = db.execute("SELECT active_run,owner_pid FROM execution_allowances WHERE id=?", (execution_id,)).fetchone()
    return bool(row and row["active_run"] == run_id and pid_alive(row["owner_pid"]))


def open_checkpoints(store: Store) -> list[dict]:
    """Uncertain external operations, and pending ones whose attempt is no longer running."""
    rows = store._all("SELECT * FROM execution_checkpoints WHERE name LIKE 'external:%' AND status IN ('pending','uncertain')")
    with store._lock:
        return [r for r in rows if r["status"] == "uncertain" or not _owner_active(store.conn, r["execution_id"], r["run_id"])]


def open_requests(store: Store) -> list[dict]:
    if not store._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_requests'"):
        return []
    rows = store._all("SELECT * FROM dashboard_requests WHERE status IN ('pending','uncertain')")
    now = time.time()
    return [r for r in rows if r["status"] == "uncertain" or now - r["created_at"] > STALE_REQUEST_S]


def uncertain_model_calls(store: Store) -> int:
    row = store._one("SELECT COUNT(*) AS n FROM execution_calls WHERE status='uncertain'")
    return int(row["n"]) if row else 0


def reconcile_all(store: Store, cfg, *, gh=None) -> list[dict]:
    """Settle every open external operation GitHub can answer for. Returns one line per record."""
    from .github import GitHubClient
    ensure_tables(store)
    checkpoints, requests = open_checkpoints(store), open_requests(store)
    if not checkpoints and not requests:
        return []
    gh = gh or GitHubClient(cfg.github)
    report = []
    for row in checkpoints:
        detail = _loads(row["detail"])
        verdict = check_external(gh, row["name"], (detail or {}).get("intent") if isinstance(detail, dict) else None)
        settle_checkpoint(store, row["execution_id"], row["name"], verdict)
        report.append({"source": "task", "run_id": row["run_id"], "operation": row["name"].removeprefix("external:"),
                       "outcome": verdict.outcome, "note": verdict.note})
    for row in requests:
        args = _loads(row["intent"]) if "intent" in row.keys() else None
        verdict = check_request(gh, row["operation"], row["target"], args if isinstance(args, dict) else {})
        settle_request(store, row["key"], verdict)
        report.append({"source": "web action", "operation": f"{row['operation']} {row['target']}".strip(),
                       "outcome": verdict.outcome, "note": verdict.note})
    return report


async def reconcile_execution(store: Store, cfg, execution_id: str, *, gh=None) -> list[dict]:
    """Settle one task lineage's open external operations before its next attempt starts."""
    import asyncio
    rows = [r for r in open_checkpoints(store) if r["execution_id"] == execution_id]
    if not rows:
        return []
    from .github import GitHubClient
    gh = gh or GitHubClient(cfg.github)
    report = []
    for row in rows:
        detail = _loads(row["detail"])
        verdict = await asyncio.to_thread(check_external, gh, row["name"],
                                          (detail or {}).get("intent") if isinstance(detail, dict) else None)
        settle_checkpoint(store, execution_id, row["name"], verdict)
        report.append({"operation": row["name"].removeprefix("external:"), "outcome": verdict.outcome, "note": verdict.note})
    return report


def evidence(store: Store, source: str, key: str) -> list[dict]:
    ensure_tables(store)
    rows = store._all("SELECT ts,outcome,evidence,note FROM reconciliations WHERE source=? AND key=? ORDER BY id", (source, key))
    return [{**r, "evidence": _loads(r["evidence"])} for r in rows]
