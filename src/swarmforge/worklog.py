"""The work log: one running record of every attempt at the same piece of work.

Retries, self-healing attempts, fix-ups, re-plans and a project's repeated tasks used to see only the
latest diagnosis. The work log keeps every attempt: what it tried, which tests failed and how, what
the reviewer still flagged, why it was judged to fail, and what the owner said when they declined
it. Every new attempt at the same work starts with the whole log (the Ralph-loop idea: memory lives
in a file every fresh agent reads), and the project lead reads it when it decides what to do next.

Entries are kept in the key-value table under `worklog:<lineage>`, newest last. The lineage is the
project task (`project:<id>:<milestone>:<task key>`) or, for other work, the first run of the chain
of retries, self-healing attempts and continuations.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

KEY = "worklog:"
DECLINE_KEY = "decline_reason:"
MAX_ENTRIES = 30
RENDER_CHARS = 9000


def lineage(store, run_id: str) -> str:
    seen, cur = set(), run_id
    while cur and cur not in seen:
        seen.add(cur)
        q = store._one("SELECT project_id, milestone, project_key FROM queue_items WHERE run_id=? "
                       "AND project_id IS NOT NULL AND project_key IS NOT NULL ORDER BY id DESC LIMIT 1", (cur,))
        if q:
            return f"project:{q['project_id']}:{q['milestone']}:{q['project_key']}"
        r = store.get_run(cur)
        nxt = r and (r.get("heal_of") or r.get("retry_of") or r.get("seed_run_id"))
        if not nxt or not store.get_run(nxt):
            return f"run:{cur}"
        cur = nxt
    return f"run:{cur}"


def entries(store, key: str) -> list[dict]:
    try:
        value = json.loads(store.kv_get(KEY + key) or "[]")
    except ValueError:
        return []
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _save(store, key: str, items: list[dict]) -> None:
    store.kv_set(KEY + key, json.dumps(items[-MAX_ENTRIES:]))


def _clip(text, n: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def _failed_tests(test_run: dict | None) -> list[dict]:
    if not test_run or not test_run.get("junit_path") or not Path(test_run["junit_path"]).is_file():
        return []
    from .testing import parse_junit
    try:
        _, failures, _ = parse_junit(Path(test_run["junit_path"]))
    except Exception:  # noqa: BLE001 - a broken report must not break the log
        return []
    return [{"test": f["id"], "error": _clip(f.get("message") or f.get("text"), 300)} for f in failures[:8]]


def build_entry(store, run_id: str) -> dict:
    run = store.get_run(run_id) or {}
    try:
        plan = json.loads(run.get("plan_json") or "null") or {}
    except ValueError:
        plan = {}
    tasks = store.tasks(run_id)
    tests = [t for t in store.test_runs(run_id) if t.get("label") != "baseline"]
    last = tests[-1] if tests else None
    cycle = run.get("repair_cycle") or 0
    blocking = [f for f in store.findings(run_id, cycle) if f.get("severity") in ("critical", "major")]
    fail = store.failure(run_id) or {}
    entry = {
        "run_id": run_id, "at": run.get("finished_at") or time.time(), "mode": run.get("mode"),
        "state": run.get("state"), "reason": _clip(run.get("state_reason"), 600),
        "outcome": "completed" if run.get("state") == "COMPLETED" else "stopped" if run.get("state") in ("CANCELLED",) else "failed",
        "approach": _clip(plan.get("summary"), 500),
        "tasks": [{"title": _clip(t.get("title") or t.get("task_key"), 120), "state": t.get("state"),
                   "error": _clip(t.get("error"), 200)} for t in tasks[:12]],
        "tests": (f"{last['passed']} passed, {last['failed']} failed, {last['errors']} errors"
                  + (" (timed out)" if last.get("timed_out") else "")) if last else "no test run",
        "failed_tests": _failed_tests(last),
        "review_problems": [_clip(f"[{f['severity']}] {f.get('title')}" + (f" ({f['file']})" if f.get("file") else ""), 200)
                            for f in blocking[:8]],
        "diagnosis": _clip(fail.get("root_cause"), 600), "fix": _clip(fail.get("fix"), 600),
        "category": fail.get("category") or "",
    }
    ralph = store.kv_get("ralph_summary:" + run_id)
    if ralph:
        try:
            entry["ralph"] = json.loads(ralph)
        except ValueError:
            pass
    declined = store.kv_get(DECLINE_KEY + run_id)
    if declined:
        entry["outcome"] = "declined by the owner"
        entry["owner"] = _clip(declined, 2000)
    return entry


def record(store, run_id: str) -> dict:
    """Add (or refresh) this run's entry in its work log; returns the entry."""
    key = lineage(store, run_id)
    items = [e for e in entries(store, key) if e.get("run_id") != run_id]
    entry = build_entry(store, run_id)
    items.append(entry)
    items.sort(key=lambda e: e.get("at") or 0)
    _save(store, key, items)
    return entry


