"""Known broken apps must not get product acceptance from passing unit tests."""
import copy
import importlib.util
import json
import shutil
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmforge.config import ReviewConfig
from swarmforge.plan import Criterion, Plan, Task
from swarmforge.sandbox import CommandResult
from swarmforge.services.assessment import assess_gate
from swarmforge.testing import TestEvidence, evaluate_criteria
from swarmforge.verification import (VerificationError, VerificationEvidence, bind_requirements,
                                    contract_hash, enforce_evidence, freeze_checks, parse_checks,
                                    prepare_workspace, run_checks)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sf_product_probe", ROOT / "src/swarmforge/resources/product_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def check(mode="healthy", **changes):
    case = {"id": "index-smoke", "criteria": ["AC1"], "level": "product", "fixtures": ["sample.txt"],
            "steps": [{"kind": "command", "argv": [sys.executable, "indexer.py", "--mode", mode]},
                      {"kind": "json", "path": "result.json", "pointer": "/entries", "min_count": 1},
                      {"kind": "file", "path": "result.json", "contains": "red bicycle"}]}
    return parse_checks([{**case, **changes}])[0]


def run_probe(case, workspace, artifacts):
    artifacts.mkdir(parents=True, exist_ok=True)
    return probe.execute({"check": case, "commit_sha": "tested-sha", "contract_hash": contract_hash([case])}, workspace, artifacts)


@pytest.fixture
def app(tmp_path):
    workspace = tmp_path / "app"
    shutil.copytree(ROOT / "bench/product-verification", workspace)
    return workspace


@pytest.mark.parametrize("case", json.loads((ROOT / "bench/product-verification/suite.json").read_text()))
def test_known_broken_success_messages_are_rejected(app, tmp_path, case):
    result = run_probe(check(case["mode"]), app, tmp_path / "evidence")
    assert result["status"] == case["expected"]
    assert result["environment"]["fixtures"]["sample.txt"]
    assert result["commit_sha"] == "tested-sha"


def unit_result():
    ev = TestEvidence("cycle0", "pytest", 0, False, 0, passed=1, junit_found=True,
                      cases={"tests.test_fake.test_transcript": "passed"})
    return evaluate_criteria({"AC1": ["tests/test_fake.py::test_transcript"]}, [("AC1", "Transcribes real speech")], ev)


def evidence(status="passed", level="product", sha="tested-sha"):
    return VerificationEvidence("speech", ["M1C1"], level, status, "observed outputs", sha, "frozen", "result.json")


def test_green_mock_tests_cannot_satisfy_product_requirement():
    result = enforce_evidence(unit_result(), [Criterion("AC1", "Speech", "product", "M1C1")], [], commit_sha="tested-sha")
    assert result[0]["status"] == "unverified"
    assert result[0]["evidence_level"] == "unit"
    assert "unit tests are insufficient" in result[0]["evidence"]
    ev = TestEvidence("c0", "pytest", 0, False, 0, passed=1, junit_found=True)
    problems = assess_gate(ev, criteria=result, tester_report=None, review=None, failed_tasks=[], task_rows=[],
                           scope=set(), visual_findings=[], policy=ReviewConfig(require_verified_criteria=False))
    assert problems[0]["kind"] == "acceptance"  # required product evidence cannot be opted out this way


@pytest.mark.parametrize("status,level,sha,expected", [
    ("passed", "product", "tested-sha", "satisfied"),
    ("passed", "integration", "tested-sha", "unverified"),
    ("passed", "product", "old-sha", "unverified"),
    ("empty", "product", "tested-sha", "failed"),
    ("blocked", "product", "tested-sha", "unverified"),
    ("failed", "product", "tested-sha", "failed"),
])
def test_evidence_fidelity_commit_and_outcome(status, level, sha, expected):
    result = enforce_evidence(unit_result(), [Criterion("AC1", "Speech", "product", "M1C1")],
                              [evidence(status, level, sha)], commit_sha="tested-sha")
    assert result[0]["status"] == expected


