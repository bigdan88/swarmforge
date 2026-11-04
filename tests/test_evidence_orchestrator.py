"""Orchestrator wiring for visual evidence collection (T2).

Covers the acceptance criteria of the "Auto-attach dashboard screenshots" issue that live in
``Orchestrator._collect_evidence``:

* a UI-touching plan records a kind='evidence' event with the shots before the report is written;
* a non-UI plan never calls ``evidence.screenshot_tabs``;
* capture failures become warn events and never change the run's terminal state;
* collection is idempotent and runs on both the COMPLETED and the FAILED path.
"""

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from swarmforge import evidence, visual
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator


@dataclass
class _Plan:
    summary: str = "touch the dashboard"
    tasks: list = field(default_factory=list)
    acceptance_criteria: list = field(default_factory=list)


class _FakeWorkspace:
    def __init__(self, repo, changed=("src/app/dashboard.py",)):
        self.repo = repo
        self._changed = list(changed)

    def changed_files(self):
        return list(self._changed)


def _orch(tmp_path, cfg, *, changed=("src/app/dashboard.py",), mode="swarm"):
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, mode=mode, echo=None)
    orch.run_id = "r-evidence"
    orch.run_dir = tmp_path / "runs" / "r-evidence"
    orch.run_dir.mkdir(parents=True)
    orch.ws = _FakeWorkspace(str(tmp_path), changed=changed)
    orch.plan = _Plan()
    store.create_run(orch.run_id, mode=mode, run_dir=str(orch.run_dir))
    return orch, store


def _events(store, kind):
    return [e for e in store.events("r-evidence") if e["kind"] == kind]


def test_collect_evidence_records_shots_for_ui_plans(tmp_path, cfg, monkeypatch):
    """A plan touching dashboard.py gets an evidence event with the captured shots."""
    orch, store = _orch(tmp_path, cfg)
    calls = []

    def fake_tabs(repo, out_dir, files, **kw):
        calls.append((repo, list(files), kw.get("failed")))
        return [
            {
                "tab": "chat",
                "theme": "light",
                "file": str(tmp_path / "shots" / "chat-light.png"),
            }
        ]

    monkeypatch.setattr(evidence, "screenshot_tabs", fake_tabs)
    asyncio.run(orch._collect_evidence())

    evs = _events(store, "evidence")
    assert len(evs) == 1 and evs[0]["level"] == "info"
    assert "1 evidence screenshot(s) taken" in evs[0]["message"]
    data = evs[0]["data"]
    if isinstance(data, str):
        data = json.loads(data)
    assert data["shots"] == [
        {
            "tab": "chat",
            "theme": "light",
            "file": str(tmp_path / "shots" / "chat-light.png"),
        }
    ]
    assert calls and calls[0][1] == ["src/app/dashboard.py"]