def owner_said(store, run_id: str, reason: str) -> None:
    """The owner declined this run's result and said why: kept for every later attempt."""
    store.kv_set(DECLINE_KEY + run_id, reason.strip()[:2000])
    if store.get_run(run_id):
        record(store, run_id)


def _render_one(n: int, e: dict, full: bool) -> str:
    head = f"### Attempt {n}: {e.get('outcome', '?')} (run {e.get('run_id')}{', ' + e['mode'] if e.get('mode') else ''})"
    if not full:
        bits = [e.get("reason") or "", f"diagnosis: {e['diagnosis']}" if e.get("diagnosis") else "",
                f"owner: {e['owner']}" if e.get("owner") else ""]
        return head + "\n" + _clip(" · ".join(b for b in bits if b), 400)
    lines = [head]
    if e.get("owner"):
        lines.append(f"**The owner declined it and said:** {e['owner']}")
    if e.get("approach"):
        lines.append(f"Approach: {e['approach']}")
    if e.get("tasks"):
        lines.append("Work items: " + "; ".join(f"{t['title']} ({(t.get('state') or '?').lower()}"
                                                + (f": {t['error']}" if t.get("error") else "") + ")" for t in e["tasks"]))
    if e.get("ralph"):
        r = e["ralph"]
        lines.append(f"Ralph loop: {r.get('kept', 0)} pass(es) kept, {r.get('thrown_away', 0)} thrown away; "
                     f"stopped because {r.get('stopped', '?')}; plan items left: {r.get('open_items', '?')}")
    lines.append(f"Final tests: {e.get('tests')}")
    for t in e.get("failed_tests", []):
        lines.append(f"- failing `{t['test']}`: {t['error']}")
    for p in e.get("review_problems", []):
        lines.append(f"- review: {p}")
    if e.get("reason"):
        lines.append(f"Ended: {e['reason']}")
    if e.get("diagnosis"):
        lines.append(f"Diagnosis: {e['diagnosis']}")
    if e.get("fix"):
        lines.append(f"Suggested next: {e['fix']}")
    return "\n".join(lines)


def render(store, key: str, *, exclude: str | None = None, limit: int = RENDER_CHARS) -> str:
    """Markdown of every earlier attempt, oldest first; older ones shortened to fit `limit`."""
    items = [e for e in entries(store, key) if e.get("run_id") != exclude]
    if not items:
        return ""
    head = ["## Work log: every earlier attempt at this work",
            "Read this first. Do not repeat an approach that already failed; build on what worked, and fix "
            "what the owner asked for."]
    full = len(items)
    while True:
        parts = [_render_one(i + 1, e, i >= len(items) - full) for i, e in enumerate(items)]
        text = "\n\n".join(head + parts)
        if len(text) <= limit or full <= 1:
            return text[:limit]
        full -= 1


def for_run(store, run_id: str, **kw) -> str:
    return render(store, lineage(store, run_id), exclude=run_id, **kw)
