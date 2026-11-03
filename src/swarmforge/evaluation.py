"""Repeated, pinned comparisons with persisted progress and a shared spend cap."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import statistics
import time
from pathlib import Path

from .benchmark import evaluate_hidden, freeze_source, metrics
from .effective_config import effective_config
from .variants import _fresh_repo, load_suite
from .workspace import git


def summarize(rows: list[dict]) -> dict:
    groups = {}
    for mode in sorted({r["mode"] for r in rows}):
        selected = [r for r in rows if r["mode"] == mode]
        successful = [r for r in selected if r["final_state"] == "COMPLETED"
                      and r["hidden_acceptance"].get("total", 0) > 0
                      and r["hidden_acceptance"].get("passed") == r["hidden_acceptance"].get("total")]
        times = sorted(r["elapsed_s"] for r in selected)
        cost = sum(r["cost_usd"] for r in selected)
        groups[mode] = {"runs": len(selected), "successful": len(successful),
                        "delivery_rate": len(successful) / len(selected),
                        "median_elapsed_s": statistics.median(times),
                        "p95_elapsed_s": times[max(0, math.ceil(.95 * len(times)) - 1)],
                        "cost_usd": round(cost, 4),
                        "cost_per_success_usd": round(cost / len(successful), 4) if successful else None,
                        "repair_cycles": sum(r["repair_cycles"] for r in selected),
                        "human_interventions": sum(r["human_interventions"] for r in selected),
                        "unpriced_calls": sum(r["calls_without_cost"] for r in selected)}
    return groups


async def run_suite(cfg, store, suite_path, *, runs=3, modes=("single", "swarm"), budget=20.,
                    label="comparison", knowledge=False, same_repo="coordinate", limit=None,
                    echo=print, orchestrator_factory=None) -> dict:
    from .orchestrator import Orchestrator
    if runs < 1 or not modes or any(m not in {"single", "reviewed", "swarm"} for m in modes):
        raise ValueError("positive repeats and valid comparison modes are required")
    if not math.isfinite(budget) or budget <= 0 or (limit is not None and limit < 1):
        raise ValueError("budget and case limit must be positive")
    if same_repo not in {"serial", "coordinate", "parallel"}:
        raise ValueError("invalid repository scheduling policy")
    factory = orchestrator_factory or Orchestrator
    suite, cases = load_suite(suite_path)
    cases = cases[:limit] if limit else cases
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:60] or "comparison"
    root = cfg.workdir / "evaluations" / f"{safe_label}-{time.time_ns()}"
    root.mkdir(parents=True)
    c = copy.deepcopy(cfg)
    c.github.create_pr = False
    c.auto_merge.enabled = False
    c.self_heal.enabled = False
    c.self_heal.diagnose = False  # no separately budgeted diagnostic model calls in comparisons
    c.limits.same_repo = same_repo
    c.limits.one_run_per_repo = same_repo != "parallel"
    c.project.reuse_baseline = False  # each repeat includes the same baseline work
    c.knowledge.enabled = knowledge
    if knowledge:
        vault = root / "knowledge-source"
        if cfg.vault_path.exists():
            shutil.copytree(cfg.vault_path, vault)
        else:
            vault.mkdir()
    report = {"suite": suite, "budget_usd": budget, "config": effective_config(c),
              "knowledge": knowledge, "requested_runs": runs * len(cases) * len(modes),
              "rows": [], "pins": {}, "input_digests": {}, "summary": {}, "status": "running"}
    path = root / "results.json"
    report["report_path"] = str(path)
    echo(f"Comparison progress: {path}")

    def save():
        report["summary"] = summarize(report["rows"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        tmp.replace(path)

    save()
    spent = 0.
    try:
        for case in cases:
            inputs = root / "inputs" / case["name"]
            inputs.mkdir(parents=True)
            issue_path = inputs / "issue.md"
            shutil.copyfile(case["issue"], issue_path)
            hidden_path = inputs / "acceptance"
            shutil.copytree(case["acceptance"], hidden_path,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
            report["input_digests"][case["name"]] = {
                str(p.relative_to(inputs)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(inputs.rglob("*")) if p.is_file()}
            pinned = root / case["name"]
            if case.get("repo_template"):
                _fresh_repo(case["repo_template"], pinned)
            else:
                frozen = await freeze_source(c, case["repo"], pinned, base_ref=case.get("base_ref"))
                pinned = frozen.repo
            report["pins"][case["name"]] = git(pinned, "rev-parse", "HEAD").strip()
            for repeat in range(runs):
                # Alternate order so provider load does not always favor the same mode.
                for mode in (list(modes) if repeat % 2 == 0 else list(reversed(modes))):
                    remaining = budget - spent
                    if remaining < .05:
                        report["status"] = "budget_exhausted"
                        save()
                        return report
                    c.limits.budget_usd = min(cfg.limits.budget_usd, remaining)
                    if knowledge:
                        run_vault = root / f"knowledge-{len(report['rows'])}"
                        shutil.copytree(vault, run_vault)
                        c.knowledge.resolved_vault = run_vault
                    echo(f"{case['name']} repeat {repeat + 1}/{runs}: {mode}; ${remaining:.2f} remaining")
                    orch = factory(c, store, mode=mode, single_role=cfg.developer_roles[0],
                                   approve=lambda _: False, echo=None)
                    rid = await orch.execute(issue_file=str(issue_path), repo=str(pinned))
                    row = metrics(store, rid)
                    row.update(case=case["name"], repeat=repeat + 1,
                               base_commit=report["pins"][case["name"]])
                    row["hidden_acceptance"] = await evaluate_hidden(c, store.get_run(rid), hidden_path, mode)
                    spent += store.cost_summary(rid)["total"]
                    report["rows"].append(row)
                    report["spent_usd"] = round(spent, 4)
                    save()
                    if row["final_state"] == "CANCELLED":
                        report["status"] = "cancelled"
                        save()
                        return report
                    if row["calls_without_cost"]:
                        report["status"] = "unpriced_calls_hold"
                        save()
                        return report  # cannot enforce a dollar cap without prices
        report["status"] = "completed"
        save()
        store.record_benchmark(label, str(suite_path), report["rows"])
        return report
    except BaseException:
        report["status"] = "interrupted"
        save()
        raise
