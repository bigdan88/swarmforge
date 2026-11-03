"""Benchmark: the same issue attempted by (A) a single coding agent and (B) the swarm.

Fairness rules:
  * Both modes clone the same repository source, so they start from the same commit.
  * The same hidden acceptance tests (never visible to agents) are copied into a fresh checkout of
    each result branch and executed in the same sandbox, after the run has finished.
  * No PRs are created in benchmark mode.
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import Store
from .sandbox import make_sandbox
from .testing import run_tests
from .workspace import GitWorkspace, git

HIDDEN_DIR = "swarmforge_hidden_acceptance"


async def freeze_source(cfg: Config, source: str, destination: Path, *, base_ref: str | None = None) -> GitWorkspace:
    """Resolve/authenticate once, then keep an immutable checkout for every comparison mode."""
    from .github import get_token
    from .repos import auth_args, normalize_repo, preflight
    token = get_token(cfg.github)
    ref = normalize_repo(source)
    if base_ref:
        ref.branch = base_ref
    ref = await asyncio.to_thread(preflight, ref, token)
    frozen = GitWorkspace(destination, "benchmark-source", cfg.github.branch_prefix)
    auth = ["-c", "credential.helper=", *(auth_args(ref.source, token) if ref.use_auth else [])]
    await asyncio.to_thread(frozen.prepare, ref.source, None, ref.branch, auth)
    return frozen


async def evaluate_hidden(cfg: Config, run: dict, acceptance: Path, label: str) -> dict:
    run_dir = Path(run["run_dir"])
    repo = run_dir / "repo"
    if not repo.is_dir() or not run.get("integration_branch"):
        return {"status": "no result branch", "passed": 0, "total": 0}
    eval_dir = run_dir / "eval-hidden"
    if eval_dir.exists():
        git(repo, "worktree", "remove", "--force", str(eval_dir), check=False)
        shutil.rmtree(eval_dir, ignore_errors=True)
    git(repo, "worktree", "add", "--detach", str(eval_dir), run["integration_branch"])
    shutil.copytree(acceptance, eval_dir / HIDDEN_DIR)
    sandbox = make_sandbox(cfg.sandbox)
    if cfg.sandbox.dependency_install:
        from .deps import ensure_deps
        await ensure_deps(sandbox, eval_dir, network=cfg.sandbox.install_network,
                          timeout_s=cfg.sandbox.install_timeout_s, cache_dir=cfg.workdir / "pipcache")
    ev = await run_tests(sandbox, eval_dir, f"python -m pytest -q {HIDDEN_DIR}", run_dir / "artifacts",
                         f"hidden-{label}", cfg.sandbox.command_timeout_s)
    total = ev.passed + ev.failed + ev.errors
    return {"status": "executed", "passed": ev.passed, "failed": ev.failed, "errors": ev.errors,
            "total": total, "exit_code": ev.exit_code, "log": ev.log_path}


def metrics(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    c = store.cost_summary(run_id)
    tests = [t for t in store.test_runs(run_id) if t["label"] != "baseline"]
    last = tests[-1] if tests else None
    return {
        "run_id": run_id,
        "mode": run["mode"],
        "final_state": run["state"],
        "reason": run["state_reason"],
        "elapsed_s": round((run["finished_at"] or run["updated_at"]) - run["created_at"], 1),
        "cost_usd": round(c["total"], 4),
        "cost_provider_reported_usd": round(c["measured"], 4),
        "cost_estimated_usd": round(c["estimated"], 4),
        "calls_without_cost": c["unavailable_calls"],
        "total_tokens": c["total_tokens"],
        "llm_calls": c["calls"],
        "repair_cycles": run["repair_cycle"],
        "human_interventions": len(store.interventions(run_id)),
        "own_tests": f"{last['passed']} passed / {last['failed']} failed / {last['errors']} errors" if last else "-",
    }


async def run_benchmark(cfg: Config, store: Store, *, issue: str, repo: str, acceptance: str | None,
                        modes: list[str], single_role: str | None, label: str,
                        include_runs: list[str] | None = None) -> int:
    from .orchestrator import Orchestrator
    results = []
    acc_dir = Path(acceptance).resolve() if acceptance else None
    if acc_dir and not acc_dir.is_dir():
        print(f"acceptance directory not found: {acc_dir}")
        return 2
    frozen = None
    if modes:
        frozen = await freeze_source(cfg, repo, cfg.workdir / "benchmarks" / f"source-{datetime.now():%Y%m%d-%H%M%S-%f}")
        repo = str(frozen.repo)
    for mode in modes:
        c = copy.deepcopy(cfg)
        c.github.create_pr = False
        c.auto_merge.enabled = False
        c.knowledge.enabled = False  # identical starting knowledge for every mode; no notes written
        print(f"\n===== benchmark: {mode} =====")
        orch = Orchestrator(c, store, mode=mode, single_role=single_role, approve=lambda q: False)
        is_url = issue.startswith("http")
        run_id = await orch.execute(issue_url=issue if is_url else None, issue_file=None if is_url else issue,
                                    repo=repo)
        m = metrics(store, run_id)
        m["base_commit"] = frozen.base_sha
        if acc_dir:
            m["hidden_acceptance"] = await evaluate_hidden(c, store.get_run(run_id), acc_dir, mode)
        results.append(m)

    # Previously finished runs of the same issue (e.g. an earlier swarm run) can be added to the
    # comparison instead of paying for them again. They are evaluated with the same hidden tests.
    for rid in include_runs or []:
        run = store.get_run(rid)
        if not run:
            print(f"run not found: {rid}")
            return 2
        m = metrics(store, run["id"])
        m["mode"] = f"{run['mode']} (earlier run)"
        if acc_dir:
            m["hidden_acceptance"] = await evaluate_hidden(cfg, run, acc_dir, f"{run['mode']}-{run['id']}")
        results.append(m)

    store.record_benchmark(label or datetime.now().isoformat(timespec="seconds"), issue, results)
    md = render_benchmark(issue, results)
    out = cfg.workdir / "benchmarks" / f"bench-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print("\n" + md + f"\n(saved to {out})")
    return 0


def render_benchmark(issue: str, results: list[dict]) -> str:
    rows = [
        ("Final state", "final_state"), ("Elapsed (s)", "elapsed_s"), ("Cost total (USD)", "cost_usd"),
        ("  provider-reported", "cost_provider_reported_usd"), ("  estimated", "cost_estimated_usd"),
        ("Calls without cost info", "calls_without_cost"), ("Total tokens", "total_tokens"),
        ("LLM calls", "llm_calls"), ("Corrective iterations", "repair_cycles"),
        ("Human interventions", "human_interventions"), ("Own test suite (last run)", "own_tests"),
    ]
    head = "| Metric | " + " | ".join(r["mode"] for r in results) + " |"
    L = [f"# Benchmark: single agent vs swarm", "", f"Issue: `{issue}`", "", head,
         "|---|" + "---|" * len(results)]
    for name, key in rows:
        L.append(f"| {name} | " + " | ".join(str(r.get(key, "-")) for r in results) + " |")
    if any("hidden_acceptance" in r for r in results):
        cells = []
        for r in results:
            h = r.get("hidden_acceptance", {})
            cells.append(f"{h.get('passed', 0)}/{h.get('total', 0)}" if h.get("status") == "executed" else h.get("status", "-"))
        L.append("| Hidden acceptance tests passed | " + " | ".join(cells) + " |")
    L.append("| Run id | " + " | ".join(r["run_id"] for r in results) + " |")
    L += ["", "Notes: costs marked provider-reported come from DeepInfra's `usage.estimated_cost`; "
          "estimated costs are token counts x published per-token prices. Single runs are noisy - "
          "repeat the benchmark several times before drawing conclusions."]
    return "\n".join(L)
