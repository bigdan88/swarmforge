"""SWE-bench Lite integration: fetch, run, predictions (source-only patches), evaluate/record."""
import json
import subprocess
from pathlib import Path

import httpx

from swarmforge import swebench as SB
from swarmforge import variants as V
from swarmforge.db import Store


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _upstream(tmp_path) -> tuple[Path, str]:
    up = tmp_path / "upstream"
    (up / "pkg").mkdir(parents=True)
    (up / "pkg" / "core.py").write_text("def add(a, b):\n    return a - b\n")
    _git(up, "init", "-q", "-b", "main")
    _git(up, "add", "-A")
    _git(up, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=up, capture_output=True, text=True).stdout.strip()
    return up, sha


def test_fetch_stores_problem_statements_only(cfg):
    def handler(req):
        assert req.url.params["dataset"] == SB.DATASET
        n = int(req.url.params["length"])
        off = int(req.url.params["offset"])
        rows = [{"row": {"instance_id": f"o__r-{off + i}", "repo": "o/r", "base_commit": "abc",
                         "problem_statement": "bug", "patch": "GOLD", "test_patch": "TESTS",
                         "FAIL_TO_PASS": "[]"}} for i in range(n)]
        return httpx.Response(200, json={"rows": rows})
    path = SB.fetch(cfg, limit=3, client=httpx.Client(transport=httpx.MockTransport(handler)))
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["instance_id"] for r in rows] == ["o__r-0", "o__r-1", "o__r-2"]
    assert all("patch" not in r and "test_patch" not in r and "FAIL_TO_PASS" not in r for r in rows)


async def test_run_writes_source_only_predictions(cfg, tmp_path, monkeypatch):
    up, sha = _upstream(tmp_path)
    (SB.home(cfg) / "instances.jsonl").write_text(json.dumps(
        {"instance_id": "o__r-1", "repo": "o/r", "base_commit": sha, "problem_statement": "add() subtracts"}) + "\n")
    seen = {}

    class FakeOrch:
        def __init__(self, c, store, **kw):
            self.store, self.c = store, c
            seen["test_command"] = kw["test_command"]

        async def execute(self, issue_file, repo, base_branch):
            seen["issue"] = Path(issue_file).read_text()
            seen["base"] = base_branch
            run_dir = tmp_path / "run1"
            _git(tmp_path, "clone", "-q", "-b", base_branch, repo, str(run_dir / "repo"))
            r = run_dir / "repo"
            _git(r, "checkout", "-q", "-b", "swarmforge/task-1")
            (r / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n")
            (r / "tests").mkdir()
            (r / "tests" / "test_core.py").write_text("def test_add():\n    assert True\n")
            _git(r, "add", "-A")
            _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fix")
            self.store.create_run("run1", state="COMPLETED", run_dir=str(run_dir),
                                  integration_branch="swarmforge/task-1")
            return "run1"
    import swarmforge.orchestrator as om
    monkeypatch.setattr(om, "Orchestrator", FakeOrch)
    st = Store(cfg.db_path)
    clone = lambda url, dest: _git(tmp_path, "clone", "-q", str(up), str(dest))  # noqa: E731
    preds = await SB.run_instances(cfg, st, label="t1", clone=clone, echo=lambda m: None)
    p = json.loads(preds.read_text().splitlines()[0])
    assert p["instance_id"] == "o__r-1" and p["model_name_or_path"] == "swarmforge-base-swarm"
    assert "return a + b" in p["model_patch"] and "tests/test_core.py" not in p["model_patch"]
    assert "add() subtracts" in seen["issue"] and seen["base"] == "swebench-base/o__r-1"
    assert seen["test_command"] == SB.NO_TESTS
    # resumable: a second call skips finished instances
    await SB.run_instances(cfg, st, label="t1", clone=clone, echo=lambda m: None)
    assert len(preds.read_text().splitlines()) == 1


def test_evaluate_records_score_on_the_variant(cfg, tmp_path):
    st = Store(cfg.db_path)
    V.ensure_root(st)
    out = SB.home(cfg) / "t2"
    out.mkdir(parents=True)
    (out / "predictions.jsonl").write_text("{}\n")
    (out / "runs.json").write_text(json.dumps({"a": {"run_id": "ra", "state": "COMPLETED", "cost_usd": 1.5},
                                               "b": {"run_id": "rb", "state": "FAILED", "cost_usd": 0.5}}))
    (out / "swarmforge-base-swarm.t2.json").write_text(json.dumps(
        {"submitted_instances": 2, "resolved_ids": ["a"]}))
    msgs = []
    assert SB.evaluate(cfg, st, label="t2", variant="v0", echo=msgs.append) == 0
    assert "1/2 resolved (50%)" in msgs[-1]
    s = st.variant("v0")["scores"][-1]
    assert s["suite"] == "swebench-lite" and s["score"] == 0.5 and s["cost_usd"] == 2.0
    assert st.benchmarks()


def test_evaluate_without_harness_explains_what_to_run(cfg, tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_swebench(name, *a, **k):
        if name == "swebench":
            raise ImportError
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_swebench)
    out = SB.home(cfg) / "t3"
    out.mkdir(parents=True)
    (out / "predictions.jsonl").write_text("{}\n")
    msgs = []
    assert SB.evaluate(cfg, Store(cfg.db_path), label="t3", echo=msgs.append) == 2
    assert "pip install swebench" in msgs[0] and "run_evaluation" in msgs[0]
