"""Offline end-to-end tests: real orchestrator, git worktrees, scheduler, sandbox and pytest
execution; only the model API is replaced by scripted responses."""
import shutil
import subprocess
from pathlib import Path

import pytest

from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator
from swarmforge.report import parallel_intervals
from swarmforge.sandbox import DockerSandbox

from . import fake_team
from .conftest import ISSUE, FakeProvider


def _provider():
    return FakeProvider({
        "qwen-fake": fake_team.planner,
        "deepseek-fake": fake_team.developer,
        "kimi-fake": fake_team.developer,
        "minimax-fake": fake_team.tester,
        "glm-fake": fake_team.Reviewer(),
    })


async def _run(cfg, sample_repo, sandbox=None, mode="swarm"):
    store = Store(cfg.db_path)
    fake = _provider()
    orch = Orchestrator(cfg, store, mode=mode, echo=None, llm_transport=fake.transport, sandbox=sandbox)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    return store, run_id, fake


async def test_full_swarm_cycle_local(cfg, sample_repo):
    store, run_id, fake = await _run(cfg, sample_repo)
    run = store.get_run(run_id)
    states = [e["data"] for e in store.events(run_id) if e["kind"] == "state"]
    path = [s.split('"to": "')[1].split('"')[0] for s in states]
    assert run["state"] == "COMPLETED", (run["state_reason"], path)
    # full lifecycle incl. one repair cycle driven by a major review finding
    assert path[:9] == ["REPOSITORY_INSPECTION", "PLANNING", "TASK_VALIDATION", "TASK_EXECUTION",
                        "INTEGRATION", "TESTING", "CODE_REVIEW", "FIX_REQUIRED", "TASK_VALIDATION"]
    assert path[-2:] == ["APPROVED", "COMPLETED"]
    assert run["repair_cycle"] == 1

    tasks = {t["task_key"]: t for t in store.tasks(run_id)}
    assert {k: tasks[k]["state"] for k in ("T1", "T2", "T3", "F1")} == dict.fromkeys(("T1", "T2", "T3", "F1"), "INTEGRATED")
    assert tasks["T1"]["model"] == "deepseek-fake" and tasks["T2"]["model"] == "kimi-fake"
    # T1 and T2 are independent -> ran concurrently; T3 started after T1 finished
    assert parallel_intervals([tasks["T1"], tasks["T2"]]) == 2
    assert tasks["T3"]["started_at"] >= tasks["T1"]["ended_at"]

    # planner received validation feedback and fixed its plan
    assert any("REJECTED" in m.get("content", "") for r in fake.requests for m in r["messages"] if m["role"] == "tool")

    # deterministic test evidence; tester's edit of non-test file was reverted
    tr = store.test_runs(run_id)
    assert tr[0]["label"] == "baseline" and tr[0]["passed"] == 13
    assert tr[-1]["exit_code"] == 0 and tr[-1]["passed"] > 13 and tr[-1]["failed"] == 0
    repo = cfg.workdir / "runs" / run_id / "repo"
    assert "tester must not edit" not in (repo / "inventory" / "models.py").read_text()
    assert (repo / "tests" / "test_acceptance_swarmforge.py").exists()
    acc = [a for a in store.acceptance(run_id) if a["cycle"] == 1]
    assert {a["criterion_id"]: a["status"] for a in acc} == {"AC1": "satisfied", "AC2": "satisfied", "AC3": "satisfied"}

    # the result branch really contains the work, and the user's source repo is untouched
    log = subprocess.run(["git", "log", "--oneline", run["integration_branch"]], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "Integrate T1" in log and "Integrate F1" in log
    assert not (sample_repo / "inventory" / "exporters.py").exists()

    # telemetry
    cost = store.cost_summary(run_id)
    assert cost["calls"] == len(fake.requests) and cost["measured"] > 0 and cost["estimated"] == 0
    report = (cfg.workdir / "runs" / run_id / "report.md").read_text()
    assert "Measured maximum concurrently running coding tasks: **2**" in report
    assert "provider" in report and "FIX_REQUIRED" in report

    # Zettelkasten: librarian notes (linked to each other), run log and repo hub were written
    from swarmforge.knowledge import Vault
    vault = Vault(cfg.vault_path)
    notes = {n.title: n for n in vault.notes()}
    pattern = notes["Exporters must wrap every OSError in ExportError"]
    assert pattern.type == "pattern" and pattern.repo == "sample" and pattern.source == "agent:planner/qwen-fake"
    assert notes["README must document every CLI flag"].stem in pattern.links
    assert any(n.type == "run-log" for n in notes.values()) and any(n.type == "hub" for n in notes.values())


async def test_knowledge_is_retrieved_by_next_run(cfg, sample_repo):
    from swarmforge.knowledge import Vault
    Vault(cfg.vault_path).write("Inventory CSV export needs csv module quoting", "Names with commas broke "
                                "naive joins before; always use csv.writer for export.", type="pitfall",
                                repo="sample", tags=["csv"])
    store, run_id, fake = await _run(cfg, sample_repo)
    ev = [e for e in store.events(run_id) if e["kind"] == "knowledge" and "retrieved" in e["message"]]
    assert ev and "csv module quoting" in ev[0]["message"]
    planner_first = next(r for r in fake.requests if r["model"] == "qwen-fake")
    assert "<knowledge_base>" in planner_first["messages"][1]["content"]
    dev = next(r for r in fake.requests if r["model"] == "deepseek-fake")
    assert "csv module quoting" in dev["messages"][1]["content"]


async def test_repair_limit_fails_gracefully(cfg, sample_repo):
    cfg.limits.max_repair_cycles = 0
    store, run_id, _ = await _run(cfg, sample_repo)
    run = store.get_run(run_id)
    assert run["state"] == "FAILED" and "repair limit" in run["state_reason"]
    assert "--format undocumented" in (cfg.workdir / "runs" / run_id / "report.md").read_text()


async def test_budget_exceeded(cfg, sample_repo):
    cfg.limits.budget_usd = 0.0005  # each fake call reports $0.0001
    store, run_id, _ = await _run(cfg, sample_repo)
    assert store.get_run(run_id)["state"] == "BUDGET_EXCEEDED"


async def test_cancel(cfg, sample_repo):
    store = Store(cfg.db_path)
    fake = _provider()
    orig = fake.handlers["qwen-fake"]

    def planner_then_cancel(body):
        store.request_cancel(orch.run_id)
        return orig(body)
    fake.handlers["qwen-fake"] = planner_then_cancel
    orch = Orchestrator(cfg, store, echo=None, llm_transport=fake.transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    assert store.get_run(run_id)["state"] == "CANCELLED"
    assert store.interventions(run_id)[0]["kind"] == "cancel"


async def test_single_agent_mode(cfg, sample_repo):
    cfg.knowledge.enabled = False  # as in benchmarks: no knowledge in, no librarian out
    store = Store(cfg.db_path)

    def single_dev(body):
        # one agent does everything: reuse the scripted T1 then T3 then T2 steps
        n = sum(1 for m in body["messages"] if m["role"] == "assistant")
        seq = []
        for t in ("T1", "T3", "T2"):
            k = 0
            while True:
                b = {"messages": [{"role": "system"}, {"role": "user", "content": f"## Your task {t}"}]
                     + [{"role": "assistant"}] * k}
                m = fake_team.developer(b)
                if m["tool_calls"][0]["function"]["name"] in ("run_command", "finish"):
                    break
                seq.append(m)
                k += 1
        seq.append(fake_team.developer({"messages": [{"role": "system"}, {"role": "user", "content": "## Your task T1"}]
                                                     + [{"role": "assistant"}] * 99}))
        return seq[min(n, len(seq) - 1)]
    fake = FakeProvider({"deepseek-fake": single_dev})
    orch = Orchestrator(cfg, store, mode="single", echo=None, llm_transport=fake.transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert {r["model"] for r in fake.requests} == {"deepseek-fake"}
    assert len(store.tasks(run_id)) == 1


docker_ok = shutil.which("docker") and subprocess.run(
    ["docker", "image", "inspect", "swarmforge-sandbox:latest"], capture_output=True).returncode == 0


@pytest.mark.skipif(not docker_ok, reason="docker sandbox image not available")
async def test_full_swarm_cycle_docker(cfg, sample_repo):
    cfg.project.test_command = "python -m pytest -q -p no:cacheprovider"
    cfg.sandbox.mode = "docker"
    store, run_id, _ = await _run(cfg, sample_repo, sandbox=DockerSandbox(cfg.sandbox, run_label="pytest"))
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert store.test_runs(run_id)[-1]["passed"] > 13


@pytest.mark.skipif(not docker_ok, reason="docker sandbox image not available")
async def test_docker_sandbox_isolation(tmp_path):
    from swarmforge.config import SandboxConfig
    sb = DockerSandbox(SandboxConfig())
    (tmp_path / "f.txt").write_text("hi")
    r = await sb.run(tmp_path, "cat f.txt; id -u; env | grep -ci token || true; "
                               "python -c \"import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=3)\"")
    assert "hi" in r.output and r.exit_code != 0 and "Network is unreachable" in r.output or "urlopen error" in r.output
    r = await sb.run(tmp_path, "touch /etc/x")
    assert r.exit_code != 0  # read-only root fs
    r = await sb.run(tmp_path, "sleep 30", timeout_s=2)
    assert r.timed_out


async def test_empty_repository_is_blocked_with_guidance(cfg, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=empty, check=True)
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None, llm_transport=_provider().transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(empty))
    run = store.get_run(run_id)
    assert run["state"] == "BLOCKED" and "no commits yet" in run["state_reason"]
    assert "git commit" in run["state_reason"]
    assert store.cost_summary(run_id)["calls"] == 0


async def test_local_delivery_and_project_profile(cfg, sample_repo):
    import sys
    (sample_repo / "swarmforge.project.yml").write_text(
        f"test_command: {sys.executable} -m pytest -q -p no:cacheprovider tests\n"
        "description: sample\nnetwork: bridge\n")
    subprocess.run(["git", "add", "-A"], cwd=sample_repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "profile"],
                   cwd=sample_repo, check=True)
    store, run_id, _ = await _run(cfg, sample_repo)
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    branch = run["integration_branch"]
    assert f"delivered to branch {branch}" in run["state_reason"]
    # the branch now exists in the user's repo, the checkout is untouched
    out = subprocess.run(["git", "log", "--oneline", branch], cwd=sample_repo, capture_output=True, text=True)
    assert "Integrate T1" in out.stdout
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=sample_repo,
                          capture_output=True, text=True).stdout.strip()
    assert head == "main" and not (sample_repo / "inventory" / "exporters.py").exists()
    msgs = [e["message"] for e in store.events(run_id)]
    assert any("from swarmforge.project.yml" in m and " tests`" in m for m in msgs)
    assert any("ignoring unsupported keys" in m and "network" in m for m in msgs)
    assert any(t["command"].endswith("tests --junitxml=/artifacts/junit-cycle1.xml") for t in store.test_runs(run_id))


