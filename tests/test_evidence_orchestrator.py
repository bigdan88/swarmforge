"""Orchestrator visual-evidence collection (AC3/AC4, idempotency, terminal paths).

No browser, no network, no subprocess: evidence.collect is monkeypatched throughout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from swarmforge import evidence
from swarmforge.db import Store
from swarmforge.github import Issue
from swarmforge.orchestrator import Orchestrator
from swarmforge.services.publication import PublicationService
from swarmforge.state import RunState

from .conftest import ISSUE


def _orch(cfg, tmp_path) -> Orchestrator:
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, mode="single", echo=None, llm_transport=None)
    orch.run_id = "test-evidence"
    orch.run_dir = tmp_path / "runs" / "test-evidence"
    orch.run_dir.mkdir(parents=True, exist_ok=True)
    return orch


def _stub_ws(tmp_path, changed=None):
    return SimpleNamespace(
        integration=str(tmp_path / "repo"),
        repo=str(tmp_path / "repo"),
        changed_files=changed or [],
    )


def _stub_plan(files):
    return SimpleNamespace(tasks=[SimpleNamespace(expected_files=files)])


def _events(orch, level=None):
    evs = orch.store.events(orch.run_id)
    return [
        e
        for e in evs
        if e["kind"] == "evidence" and (level is None or e["level"] == level)
    ]


async def test_collect_evidence_ui_plan_captures_tabs_and_manifest(
    cfg, tmp_path, monkeypatch
):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard.py", "static/style.css"])

    calls = []

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        calls.append((repo_dir, Path(out_dir), list(tabs or [])))
        shots = [
            {"tab": t, "theme": "light", "path": str(Path(out_dir) / f"{t}.png")}
            for t in tabs
        ]
        evidence.write_manifest(out_dir, shots)
        return shots

    monkeypatch.setattr(evidence, "collect", fake_collect)

    await orch._collect_evidence()

    assert len(calls) == 1
    # dashboard.py/css map to the affected dashboard tabs (work tab always included)
    expected_tabs = evidence.tabs_for(
        ["src/swarmforge/dashboard.py", "static/style.css"]
    )
    assert calls[0][2] == expected_tabs
    assert "work" in calls[0][2]
    assert orch._evidence_shots and orch._evidence_shots[0]["tab"] in expected_tabs
    manifest = tmp_path / "runs" / "test-evidence" / "evidence" / "manifest.json"
    assert manifest.exists()
    assert evidence.read_manifest(manifest.parent) == orch._evidence_shots
    assert any(
        f"captured {len(orch._evidence_shots)} screenshot" in e["message"]
        for e in _events(orch)
    )


async def test_collect_evidence_non_ui_plan_never_captures(cfg, tmp_path, monkeypatch):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/state.py", "src/swarmforge/llm.py"])

    def boom(*a, **k):  # AC3: capture must not even be attempted
        raise AssertionError("collect must not be called for non-UI plans")

    monkeypatch.setattr(evidence, "collect", boom)

    await orch._collect_evidence()

    assert orch._evidence_shots == []
    assert orch._evidence_done is True
    assert any("skipping" in e["message"] for e in _events(orch))
    assert not (tmp_path / "runs" / "test-evidence" / "evidence").exists()


async def test_collect_evidence_swallows_capture_exception(cfg, tmp_path, monkeypatch):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard.py"])

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(evidence, "collect", boom)

    await orch._collect_evidence()  # must not raise

    warns = _events(orch, level="warn")
    assert warns and "boom" in warns[-1]["message"]
    # The done-flag is set BEFORE capture on purpose (never retry); it must be True.
    assert orch._evidence_done is True


async def test_collect_evidence_is_idempotent(cfg, tmp_path, monkeypatch):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard.py"])

    calls = []

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        calls.append(1)
        return [{"tab": "work", "theme": "light", "path": "x.png"}]

    monkeypatch.setattr(evidence, "collect", fake_collect)

    await orch._collect_evidence()
    await orch._collect_evidence()  # second call is a no-op

    assert len(calls) == 1
    # the done-flag is set before capture: a second call must never re-enter
    assert orch._evidence_done is True
    assert orch._evidence_shots


async def test_collect_evidence_happy_path_sets_done_flag(cfg, tmp_path, monkeypatch):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard.py"])

    calls = []

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        calls.append(1)
        shots = [
            {"tab": t, "theme": "light", "path": str(Path(out_dir) / f"{t}.png")}
            for t in (tabs or [])
        ]
        evidence.write_manifest(out_dir, shots)
        return shots

    monkeypatch.setattr(evidence, "collect", fake_collect)

    await orch._collect_evidence()

    assert len(calls) == 1
    assert orch._evidence_shots  # non-empty
    assert orch._evidence_done is True
    manifest = tmp_path / "runs" / "test-evidence" / "evidence" / "manifest.json"
    assert manifest.exists()


async def test_execute_finally_collects_evidence_on_failure_path(
    cfg, tmp_path, monkeypatch
):
    cfg.self_heal.diagnose = False  # rules-only post-mortem: no model call in this test
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store, mode="single", echo=None, llm_transport=None)

    async def boom_pipeline(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(Orchestrator, "_pipeline", boom_pipeline)

    calls = []

    async def spy_evidence():
        calls.append(1)

    orch._collect_evidence = (
        spy_evidence  # instance attribute: the finally hits the spy
    )

    run_id = await orch.execute(issue_file=str(ISSUE), repo=str(tmp_path / "nope"))

    assert len(calls) == 1  # evidence collection reached on the FAILED path
    assert store.get_run(run_id)["state"] == "FAILED"


async def test_collect_evidence_timeout_is_swallowed(cfg, tmp_path, monkeypatch):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.FAILED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard.py"])

    def slow(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(evidence, "collect", slow)

    await orch._collect_evidence()  # must not raise

    warns = _events(orch, level="warn")
    assert warns and "TimeoutError" in warns[-1]["message"]
    assert orch._evidence_done is True
    assert orch.sm.state is RunState.FAILED  # run state untouched


async def test_collect_evidence_no_workspace_warns(cfg, tmp_path):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = None

    await orch._collect_evidence()

    assert any("no workspace" in e["message"] for e in _events(orch, level="warn"))
    assert orch._evidence_done is True


async def test_collect_evidence_failed_run_includes_failures_tab(
    cfg, tmp_path, monkeypatch
):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.FAILED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard_pages/settings.py"])

    seen = {}

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        seen["tabs"] = list(tabs or [])
        return []

    monkeypatch.setattr(evidence, "collect", fake_collect)

    await orch._collect_evidence()

    assert "failures" in seen["tabs"]  # Review tab for unresolved findings
    assert "settings" in seen["tabs"]


async def test_collect_evidence_uses_changed_files_without_plan(
    cfg, tmp_path, monkeypatch
):
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path, changed=["src/swarmforge/dashboard.py"])
    # no self.plan attribute at all

    seen = {}

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        seen["repo"] = repo_dir
        return []

    monkeypatch.setattr(evidence, "collect", fake_collect)

    await orch._collect_evidence()

    assert seen["repo"] == str(tmp_path / "repo")


def test_deliver_and_finally_call_sites_exist():
    import inspect

    from swarmforge.orchestrator import Orchestrator as O

    src = inspect.getsource(O._deliver)
    assert "await self._collect_evidence()" in src
    full = inspect.getsource(O)
    assert (
        full.count("await self._collect_evidence()") == 2
    )  # _deliver start + execute() finally


# ------------------------------------------------ acceptance-mapped behaviour
# These two node ids are the fixed AC3/AC4 anchors of the acceptance mapping.
# They verify the same behaviour as the focused tests above, through the names
# the criteria reference; renaming or removing them breaks the AC gate.


async def test_no_screenshots_for_non_ui_plan(cfg, tmp_path, monkeypatch):
    """AC3: a plan touching only non-UI modules (state.py, llm.py, ...) must not
    trigger any dashboard screenshot capture and must write no evidence files."""
    files = [
        "src/swarmforge/state.py",
        "src/swarmforge/llm.py",
        "src/swarmforge/report.py",
        "src/swarmforge/github.py",
    ]
    # pure-helper level: the file set is classified as non-UI
    assert evidence.touches_ui(files) is False
    assert evidence.tabs_for(files) == []

    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(files)

    calls = []

    def spy_collect(repo_dir, out_dir, *, tabs=None):
        calls.append((repo_dir, out_dir, list(tabs or [])))
        return []

    monkeypatch.setattr(evidence, "collect", spy_collect)

    await orch._collect_evidence()  # must not raise

    assert calls == [], "capture must never be attempted for a non-UI plan"
    assert orch._evidence_shots == []
    assert not (tmp_path / "runs" / "test-evidence" / "evidence").exists()
    assert any(
        "non-UI" in e["message"] and "skipping" in e["message"] for e in _events(orch)
    ), "the skip must be recorded as an evidence event"


async def test_missing_github_token_skips_upload(cfg, tmp_path, monkeypatch):
    """AC4: with screenshots captured but no GitHub token, the upload stage is
    skipped with a logged warning; nothing raises and the run still completes."""
    orch = _orch(cfg, tmp_path)
    orch.sm = SimpleNamespace(state=RunState.APPROVED)
    orch.ws = _stub_ws(tmp_path)
    orch.plan = _stub_plan(["src/swarmforge/dashboard.py"])

    def fake_collect(repo_dir, out_dir, *, tabs=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        shots = []
        for t in tabs or []:
            p = out / f"{t}-light.png"
            p.write_bytes(b"\x89PNG fake")
            shots.append({"tab": t, "theme": "light", "file": str(p)})
        evidence.write_manifest(out, shots)
        return shots

    monkeypatch.setattr(evidence, "collect", fake_collect)
    await orch._collect_evidence()
    assert orch._evidence_shots, "precondition: UI plan captured screenshots"

    # the run row publication reads (same layout the orchestrator writes)
    orch.store.create_run(
        orch.run_id,
        mode="swarm",
        issue_title="T",
        state="APPROVED",
        run_dir=str(orch.run_dir),
        base_branch="main",
        integration_branch="sf/result",
        config_json="{}",
    )

    class NoTokenGH:
        token = ""  # GitHub token absent, exactly as in a token-less environment

        def __init__(self):
            self.calls = []

        def get_pr_body(self, *a):
            self.calls.append("get_pr_body")

        def upload_pr_asset(self, *a):
            self.calls.append("upload_pr_asset")
            return "https://raw.githubusercontent.com/o/r/sf/result/x.png"

        def update_pr_body(self, *a):
            self.calls.append("update_pr_body")

    gh = NoTokenGH()
    svc = PublicationService(
        cfg=None,
        store=orch.store,
        run_id=orch.run_id,
        issue=Issue("T", "body"),
        ws=None,
        llm=None,
        log=orch.log,  # warnings land in the orchestrator's own event store
        update_base=None,
        push=lambda *a, **k: None,
    )

    await svc._attach_evidence(  # must not raise: the run still completes
        gh, "o", "r", "https://github.com/o/r/pull/7"
    )

    assert gh.calls == [], "no upload or body update may be attempted without a token"
    warns = _events(orch, level="warn")
    assert any("no GitHub token" in e["message"] for e in warns), (
        "the skipped upload must be logged as a warning"
    )
    assert orch.sm.state is RunState.APPROVED  # run outcome untouched
