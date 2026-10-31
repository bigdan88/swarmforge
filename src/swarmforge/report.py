"""Execution reports (Markdown) built purely from recorded telemetry."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "-"


def _dur(a: float | None, b: float | None) -> str:
    if not a or not b:
        return "-"
    s = int(b - a)
    return f"{s // 60}m{s % 60:02d}s"


def _money(x: float | None) -> str:
    return "-" if x is None else f"${x:.4f}"


def parallel_intervals(tasks: list[dict]) -> int:
    """Max number of tasks whose [started_at, ended_at] intervals overlapped (measured)."""
    pts = []
    for t in tasks:
        if t.get("started_at") and t.get("ended_at"):
            pts += [(t["started_at"], 1), (t["ended_at"], -1)]
    cur = best = 0
    for _, d in sorted(pts, key=lambda p: (p[0], p[1])):
        cur += d
        best = max(best, cur)
    return best


def cost_lines(store, run_id: str) -> list[str]:
    c = store.cost_summary(run_id)
    lines = [
        f"- LLM calls: {c.get('calls') or 0} "
        f"({c.get('calls_without_usage') or 0} without provider token usage)",
        f"- Tokens: {c.get('prompt_tokens') or 0:,} prompt + {c.get('completion_tokens') or 0:,} completion "
        f"= {c.get('total_tokens') or 0:,} total (as reported by the provider)",
        f"- Cost reported by provider (DeepInfra `usage.estimated_cost`): {_money(c.get('measured'))}",
        f"- Cost estimated by SwarmForge from token counts x published prices: {_money(c.get('estimated'))}",
        f"- Calls with no cost information: {c.get('unavailable_calls') or 0}",
        f"- **Total known cost: {_money(c.get('total'))}**",
    ]
    return lines


def build_report(store, run_id: str) -> str:
    run = store.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    tasks = store.tasks(run_id)
    events = store.events(run_id)
    tests = store.test_runs(run_id)
    findings = store.findings(run_id)
    acc = store.acceptance(run_id)
    inter = store.interventions(run_id)
    by_role = store.cost_by_role(run_id)
    cfg = json.loads(run["config_json"] or "{}")

    L = [f"# SwarmForge execution report - run `{run_id}`", ""]
    L += [
        "| Field | Value |", "|---|---|",
        f"| Issue | {run['issue_title'] or '-'} |",
        f"| Source | {run['issue_url'] or '-'} |",
        f"| Repository | {run['repo'] or '-'} |",
        f"| Mode | {run['mode']} |",
        f"| Final state | **{run['state']}** |",
        f"| Reason | {run['state_reason'] or '-'} |",
        f"| Started | {_ts(run['created_at'])} |",
        f"| Finished | {_ts(run['finished_at'])} |",
        f"| Elapsed | {_dur(run['created_at'], run['finished_at'] or run['updated_at'])} |",
        f"| Base branch | {run['base_branch'] or '-'} |",
        f"| Result branch | {run['integration_branch'] or '-'} |",
        f"| Repair cycles used | {run['repair_cycle']} |",
        f"| Human interventions | {len(inter)} |",
        f"| Pull request | {run['pr_url'] or 'not created'} |",
        "",
    ]
    if cfg.get("roles"):
        L += ["## Model assignments", "", "| Role | Model | Provider |", "|---|---|---|"]
        for r, v in cfg["roles"].items():
            L.append(f"| {r} | `{v['model']}` | {v['provider']} |")
        L.append("")

    plan = json.loads(run["plan_json"]) if run["plan_json"] else None
    if plan:
        L += ["## Plan", "", plan.get("summary", ""), ""]
        if plan.get("acceptance_criteria"):
            L += ["Acceptance criteria:", ""] + [f"- **{c['id']}**: {c['text']}" for c in plan["acceptance_criteria"]] + [""]

    L += ["## Tasks", "",
          "| Task | Cycle | Kind | Role | Model | Depends on | State | Attempts | Duration |",
          "|---|---|---|---|---|---|---|---|---|"]
    for t in tasks:
        L.append(f"| {t['task_key']} {('- ' + t['title'][:50]) if t['title'] else ''} | {t['cycle']} | {t['kind']} | "
                 f"{t['role'] or '-'} | `{t['model'] or '-'}` | {', '.join(t['depends_on']) or '-'} | "
                 f"{t['state']} | {t['attempts']} | {_dur(t['started_at'], t['ended_at'])} |")
    impl = [t for t in tasks if t["kind"] in ("implement", "fix")]
    L += ["", f"Measured maximum concurrently running coding tasks: **{parallel_intervals(impl)}**", ""]
    for t in tasks:
        if t.get("summary") or t.get("error"):
            L.append(f"- **{t['task_key']}**: " + (t.get("summary") or "").replace("\n", " ")[:600]
                     + (f" _Error: {t['error'][:300]}_" if t.get("error") else ""))
    L.append("")

    L += ["## Test evidence (executed by the orchestrator, not reported by agents)", "",
          "| Label | Cycle | Exit | Passed | Failed | Errors | Skipped | Duration | Log |",
          "|---|---|---|---|---|---|---|---|---|"]
    for tr in tests:
        L.append(f"| {tr['label']} | {tr['cycle']} | {tr['exit_code']}{' (timeout)' if tr['timed_out'] else ''} | "
                 f"{tr['passed']} | {tr['failed']} | {tr['errors']} | {tr['skipped']} | "
                 f"{(tr['duration_s'] or 0):.1f}s | `{tr['log_path']}` |")
    L.append("")
    if acc:
        last_cycle = max(a["cycle"] for a in acc)
        L += [f"### Acceptance criteria (latest cycle {last_cycle})", "", "| Criterion | Status | Evidence |", "|---|---|---|"]
        for a in acc:
            if a["cycle"] == last_cycle:
                L.append(f"| {a['criterion_id']}: {a['text'][:80]} | **{a['status']}** | {a['evidence'][:150]} |")
        L.append("")

    if findings:
        L += ["## Review findings and defect reports", "",
              "| Cycle | Source | Severity | Location | Title | Recommendation |", "|---|---|---|---|---|---|"]
        for f in findings:
            loc = f"{f['file'] or '-'}" + (f":{f['line']}" if f["line"] else "")
            L.append(f"| {f['cycle']} | {f['source']} | {f['severity']} | `{loc}` | {(f['title'] or '')[:80]} | "
                     f"{(f['recommendation'] or '').replace(chr(10), ' ')[:160]} |")
        L.append("")
    else:
        L += ["## Review findings", "", "None recorded.", ""]

    L += ["## Tokens and cost", ""] + cost_lines(store, run_id) + ["",
          "| Role | Model | Calls | Prompt tok | Completion tok | Cost | Cost source |", "|---|---|---|---|---|---|---|"]
    for r in by_role:
        L.append(f"| {r['role']} | `{r['model']}` | {r['calls']} | {r['prompt_tokens']:,} | {r['completion_tokens']:,} | "
                 f"{_money(r['cost_usd'])} | {r['cost_sources']} |")
    L.append("")

    L += ["## Human interventions", ""]
    L += [f"- {_ts(i['ts'])} **{i['kind']}**: {i['detail']}" for i in inter] or ["None."]
    L.append("")

    errs = [e for e in events if e["level"] in ("warn", "error")]
    L += ["## Warnings and errors", ""]
    L += [f"- {_ts(e['ts'])} [{e['level']}] {e['kind']} {e['task_key'] or ''}: {e['message'][:300]}" for e in errs] or ["None."]
    L.append("")

    L += ["## Known limitations of this result", ""]
    lim = []
    if run["state"] != "COMPLETED":
        lim.append(f"The run did not complete successfully (state {run['state']}): {run['state_reason']}")
    unv = [a for a in acc if a["status"] != "satisfied" and a["cycle"] == max((x["cycle"] for x in acc), default=0)]
    for a in unv:
        lim.append(f"Acceptance criterion {a['criterion_id']} is {a['status']}: {a['evidence']}")
    open_minor = [f for f in findings if f["severity"] in ("minor",) and f["cycle"] == run["repair_cycle"]]
    if open_minor:
        lim.append(f"{len(open_minor)} non-blocking (minor) review finding(s) were not addressed.")
    L += [f"- {x}" for x in lim] or ["- None identified by the automated gates. A human should still review the diff."]
    L.append("")

    L += ["## Timeline", "", "```"]
    for e in events:
        who = f"[{e['task_key']}/{e['role']}]" if e["role"] else (f"[{e['task_key']}]" if e["task_key"] else "")
        L.append(f"{datetime.fromtimestamp(e['ts']).strftime('%H:%M:%S')} {e['kind']:9} {who} {e['message'][:220]}")
    L += ["```", ""]
    return "\n".join(L)


def write_report(store, run_id: str, run_dir: Path) -> Path:
    path = run_dir / "report.md"
    path.write_text(build_report(store, run_id), encoding="utf-8")
    return path


def build_pr_body(store, run_id: str) -> str:
    run = store.get_run(run_id)
    tasks = store.tasks(run_id)
    tests = store.test_runs(run_id)
    acc = store.acceptance(run_id)
    findings = store.findings(run_id)
    inter = store.interventions(run_id)
    L = ["> **Draft PR generated autonomously by SwarmForge.** Review carefully before merging; "
         "SwarmForge never merges.", ""]
    if run["issue_number"]:
        L += [f"Addresses #{run['issue_number']}", ""]
    plan = json.loads(run["plan_json"]) if run["plan_json"] else {}
    if plan.get("summary"):
        L += ["## Approach", plan["summary"], ""]
    L += ["## Agent activity", "", "| Task | Role | Model | State |", "|---|---|---|---|"]
    for t in tasks:
        L.append(f"| {t['task_key']}: {(t['title'] or '')[:60]} | {t['role']} | `{t['model']}` | {t['state']} |")
    L += ["", "## Test evidence (executed by orchestrator)", ""]
    last = [t for t in tests if t["label"] != "baseline"][-1:] or tests[-1:]
    for tr in tests:
        if tr["label"] == "baseline" or tr in last:
            L.append(f"- `{tr['label']}`: exit {tr['exit_code']}, {tr['passed']} passed, {tr['failed']} failed, "
                     f"{tr['errors']} errors (`{tr['command']}`)")
    if acc:
        lc = max(a["cycle"] for a in acc)
        L += ["", "| Acceptance criterion | Status |", "|---|---|"]
        L += [f"| {a['criterion_id']}: {a['text'][:90]} | {a['status']} |" for a in acc if a["cycle"] == lc]
    L += ["", "## Review", ""]
    cur = [f for f in findings if f["cycle"] == run["repair_cycle"]]
    L += [f"- Findings in final review: {len(cur)} "
          f"({sum(1 for f in cur if f['severity'] in ('critical', 'major'))} blocking)",
          f"- Repair cycles: {run['repair_cycle']}", ""]
    minors = [f for f in cur if f["severity"] in ("minor", "info")]
    if minors:
        L += ["### Known limitations (non-blocking findings)"] + [
            f"- `{f['file']}`: {f['title']}" for f in minors[:15]] + [""]
    L += ["## Cost & supervision", ""] + cost_lines(store, run_id)
    L += [f"- Human interventions: {len(inter)}", "", f"Run id: `{run_id}`"]
    body = "\n".join(L)
    return body[:60_000]