def test_product_pass_never_hides_failed_unit_regression():
    rows = unit_result()
    rows[0]["status"] = "failed"
    result = enforce_evidence(rows, [Criterion("AC1", "Speech", "product", "M1C1")], [evidence()], commit_sha="tested-sha")
    assert result[0]["status"] == "failed"


def test_contract_freezes_expectations_across_repairs():
    original = check()
    changed = copy.deepcopy(original)
    changed["steps"][1]["min_count"] = 0
    with pytest.raises(VerificationError, match="cannot be changed"):
        freeze_checks([original], [changed], [])
    assert freeze_checks([original], [], []) == [original]
    with pytest.raises(VerificationError, match="before coding"):
        freeze_checks([], [], [Criterion("AC1", "Real speech", "product")])


def test_assigned_requirements_cannot_be_omitted_or_downgraded():
    catalog = [{"id": "M1C1", "text": "Speech is searchable", "evidence_level": "product"}]
    plan = Plan("s", [Task("T1", "t", "d", "developer_backend")], [Criterion("AC1", "paraphrased", "unit", "M1C1")])
    bind_requirements(plan, catalog, ["M1C1"])
    assert plan.acceptance_criteria[0].evidence_level == "product"
    empty = Plan("s", [])
    bind_requirements(empty, catalog, ["M1C1"])
    assert empty.acceptance_criteria[0].requirement_id == "M1C1"


@pytest.mark.parametrize("change", [
    {"steps": [{"kind": "command", "argv": ["pytest"]}, {"kind": "file", "path": "result.json"}]},
    {"steps": [{"kind": "command", "argv": ["python", "-m", "unittest"]}, {"kind": "file", "path": "result.json"}]},
    {"preflight": {"network": "bridge"}},
    {"preflight": {"cuda": "yes"}},
    {"fixtures": ["../private.mp4"]},
    {"steps": [{"kind": "command", "argv": ["app"]}, {"kind": "http", "url": "https://example.com"}]},
    {"steps": [{"kind": "command", "argv": ["app"]}, {"kind": "file", "path": "../secret"}]},
    {"timeout_s": float("inf")},
    {"level": []},
    {"steps": [{"kind": [], "argv": ["app"]}]},
    {"steps": [{"kind": "command", "argv": ["app"]}, {"kind": "browser", "url": "http://127.0.0.1:8000", "contains": "ready", "actions": [{"kind": [], "selector": "button"}]}]},
])
def test_bad_contracts_do_not_expand_access_or_promote_unit_tests(change):
    with pytest.raises(VerificationError):
        check(**change)


def test_missing_capabilities_are_blocked_with_actionable_reason(app, tmp_path):
    case = check(preflight={"executables": ["definitely-not-an-installed-runtime-sf"]})
    result = run_probe(case, app, tmp_path / "evidence")
    assert result["status"] == "blocked"
    assert "Required executable unavailable" in result["reason"]
    assert not (app / "result.json").exists()


