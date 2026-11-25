"""Read-only review of what a project built: milestones, checks, delivered tasks, screenshots, report.

Used by the web interface when a project waits for the owner's review (or is finished), so the
owner sees in one place what changed, how it was tested and what still needs a person.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .artifacts import ArtifactReader, ArtifactUnavailable

MAX_TASKS = 60
DONE_MILESTONE = ("done", "verification_pending", "checkpoint")


def _json(text):
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _section(report: str, heading: str, limit: int = 900) -> str:
    m = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", report or "")
    text = (m.group(1).strip() if m else "").replace("\r", "")
    text = text.split("\n\nAcceptance criteria:")[0].strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def project_review(conn, artifacts: ArtifactReader, pid: str) -> dict | None:
    p = conn.execute("SELECT id,name,repo,status,current_milestone,budget_usd,definition FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    definition = _json(p["definition"])
    planned = definition.get("milestones") or []
    milestones = []
    for m in conn.execute("SELECT idx,name,status,report FROM milestones WHERE project_id=? ORDER BY idx", (pid,)):
        report = _json(m["report"])
        goal = planned[m["idx"]].get("goal", "") if m["idx"] < len(planned) and isinstance(planned[m["idx"]], dict) else ""
        milestones.append({"index": m["idx"], "name": m["name"], "status": m["status"], "goal": goal,
                           "summary": str(report.get("summary") or "")[:3000],
                           "in_review": m["status"] in ("checkpoint", "verification_pending"),
                           "checks": [{"text": str(c.get("text") or "")[:600], "status": str(c.get("status") or ""),
                                       "evidence": [str(e.get("test") or e.get("name") or e)[:200] if isinstance(e, dict) else str(e)[:200]
                                                    for e in (c.get("evidence") or [])][:6]}
                                      for c in (report.get("checks") or []) if isinstance(c, dict)]})
    tasks = []
    rows = conn.execute("""SELECT q.id,q.title,q.milestone,q.run_id,q.merged_sha,q.status,r.pr_url,r.state_reason,
                                  COALESCE(r.finished_at,q.finished_at) AS finished_at,
                                  (SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE run_id=q.run_id) AS cost_usd
                           FROM queue_items q LEFT JOIN runs r ON r.id=q.run_id
                           WHERE q.project_id=? AND q.status='done' AND q.run_id IS NOT NULL
                           ORDER BY q.id DESC LIMIT ?""", (pid, MAX_TASKS)).fetchall()
    for q in rows:
        test = conn.execute("SELECT passed,failed,errors,skipped,exit_code FROM test_runs WHERE run_id=? ORDER BY id DESC LIMIT 1",
                            (q["run_id"],)).fetchone()
        acceptance = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status,COUNT(*) AS n FROM acceptance WHERE run_id=? AND cycle=(SELECT MAX(cycle) FROM acceptance WHERE run_id=?) GROUP BY status",
            (q["run_id"], q["run_id"]))}
        try:
            shots = [s["name"] for s in artifacts.listing(q["run_id"]).get("screenshots", [])]
        except ArtifactUnavailable:
            shots = []
        try:
            text = artifacts.report(q["run_id"]).get("text", "")
        except ArtifactUnavailable:
            text = ""
        tasks.append({"item_id": f"task-{q['id']}", "title": q["title"], "milestone": q["milestone"], "run_id": q["run_id"],
                      "pr_url": q["pr_url"], "merged": bool(q["merged_sha"]), "finished_at": q["finished_at"],
                      "summary": str(q["state_reason"] or "")[:400], "what_it_did": _section(text, "Plan"),
                      "cost_usd": round(float(q["cost_usd"] or 0), 4),
                      "tests": dict(test) if test else None, "acceptance": acceptance, "screenshots": shots[:12],
                      "has_report": bool(text)})
    spent = conn.execute("""SELECT COALESCE(SUM(c.cost_usd),0) FROM llm_calls c WHERE c.run_id='project:'||?
                            OR c.run_id IN (SELECT run_id FROM queue_items WHERE project_id=? AND run_id IS NOT NULL)""",
                         (pid, pid)).fetchone()[0]
    in_review = [m for m in milestones if m["in_review"]]
    checks = [c for m in (in_review or milestones) for c in m["checks"]]
    review = {"project_id": pid, "name": p["name"], "repo": p["repo"], "status": p["status"],
            "budget_usd": p["budget_usd"], "spent_usd": round(float(spent or 0), 2), "milestones": milestones,
            "tasks": tasks, "screenshot_count": sum(len(t["screenshots"]) for t in tasks),
            "counts": {"tasks": len(tasks), "merged": sum(t["merged"] for t in tasks),
                       "checks_passed": sum(c["status"] == "passed" for c in checks),
                       "checks_need_you": sum(c["status"] not in ("passed", "failed") for c in checks),
                       "checks_failed": sum(c["status"] == "failed" for c in checks)}}
    from ..projects.explainer import scope, stored
    review["explainer_scope"] = scope(review)
    try:
        review["explainer"] = stored(conn, pid, review)
    except sqlite3.Error:  # read-only view of an older database without the kv table
        review["explainer"] = None
    return review


def review_markdown(review: dict) -> str:
    """The same review as a document the owner can keep or share."""
    lines = [f"# Review: {review['name']}", "", f"Repository: {review['repo']}  ", f"Status: {review['status']}  ",
             f"Spent: ${review['spent_usd']:.2f} of ${review['budget_usd']:.2f}", ""]
    explained = (review.get("explainer") or {}).get("explainer")
    if explained:
        from ..projects.explainer import markdown
        lines += markdown(explained)
        lines += ["## The details", ""]
    c = review["counts"]
    lines += [f"**{c['tasks']} tasks delivered, {c['merged']} merged.** Checks under review: {c['checks_passed']} passed, "
              f"{c['checks_need_you']} need you, {c['checks_failed']} failed.", ""]
    for m in review["milestones"]:
        lines += [f"## Milestone {m['index'] + 1}: {m['name']} ({m['status'].replace('_', ' ')})", ""]
        if m["goal"]:
            lines += [m["goal"], ""]
        if m["summary"]:
            lines += ["**Lead's summary:** " + m["summary"], ""]
        for chk in m["checks"]:
            mark = {"passed": "✅", "failed": "❌"}.get(chk["status"], "👤")
            proof = f" — evidence: {', '.join(chk['evidence'])}" if chk["evidence"] else ""
            lines.append(f"- {mark} {chk['text']} *({chk['status'] or 'needs you'})*{proof}")
        tasks = [t for t in review["tasks"] if t["milestone"] == m["index"]]
        if tasks:
            lines += ["", "### What was built", ""]
        for t in tasks:
            tests = t["tests"]
            result = f"{tests['passed']} tests passed, {tests['failed']} failed" if tests else "no test run recorded"
            acc = ", ".join(f"{k}: {v}" for k, v in sorted(t["acceptance"].items())) or "none"
            lines += [f"#### {t['title']}", "", f"- Pull request: {t['pr_url'] or '—'}{' (merged)' if t['merged'] else ''}",
                      f"- Tests: {result}; acceptance: {acc}", f"- Cost: ${t['cost_usd']:.2f}"]
            if t["what_it_did"]:
                lines += ["", t["what_it_did"]]
            if t["screenshots"]:
                lines += ["", "Screenshots: " + ", ".join(t["screenshots"])]
            lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
