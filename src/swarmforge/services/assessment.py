"""Pure delivery-gate assessment; state transitions remain in the orchestrator."""
import json
from typing import TypedDict

from ..config import ReviewConfig
from ..state import finding_in_scope
from ..testing import TestEvidence
from ..visual import is_blocking as is_visual_blocking


class GateProblem(TypedDict):
    kind: str
    summary: str
    detail: str


def assess_gate(ev: TestEvidence, *, criteria: list[dict], tester_report: dict | None,
                review: dict | None, failed_tasks: list[str], task_rows: list[dict],
                scope: set[str], visual_findings: list[dict], policy: ReviewConfig) -> list[GateProblem]:
    """Deterministic approval gate. Returns blocking problems (empty == approved)."""
    problems: list[GateProblem] = []
    if policy.require_junit and not ev.junit_found:
        problems.append({"kind": "tests", "summary": "required JUnit evidence is missing",
                         "detail": ev.failure_report()})
    elif not ev.ok:
        problems.append({"kind": "tests", "summary": f"test suite not passing: {ev.short()}",
                         "detail": ev.failure_report()})
    for r in criteria:
        if r["status"] == "failed" or (policy.require_verified_criteria and r["status"] != "satisfied"):
            problems.append({"kind": "acceptance", "summary": f"acceptance criterion {r['criterion_id']} failing",
                             "detail": f"{r['criterion_id']}: {r['text']}\nEvidence: {r['evidence']}"})
    blocking = set(policy.blocking_severities)
    if review:
        for f in review["findings"]:
            if f["severity"] in blocking and finding_in_scope(f, scope):
                problems.append({"kind": "review", "summary": f"[{f['severity']}] {f['title']} ({f.get('file')})",
                                 "detail": json.dumps(f, indent=1)})
    if tester_report:
        for d in tester_report["defects"]:
            if d["severity"] in blocking:
                problems.append({"kind": "defect", "summary": f"[defect/{d['severity']}] {d['title']}",
                                 "detail": json.dumps(d, indent=1)})
    for f in visual_findings or []:
        if is_visual_blocking(f["severity"]) or f.get("blocking"):
            problems.append({"kind": "visual", "summary": f"[visual/{f['severity']}] {f['title']}",
                             "detail": json.dumps(f, indent=1)})
    for tid in failed_tasks:
        row = next((t for t in task_rows if t["task_key"] == tid), {})
        problems.append({"kind": "task", "summary": f"task {tid} did not complete",
                         "detail": f"Task {tid} ({row.get('title')}) failed: {row.get('error')}\n"
                                   f"{row.get('description')}"})
    return problems