def test_collect_evidence_skips_non_ui_plans(tmp_path, cfg, monkeypatch):
    """AC3: state.py/llm.py-only plans never trigger a screenshot."""
    orch, store = _orch(
        tmp_path, cfg, changed=("src/swarmforge/state.py", "src/swarmforge/llm.py")
    )

    def never(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("screenshot_tabs must not be called for non-UI plans")

    monkeypatch.setattr(evidence, "screenshot_tabs", never)
    asyncio.run(orch._collect_evidence())

    evs = _events(store, "evidence")
    assert len(evs) == 1 and "does not touch dashboard files" in evs[0]["message"]
    assert evs[0]["level"] == "info"


def test_collect_evidence_skips_single_mode(tmp_path, cfg, monkeypatch):
    orch, store = _orch(tmp_path, cfg, mode="single")
    monkeypatch.setattr(
        evidence,
        "screenshot_tabs",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    asyncio.run(orch._collect_evidence())
    assert any("single-agent mode" in e["message"] for e in _events(store, "evidence"))


@pytest.mark.parametrize(
    "exc",
    [
        OSError("playwright not installed"),
        RuntimeError("chromium crashed"),
        visual.RegressError("command failed"),
        visual.PreviewError("dashboard did not answer HTTP"),
    ],
)
def test_collect_evidence_warns_and_survives_capture_failures(
    tmp_path, cfg, monkeypatch, exc
):
    """Capture errors become a warn event; the run state is untouched and no shots are recorded."""
    orch, store = _orch(tmp_path, cfg)

    def broken(repo, out_dir, files, **kw):
        raise exc

    monkeypatch.setattr(evidence, "screenshot_tabs", broken)
    asyncio.run(orch._collect_evidence())

    evs = _events(store, "evidence")
    assert len(evs) == 1 and evs[0]["level"] == "warn"
    assert "screenshots skipped" in evs[0]["message"]
    assert str(exc) in evs[0]["message"]
    assert store.get_run("r-evidence")["state"] not in ("COMPLETED",)
    # no retry is attempted after a capture failure (the flag is set before capture)
    assert orch._evidence_done is True


def test_collect_evidence_swallows_unexpected_exception_captures(
    tmp_path, cfg, monkeypatch
):
    """F1 regression: even exception types we did not anticipate must not escape."""

    class PlaywrightLikeError(Exception):
        pass

    orch, store = _orch(tmp_path, cfg)

    def broken(repo, out_dir, files, **kw):
        raise PlaywrightLikeError("target closed unexpectedly")

    monkeypatch.setattr(evidence, "screenshot_tabs", broken)
    asyncio.run(orch._collect_evidence())  # must not raise

    evs = _events(store, "evidence")
    assert len(evs) == 1 and evs[0]["level"] == "warn"
    assert "screenshots skipped" in evs[0]["message"]
    assert "PlaywrightLikeError" in evs[0]["message"]
    assert orch._evidence_done is False  # a retry is still possible


def test_collect_evidence_captures_time_out(tmp_path, cfg, monkeypatch):
    """F1 regression: a capture that blocks longer than the timeout is cut short."""
    import time

    import swarmforge.orchestrator as orch_mod

    orch, store = _orch(tmp_path, cfg)
    monkeypatch.setattr(orch_mod, "_EVIDENCE_CAPTURE_TIMEOUT_S", 0.2)

    def blocking(repo, out_dir, files, **kw):
        time.sleep(5)
        return [{"tab": "chat", "theme": "light", "file": "x.png"}]  # pragma: no cover

    monkeypatch.setattr(evidence, "screenshot_tabs", blocking)
    asyncio.run(orch._collect_evidence())  # must not raise

    evs = _events(store, "evidence")
    assert len(evs) == 1 and evs[0]["level"] == "warn"
    assert "screenshots skipped" in evs[0]["message"]
    assert "timed out" in evs[0]["message"]
    data = evs[0].get("data")
    if data:
        if isinstance(data, str):
            data = json.loads(data)
        assert not data.get("shots")
    assert orch._evidence_done is False


def test_collect_evidence_is_idempotent(tmp_path, cfg, monkeypatch):
    """Collected once: a second call (e.g. the finally path after the COMPLETED path) is a no-op."""
    orch, store = _orch(tmp_path, cfg)
    calls = []

    def fake(repo, out_dir, files, **kw):
        calls.append(files)
        return [
            {
                "tab": "chat",
                "theme": "light",
                "file": str(tmp_path / "shots" / "chat-light.png"),
            }
        ]

    monkeypatch.setattr(evidence, "screenshot_tabs", fake)
    asyncio.run(orch._collect_evidence())
    asyncio.run(orch._collect_evidence())

    assert len(calls) == 1
    assert len(_events(store, "evidence")) == 1


def test_plan_file_set_unions_plan_and_workspace(tmp_path, cfg):
    orch, _ = _orch(tmp_path, cfg, changed=("src/app/dashboard_pages/chat.py",))
    orch.plan = _Plan(tasks=[{"expected_files": ["src/app/dashboard.py"]}])
    assert orch._plan_file_set() == [
        "src/app/dashboard.py",
        "src/app/dashboard_pages/chat.py",
    ]


def test_failed_run_includes_failures_tab(tmp_path, cfg, monkeypatch):
    """A FAILED run asks for the failures/review tab so unresolved findings are visible."""
    orch, store = _orch(tmp_path, cfg)
    store.update_run("r-evidence", state="FAILED")
    seen = {}

    def fake(repo, out_dir, files, **kw):
        seen["failed"] = kw.get("failed")
        return [
            {
                "tab": "failures",
                "theme": "light",
                "file": str(tmp_path / "shots" / "failures-light.png"),
            }
        ]

    monkeypatch.setattr(evidence, "screenshot_tabs", fake)
    asyncio.run(orch._collect_evidence())
    assert seen["failed"] is True