def test_missing_cuda_never_becomes_fake_success(app, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    result = run_probe(check(preflight={"cuda": True}), app, tmp_path / "evidence")
    assert result["status"] == "blocked" and "CUDA" in result["reason"]


def test_workspace_copy_excludes_user_recordings_and_credentials(app, tmp_path):
    (app / "data").mkdir()
    (app / "data/user-recording.mp4").write_bytes(b"private")
    (app / ".env").write_text("credential")
    (app / ".env.local").write_text("credential")
    (app / "user.db").write_bytes(b"private database")
    (app / "user.db-wal").write_bytes(b"private journal")
    target = prepare_workspace(app, tmp_path / "fresh")
    assert (target / "indexer.py").exists()
    assert not (target / "data").exists() and not (target / ".env").exists()
    assert not (target / "user.db").exists()
    assert not (target / ".env.local").exists() and not (target / "user.db-wal").exists()


def finish(coroutine):
    """These fake-sandbox calls do not suspend; no network/event-loop fixture needed."""
    with pytest.raises(StopIteration) as result:
        coroutine.send(None)
    return result.value.value


class ProbeSandbox:
    def __init__(self, produce=True):
        self.produce = produce
        self.workspaces = []
    async def run(self, workspace, command, *, artifacts_dir, **kwargs):
        self.workspaces.append(workspace)
        request = json.loads((artifacts_dir / "request.json").read_text())
        result = probe.execute(request, workspace, artifacts_dir)
        if self.produce:
            (artifacts_dir / "result.json").write_text(json.dumps(result))
        return CommandResult(command, 0, "", 0)


def test_real_cli_is_rerun_in_fresh_case_workspaces(app, tmp_path):
    sandbox = ProbeSandbox()
    cases = [check(), check(id="second-case")]
    results = finish(run_checks(sandbox, app, cases, tmp_path / "evidence", commit_sha="sha", timeout_s=30))
    assert all(r.status == "passed" for r in results)
    assert sandbox.workspaces[0] != sandbox.workspaces[1]
    assert not (app / "result.json").exists()


def test_stale_result_cannot_be_replayed(app, tmp_path):
    artifacts = tmp_path / "evidence"
    first = finish(run_checks(ProbeSandbox(), app, [check()], artifacts, commit_sha="sha", timeout_s=30))
    assert first[0].status == "passed"
    second = finish(run_checks(ProbeSandbox(produce=False), app, [check()], artifacts, commit_sha="sha", timeout_s=30))
    assert second[0].status == "failed"


def test_source_checkout_outputs_cannot_stand_in_for_real_processing(app, tmp_path):
    old = {"entries": [{"text": "the red bicycle"}]}
    (app / "result.json").write_text(json.dumps(old))
    rows = finish(run_checks(ProbeSandbox(), app, [check("missing")], tmp_path / "evidence", commit_sha="sha", timeout_s=30))
    assert rows[0].status == "failed"
    assert json.loads((app / "result.json").read_text()) == old  # source files stay untouched


def test_input_fixture_cannot_be_its_own_output_evidence():
    with pytest.raises(VerificationError, match="cannot also be a fixture"):
        check(steps=[{"kind": "command", "argv": ["python", "indexer.py", "--mode", "missing"]},
                     {"kind": "file", "path": "sample.txt", "contains": "bicycle"}])


def test_readonly_sqlite_checks_reject_empty_tables(app, tmp_path):
    case = check(steps=[{"kind": "command", "argv": [sys.executable, "-c", "import sqlite3; c=sqlite3.connect('index.db'); c.execute('CREATE TABLE transcripts(text)'); c.commit()"]},
                       {"kind": "sqlite", "path": "index.db", "query": "SELECT text FROM transcripts", "min_rows": 1}])
    result = run_probe(case, app, tmp_path / "evidence")
    assert result["status"] == "empty"


def test_attachment_header_is_a_failed_playback_check(app, tmp_path, monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Disposition": 'attachment; filename="recording.mp4"'}
        def read(self, *_): return b"video"
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=lambda *a, **kw: Response()))
    with pytest.raises(AssertionError, match="Content-Disposition"):
        probe.observe({"kind": "http", "url": "http://127.0.0.1:8000/media/1", "headers": {"Content-Disposition": "inline"}}, app, tmp_path, lambda: 5)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="real ffmpeg unavailable")
def test_real_ffmpeg_format_failure_is_detected(app, tmp_path):
    with wave.open(str(app / "speech.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)
    case = check(steps=[{"kind": "command", "argv": [shutil.which("ffmpeg"), "-y", "-i", "speech.wav", "-c:a", "aac", "audio.m4a.part"]},
                       {"kind": "file", "path": "audio.m4a"}])
    result = run_probe(case, app, tmp_path / "evidence")
    assert result["status"] == "failed"
    assert "command exited" in result["reason"]
