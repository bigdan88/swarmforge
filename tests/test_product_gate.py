"""Integration with project evidence and the existing repair cycle; fake model adapters only."""
import json
from types import SimpleNamespace

import pytest

from swarmforge import product_gate
from swarmforge.db import Store
from swarmforge.plan import Criterion, Plan, Task
from swarmforge.projects import service
from swarmforge.projects.spec import ProjectError, fixed, render, validate
from swarmforge.projects.store import Projects
from swarmforge.state import RunAborted, RunState
from swarmforge.verification import contract_hash, enforce_evidence

from .test_product_verification import ProbeSandbox, app as application_fixture, check, finish, unit_result
from .test_projects import definition

app = application_fixture


def owner(app, tmp_path):
    root = tmp_path / "run"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    store = Store(tmp_path / "state.db")
    store.create_run("run")
    async def dependencies(*args): return "none"
    return SimpleNamespace(store=store, run_id="run", run_dir=root, artifacts=artifacts,
                           ws=SimpleNamespace(repo=app, head=lambda path: "tested-sha"),
                           cfg=SimpleNamespace(sandbox=SimpleNamespace(command_timeout_s=30)),
                           sandbox=ProbeSandbox(), log=lambda *a, **kw: None, _ensure_deps=dependencies)


@pytest.mark.parametrize("with_application_check", [False, True])
def test_freeze_creates_missing_artifacts_after_cached_baseline(app, tmp_path, with_application_check):
    host = owner(app, tmp_path)
    # Resumed/self-healing runs may reuse the baseline test result, so no test
    # runner has created the new run's artifact directory before planning.
    host.artifacts = tmp_path / "resumed-run" / "artifacts"
    assert not host.artifacts.exists()
    plan = Plan("resume", [], [Criterion("AC1", "Output", "product" if with_application_check else "unit")],
                [check()] if with_application_check else [])
    try:
        product_gate.configure(host)
        product_gate.freeze(host, plan)
        saved = json.loads((host.artifacts / "verification-contract.json").read_text())
        assert saved["hash"] == contract_hash(host._verification_checks)
        assert json.loads(host.store.kv_get(host._verification_key)) == saved["checks"]
        retained = host.artifacts / "existing-evidence.txt"
        retained.write_text("keep this evidence")
        product_gate.freeze(host, plan)
        assert retained.read_text() == "keep this evidence"
    finally:
        host.store.close()


def test_cached_baseline_then_contract_freeze_is_safe_without_prior_artifacts(app, tmp_path, monkeypatch):
    import hashlib
    from swarmforge import orchestrator
    host = owner(app, tmp_path)
    host.artifacts = tmp_path / "cached-baseline-run" / "artifacts"
    host.ws.base_sha = "unchanged-base"
    host.ws.file_tree = lambda: ["indexer.py"]
    host.cfg.project = SimpleNamespace(reuse_baseline=True, test_command="pytest")
    host.cfg.sandbox.image = "test-image"
    host._record_tests = lambda ev, cycle: None
    cache_key = "baseline:" + hashlib.sha1(b"unchanged-base|pytest|test-image").hexdigest()[:20]
    host.store.kv_set(cache_key, json.dumps({"command": "pytest", "passed": 1, "failed": 0,
                                          "errors": 0, "skipped": 0, "junit_found": True, "run_id": "previous"}))
    async def direct(function, *args, **kwargs): return function(*args, **kwargs)
    async def unexpected_test_run(*args, **kwargs): raise AssertionError("The cached baseline must be reused")
    monkeypatch.setattr(orchestrator.asyncio, "to_thread", direct)
    monkeypatch.setattr(orchestrator, "run_tests", unexpected_test_run)
    try:
        finish(orchestrator.Orchestrator._inspect_repo(host))
        assert host.baseline.ok and not host.artifacts.exists()
        product_gate.configure(host)
        product_gate.freeze(host, Plan("resume", [], [Criterion("AC1", "Unit behavior")]))
        assert (host.artifacts / "verification-contract.json").is_file()
    finally:
        host.store.close()


