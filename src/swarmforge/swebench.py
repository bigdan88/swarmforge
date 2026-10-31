"""SWE-bench Lite: score SwarmForge on the standard "resolve this GitHub issue" benchmark.

  swarmforge swebench fetch --limit 10          # download task instances (problem statements only)
  swarmforge swebench run --label try1          # run the swarm on each, write predictions.jsonl
  swarmforge swebench eval --label try1         # official harness -> resolved / total

Agents only see the issue text (`problem_statement`), never the gold patch or the hidden tests.
The patch SwarmForge produces (source files only; test files are left out because the harness applies
its own tests) is written in the official predictions format, and the official SWE-bench harness
(`pip install swebench`, needs Docker; on Windows run it inside WSL) decides what counts as resolved.

Limitations, stated plainly: SwarmForge's sandbox does not reproduce each project's exact test
environment, so agents often can't run the project's tests while working; the result is a fair
end-to-end score of the patch, not of the agents' own test runs. Every instance is a full run (minutes
and real money), so start with a handful.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import Config
from .db import Store
from .workspace import git, git_rc

DATASET = "princeton-nlp/SWE-bench_Lite"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
NO_TESTS = 'python -c "print(\'SWE-bench: the official harness runs the tests\')"'
TEST_EXCLUDES = [":(glob,exclude)**/tests/**", ":(glob,exclude)tests/**", ":(glob,exclude)**/test_*.py",
                 ":(glob,exclude)**/*_tests.py", ":(glob,exclude)**/testing/**", ":(glob,exclude)test/**",
                 ":(glob,exclude)**/test/**"]


def home(cfg: Config) -> Path:
    p = cfg.workdir / "swebench"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------------------------------------------------ fetch
def fetch(cfg: Config, *, limit: int = 10, offset: int = 0, split: str = "test",
          client: httpx.Client | None = None) -> Path:
    client = client or httpx.Client(timeout=60)
    rows, got = [], 0
    while got < limit:
        n = min(100, limit - got)
        r = client.get(ROWS_URL, params={"dataset": DATASET, "config": "default", "split": split,
                                         "offset": offset + got, "length": n})
        r.raise_for_status()
        batch = [x["row"] for x in r.json().get("rows", [])]
        if not batch:
            break
        rows += batch
        got += len(batch)
    keep = ("instance_id", "repo", "base_commit", "problem_statement", "version", "created_at")
    out = home(cfg) / "instances.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({k: row.get(k) for k in keep}) + "\n")   # no gold patch / tests stored
    return out


def load_instances(cfg: Config, ids: list[str] | None = None, limit: int | None = None) -> list[dict]:
    path = home(cfg) / "instances.jsonl"
    if not path.is_file():
        raise FileNotFoundError("no instances yet: run `swarmforge swebench fetch` first")
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if ids:
        items = [i for i in items if i["instance_id"] in set(ids)]
    return items[:limit] if limit else items


# ------------------------------------------------------------------ run
def prepare_repo(cfg: Config, inst: dict, clone=None) -> tuple[Path, str]:
    """A cached clone of the instance's repository with a branch at its base commit."""
    path = home(cfg) / "repos" / inst["repo"].replace("/", "__")
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        (clone or _clone)(f"https://github.com/{inst['repo']}.git", path)
        git(path, "config", "user.name", "SwarmForge Bench")
        git(path, "config", "user.email", "bench@localhost")
    branch = f"swebench-base/{inst['instance_id']}"
    rc, _ = git_rc(path, "cat-file", "-e", f"{inst['base_commit']}^{{commit}}")
    if rc != 0:
        git(path, "fetch", "--quiet", "origin", inst["base_commit"])
    git(path, "branch", "-f", branch, inst["base_commit"])
    git(path, "checkout", "-q", branch)
    return path, branch


def _clone(url: str, dest: Path) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", url, str(dest)], check=True)


def issue_text(inst: dict) -> str:
    return (f"# SWE-bench {inst['instance_id']}\n\n{inst['problem_statement'].strip()}\n\n"
            "---\nResolve this issue in the repository with a minimal, correct change to the source code. "
            "Hidden tests from the project will be run afterwards; keep existing behaviour working.\n")


def model_patch(run: dict, base_commit: str) -> str:
    repo = Path(run["run_dir"]) / "repo"
    if not run.get("integration_branch") or not (repo / ".git").exists():
        return ""
    return git(repo, "diff", base_commit, run["integration_branch"], "--", ".", *TEST_EXCLUDES, check=False)