async def test_unbuilt_profile_image_is_blocked(cfg, sample_repo):
    cfg.sandbox.mode = "docker"
    store = Store(cfg.db_path)
    from swarmforge.sandbox import LocalSandbox
    orch = Orchestrator(cfg, store, echo=None, llm_transport=_provider().transport,
                        sandbox_image="definitely-not-built-image:42")
    orch._sandbox_override = None
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon not available")
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "BLOCKED" and "not built" in run["state_reason"]


async def test_run_with_repo_given_as_url_with_branch(cfg, sample_repo, tmp_path):
    # a file:// URL behaves like a remote; the run clones it via the normalized reference
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None, llm_transport=_provider().transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=f"  {sample_repo.resolve().as_uri()}  ")
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert run["base_branch"] == "main"


async def test_unreachable_repo_is_blocked_not_crashed(cfg, tmp_path):
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, echo=None, llm_transport=_provider().transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=(tmp_path / "gone.git").resolve().as_uri())
    run = store.get_run(run_id)
    assert run["state"] == "BLOCKED" and "cannot access repository" in run["state_reason"]


def _make_wheel(dest: Path) -> Path:
    """Build a tiny pure-Python wheel by hand (no network, no build backend needed)."""
    import zipfile
    whl = dest / "tinydep-1.0-py3-none-any.whl"
    files = {"tinydep/__init__.py": "VALUE = 42\n",
             "tinydep-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: tinydep\nVersion: 1.0\n",
             "tinydep-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = "".join(f"{k},,\n" for k in files) + "tinydep-1.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(whl, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
        z.writestr("tinydep-1.0.dist-info/RECORD", record)
    return whl


async def test_project_dependencies_are_installed_for_tests(cfg, sample_repo):
    vendor = sample_repo / "vendor"
    vendor.mkdir()
    _make_wheel(vendor)
    (sample_repo / "requirements.txt").write_text("./vendor/tinydep-1.0-py3-none-any.whl\n")
    (sample_repo / "tests" / "test_dep.py").write_text("import tinydep\n\ndef test_dep():\n    assert tinydep.VALUE == 42\n")
    subprocess.run(["git", "add", "-A"], cwd=sample_repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "dep"], cwd=sample_repo, check=True)
    import sys
    cfg.project.test_command = f"{Path(sys.executable).as_posix()} -m pytest -q -p no:cacheprovider"
    store, run_id, _ = await _run(cfg, sample_repo)
    run = store.get_run(run_id)
    base = store.test_runs(run_id)[0]
    assert base["label"] == "baseline" and base["exit_code"] == 0 and base["passed"] == 14, base
    assert any(e["kind"] == "deps" and "installed dependencies from requirements.txt" in e["message"]
               for e in store.events(run_id))
    assert run["state"] == "COMPLETED", run["state_reason"]
    # installed packages never end up in commits
    repo = cfg.workdir / "runs" / run_id / "repo"
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout
    assert ".swarmforge/pydeps" not in tracked


async def test_environment_problems_block_before_any_work(cfg, sample_repo, monkeypatch):
    store = Store(cfg.db_path)
    monkeypatch.delenv("FAKE_KEY")
    cfg.sandbox.mode = "docker"
    cfg.sandbox.docker_binary = "false"   # a "docker" that never answers properly
    orch = Orchestrator(cfg, store, echo=None, llm_transport=_provider().transport)
    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(sample_repo))
    run = store.get_run(run_id)
    assert run["state"] == "BLOCKED"
    assert "FAKE_KEY" in run["state_reason"] and "Docker is not responding" in run["state_reason"]
    assert store.cost_summary(run_id)["calls"] == 0