def test_failed_application_check_repeats_unchanged_after_implementation_repair(app, tmp_path):
    host = owner(app, tmp_path)
    product_gate.configure(host)
    plan = Plan("index", [Task("T1", "index", "d", "developer_backend")],
                [Criterion("AC1", "Input is searchable", "product")], [check("empty")])
    product_gate.freeze(host, plan)
    original_hash = contract_hash(host._verification_checks)
    evidence, sha = finish(product_gate.execute(host, 0))
    result = enforce_evidence(unit_result(), plan.acceptance_criteria, evidence, commit_sha=sha)
    assert result[0]["status"] == "failed"
    problem = product_gate.findings(host)[0]
    assert problem["kind"] == "verification" and "verification-contract.json" in problem["detail"]
    assert "Completed successfully" in problem["detail"]
    assert "min_count" in problem["detail"]
    # Repair the actual implementation; the accepted command and expected result stay fixed.
    code = (app / "indexer.py").read_text()
    (app / "indexer.py").write_text(code.replace('[] if args.mode == "empty" else', '[] if False else'))
    evidence, sha = finish(product_gate.execute(host, 1))
    result = enforce_evidence(unit_result(), plan.acceptance_criteria, evidence, commit_sha=sha)
    assert result[0]["status"] == "satisfied"
    assert contract_hash(host._verification_checks) == original_hash
    assert not product_gate.findings(host)
    assert json.loads(host.store.kv_get("verification_results:run:1"))[0]["status"] == "passed"
    host.store.close()


def test_missing_environment_stops_code_repair_with_blocked_state():
    from .test_product_verification import evidence
    with pytest.raises(RunAborted) as exc:
        product_gate.unavailable([evidence(status="blocked")])
    assert exc.value.state == RunState.BLOCKED


def recorded_project(tmp_path, level="unit", requirement_id="M1C1", text="paraphrased description", cycle=1):
    store = Store(tmp_path / "project.db")
    ps = Projects(store)
    project = ps.create()
    ps.draft(project["id"], definition())
    service.approve(store, project["id"])
    store.create_run("merged")
    item = store.queue_add("feature", "body", "owner/inventory", project_id=project["id"], milestone=0)
    store.queue_update(item, status="done", project_processed=1, merged_sha="merge-sha", run_id="merged")
    store.record_test_run("merged", cycle=1, label="cycle1", command="pytest", exit_code=0, passed=1)
    store.record_acceptance("merged", cycle=cycle, criterion_id="AC1", requirement_id=requirement_id, text=text,
                            status="satisfied", evidence="Output observed", evidence_level=level,
                            required_level="product", commit_sha="tested-sha")
    return store, ps, project["id"]


def test_project_gate_rejects_old_mocked_evidence_even_when_words_match(tmp_path):
    text = definition()["milestones"][0]["checks"][0]
    store, ps, pid = recorded_project(tmp_path, text=text, requirement_id=None)
    result = service.verify_checks(store, ps.get(pid))[0]
    assert result["status"] == "needs you" and result["required_level"] == "product"
    assert "recorded evidence is unit" in result["explanation"]
    store.close()


def test_stable_ids_allow_paraphrased_text_without_duplicate_evidence_carriers(tmp_path):
    store, ps, pid = recorded_project(tmp_path, level="product")
    result = service.verify_checks(store, ps.get(pid))[0]
    assert result["status"] == "passed" and result["id"] == "M1C1"
    assert result["evidence"][0]["tested_sha"] == "tested-sha"
    store.close()


def test_older_repair_cycle_evidence_cannot_satisfy_current_project_check(tmp_path):
    store, ps, pid = recorded_project(tmp_path, level="product", cycle=0)
    assert service.verify_checks(store, ps.get(pid))[0]["status"] == "needs you"
    store.close()


def test_explicit_documentation_evidence_policy_is_fixed_spec_protected():
    doc = definition()
    doc["milestones"][0]["evidence_requirements"] = {"M1C1": "unit"}
    spec = render(doc)
    assert "M1C1=unit" in fixed(spec)
    doc["milestones"][0]["evidence_requirements"] = {"M9C9": "unit"}
    with pytest.raises(ProjectError, match="stable check IDs"):
        validate(doc)


def test_project_queue_assignments_cannot_be_removed_by_the_planner(app, tmp_path):
    host = owner(app, tmp_path)
    ps = Projects(host.store)
    project = ps.create()
    ps.draft(project["id"], definition())
    service.approve(host.store, project["id"])
    item = host.store.queue_add("feature", "body", "owner/inventory", project_id=project["id"], milestone=0)
    host.store.queue_update(item, run_id=host.run_id, requirement_ids='["M1C1"]')
    product_gate.configure(host)
    plan = Plan("omit outcomes", [])
    with pytest.raises(ValueError, match="before coding"):
        product_gate.validate(host, plan)
    assert plan.acceptance_criteria[0].requirement_id == "M1C1"
    assert plan.acceptance_criteria[0].evidence_level == "product"
    host.store.close()