async def run_instances(cfg: Config, store: Store, *, label: str, ids: list[str] | None = None,
                        limit: int | None = None, mode: str = "swarm", budget: float | None = None,
                        test_command: str | None = None, echo: Callable[[str], None] = print,
                        orchestrator_kwargs: dict | None = None, clone=None) -> Path:
    from .orchestrator import Orchestrator
    out_dir = home(cfg) / label
    out_dir.mkdir(parents=True, exist_ok=True)
    preds_path, meta_path = out_dir / "predictions.jsonl", out_dir / "runs.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    c = copy.deepcopy(cfg)
    c.github.create_pr = False
    c.knowledge.enabled = False
    c.limits.one_run_per_repo = False
    if budget:
        c.limits.budget_usd = float(budget)
    model_name = f"swarmforge-{c.active_variant or 'base'}-{mode}"
    for inst in load_instances(cfg, ids, limit):
        iid = inst["instance_id"]
        if iid in meta and meta[iid].get("patch_lines") is not None:
            echo(f"{iid}: already done ({meta[iid]['state']}), skipping")
            continue
        echo(f"{iid}: preparing {inst['repo']} @ {inst['base_commit'][:8]}")
        repo, branch = prepare_repo(cfg, inst, clone)
        issue = out_dir / "issues" / f"{iid}.md"
        issue.parent.mkdir(exist_ok=True)
        issue.write_text(issue_text(inst), encoding="utf-8")
        orch = Orchestrator(c, store, mode=mode, approve=lambda q: True, echo=None,
                            test_command=test_command or NO_TESTS, **(orchestrator_kwargs or {}))
        rid = await orch.execute(issue_file=str(issue), repo=str(repo), base_branch=branch)
        run = store.get_run(rid)
        patch = model_patch(run, inst["base_commit"])
        meta[iid] = {"run_id": rid, "state": run["state"], "reason": run["state_reason"],
                     "cost_usd": round(store.cost_summary(rid)["total"] or 0, 4),
                     "elapsed_s": round((run["finished_at"] or time.time()) - run["created_at"]),
                     "patch_lines": patch.count("\n")}
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        with preds_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"instance_id": iid, "model_name_or_path": model_name, "model_patch": patch}) + "\n")
        echo(f"{iid}: {run['state']}, patch {meta[iid]['patch_lines']} lines, ${meta[iid]['cost_usd']:.2f}")
    echo(f"predictions: {preds_path}")
    return preds_path


# ------------------------------------------------------------------ evaluate
def harness_command(preds: Path, run_id: str, workers: int = 2) -> list[str]:
    return [sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name", DATASET,
            "--predictions_path", str(preds), "--max_workers", str(workers), "--run_id", run_id]


def parse_report(out_dir: Path, run_id: str) -> dict | None:
    for p in sorted(out_dir.glob(f"*.{run_id}.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if "resolved_ids" in d or "resolved_instances" in d:
            return d
    return None


def record(cfg: Config, store: Store, label: str, report: dict, variant: str | None = None) -> dict:
    meta_path = home(cfg) / label / "runs.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    resolved = set(report.get("resolved_ids") or [])
    total = report.get("submitted_instances") or len(meta) or 0
    result = {"suite": "swebench-lite", "label": label, "resolved": len(resolved), "total": total,
              "score": round(len(resolved) / total, 3) if total else 0.0,
              "cost_usd": round(sum(m.get("cost_usd") or 0 for m in meta.values()), 4),
              "runs": [m["run_id"] for m in meta.values()],
              "cases": [{"case": iid, "run_id": m["run_id"], "state": m["state"],
                         "score": 1.0 if iid in resolved else 0.0} for iid, m in meta.items()],
              "ts": time.time()}
    store.record_benchmark(f"swebench:{label}", "SWE-bench Lite", [result])
    vid = variant
    if not vid:
        for m in meta.values():
            vid = (store.get_run(m["run_id"]) or {}).get("variant_id")
            if vid:
                break
    if vid and store.variant(vid):
        v = store.variant(vid)
        store.variant_update(vid, scores=v["scores"] + [result], status="scored")
        result["variant"] = vid
    return result


def evaluate(cfg: Config, store: Store, *, label: str, workers: int = 2, variant: str | None = None,
             echo: Callable[[str], None] = print, runner=subprocess.run) -> int:
    out_dir = home(cfg) / label
    preds = out_dir / "predictions.jsonl"
    if not preds.is_file():
        echo(f"no predictions for '{label}': run `swarmforge swebench run --label {label}` first")
        return 2
    report = parse_report(out_dir, label)
    if report is None:
        try:
            import swebench  # noqa: F401
        except ImportError:
            echo("The official harness is not installed. Install it with `pip install swebench` (needs Docker). "
                 "On Windows run the evaluation inside WSL:\n  " + " ".join(harness_command(preds, label, workers))
                 + f"\nthen run `swarmforge swebench eval --label {label}` again to record the result.")
            return 2
        if sys.platform == "win32":
            echo("note: the SWE-bench harness officially supports Linux/macOS; if it fails, run it in WSL.")
        echo("running the official SWE-bench harness (builds per-project Docker images; slow the first time)")
        rc = runner(harness_command(preds, label, workers), cwd=str(out_dir)).returncode
        report = parse_report(out_dir, label)
        if report is None:
            echo(f"the harness did not produce a report (exit {rc})")
            return 1
    res = record(cfg, store, label, report, variant)
    echo(f"SWE-bench Lite '{label}': {res['resolved']}/{res['total']} resolved ({res['score']:.0%}), "
         f"${res['cost_usd']:.2f}" + (f" — recorded on variant {res['variant']}" if res.get("variant") else ""))
    return 0