def test_standalone_verify_runs_actual_cli_without_agent_calls(app, tmp_path, monkeypatch, capsys):
    from swarmforge.cli import cmd_verify
    from swarmforge.config import SandboxConfig
    from swarmforge.workspace import GitWorkspace
    monkeypatch.setattr(GitWorkspace, "head", lambda repo: "cli-tested-sha")
    contract = app / "swarmforge.verify.json"
    contract.write_text(json.dumps({"checks": [check()]}))
    config = SimpleNamespace(sandbox=SandboxConfig(mode="local", dependency_install=False), workdir=tmp_path)
    args = SimpleNamespace(repo=str(app), contract=str(contract), artifacts=str(tmp_path / "cli-evidence"), timeout=20)
    assert cmd_verify(args, config) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]["status"] == "passed" and result[0]["commit_sha"] == "cli-tested-sha"


def test_invalid_cli_timeout_is_rejected_before_creating_artifacts(tmp_path):
    from swarmforge.cli import cmd_verify
    with pytest.raises(ValueError, match="positive"):
        cmd_verify(SimpleNamespace(repo=str(tmp_path), timeout=0), None)


def test_packaging_failure_feeds_repair_instead_of_becoming_an_environment_excuse(app, tmp_path, monkeypatch):
    host = owner(app, tmp_path)
    product_gate.configure(host)
    plan = Plan("index", [], [Criterion("AC1", "Real output", "product")], [check()])
    product_gate.freeze(host, plan)
    async def broken_install(sandbox, workspace, artifacts, timeout_s):
        (artifacts / "application-install.log").write_text("The generated package has invalid metadata")
        return "Application package could not be installed"
    monkeypatch.setattr(product_gate, "install_application", broken_install)
    rows, _ = finish(product_gate.execute(host, 0))
    assert rows[0].status == "failed"
    product_gate.unavailable(rows)  # a packaging defect must go through repair
    assert "invalid metadata" in product_gate.findings(host)[0]["detail"]
    host.store.close()


def test_orchestrator_records_real_failure_and_sends_it_to_the_existing_gate(app, tmp_path, monkeypatch):
    from swarmforge import orchestrator
    from swarmforge.config import ReviewConfig
    from swarmforge.testing import TestEvidence
    host = owner(app, tmp_path)
    host.cfg.project = SimpleNamespace(test_command="pytest")
    host.cfg.review = ReviewConfig(require_verified_criteria=False)
    host._repair_scope = lambda: set()
    host._record_tests = lambda ev, cycle: host.store.record_test_run(host.run_id, cycle=cycle, label=ev.label,
                                                                   command=ev.command, exit_code=ev.exit_code, passed=1)
    host.plan = Plan("index", [], [Criterion("AC1", "Real output", "product")], [check("empty")])
    product_gate.configure(host)
    product_gate.freeze(host, host.plan)
    async def passing_mock_tests(*args):
        return TestEvidence("cycle0", "pytest", 0, False, 0, passed=1, junit_found=True,
                            cases={"tests.test_fake.test_transcript": "passed"})
    monkeypatch.setattr(orchestrator, "run_tests", passing_mock_tests)
    report = {"criteria_tests": {"AC1": ["tests/test_fake.py::test_transcript"]}, "defects": []}
    ev = finish(orchestrator.Orchestrator._test_run_phase(host, 0, report))
    recorded = host.store.acceptance(host.run_id)[0]
    assert ev.ok and recorded["status"] == "failed"  # green mocked suite is insufficient
    assert recorded["evidence_level"] == "product" and recorded["commit_sha"] == "tested-sha"
    problems = orchestrator.Orchestrator._gate(host, 0, ev, report, None, [])
    assert {p["kind"] for p in problems} == {"verification", "acceptance"}
    host.store.close()


def test_reused_local_ac_id_cannot_verify_a_different_project_requirement(app, tmp_path):
    host = owner(app, tmp_path)
    product_gate.configure(host)
    host._requirement_catalog = [{"id": "M1C1", "text": "Speech", "evidence_level": "product"},
                                 {"id": "M2C1", "text": "Objects", "evidence_level": "product"}]
    first = Plan("speech", [], [Criterion("AC1", "Speech", "product", "M1C1")], [check()])
    product_gate.freeze(host, first)
    assert host._verification_checks[0]["criteria"] == ["M1C1"]
    host._previous_verification = host._verification_checks
    second = Plan("objects", [], [Criterion("AC1", "Objects", "product", "M2C1")])
    with pytest.raises(ValueError, match="M2C1 requires"):
        product_gate.validate(host, second)
    host.store.close()
