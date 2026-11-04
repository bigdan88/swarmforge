"""Acceptance tests for the optional review.a11y_gate in the orchestrator review cycle.

Fast and offline: `visual.capture` is monkeypatched (no browser, no server), and
`visual.audit_pages` is monkeypatched to return canned a11y findings, proving the
gate wiring only: findings flow into _gate / repair tasks, the flag is optional,
and audit failures never crash the run.
"""

import asyncio

from swarmforge import state, visual
from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator
from swarmforge.testing import TestEvidence
from tests.conftest import base_raw


def _ev() -> TestEvidence:
    return TestEvidence(
        label="tests",
        command="pytest",
        exit_code=0,
        timed_out=False,
        duration_s=1.0,
        passed=3,
        junit_found=True,
    )


def _pv():
    return visual.parse_preview(
        {"command": "x", "port": 8501, "pages": [{"path": "/", "name": "chat"}]}
    )


class _FakeWorkspace:
    base_sha = "0" * 40

    def __init__(self, repo):
        self.repo = repo

    def changed_files(self):
        return ["src/app.py"]


def _cfg(tmp_path, a11y_gate=True):
    raw = base_raw(tmp_path / "work")
    raw["review"] = {"a11y_gate": a11y_gate}
    return parse_config(raw)


def _orch(tmp_path, cfg, repo):
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, echo=None)
    orch.run_id = "r-a11y-gate"
    orch.ws = _FakeWorkspace(repo)
    orch.artifacts = tmp_path / "artifacts"
    orch.sandbox = None
    orch.preview = _pv()
    return orch, store


def _shots():
    return [
        {
            "page": "chat",
            "path": "/",
            "theme": theme,
            "url": "http://localhost:8501/",
            "file": None,
            "problems": [],
            "text": "ok",
        }
        for theme in ("light", "dark")
    ]


def _patch_visual(monkeypatch):
    async def fake_capture(ctx, pv, cycle, log=None):
        return _shots()

    monkeypatch.setattr(visual, "capture", fake_capture)
    monkeypatch.setattr(visual, "golden_gate", lambda *a, **k: [])
    monkeypatch.setattr(visual, "page_error_findings", lambda shots: [])


MAJOR = {
    "severity": "major",
    "page": "chat",
    "theme": "light",
    "blocking": True,
    "a11y": True,
    "file": "src/swarmforge/dashboard_pages/chat.py",
    "line": None,
    "title": "button without an accessible name",
    "description": "button on the chat page has no accessible name",
    "recommendation": "give the chat page's unnamed buttons a label",
}

MINOR = {
    "severity": "minor",
    "page": "chat",
    "theme": "light",
    "blocking": False,
    "a11y": True,
    "file": "src/swarmforge/dashboard_pages/chat.py",
    "line": None,
    "title": "text contrast 2.1:1 below 4.5:1",
    "description": "light-theme text contrast is too low",
    "recommendation": "darken the text color",
}


def test_a11y_gate_major_finding_blocks_and_creates_repair_task(tmp_path, monkeypatch, sample_repo):
    """AC: with review.a11y_gate=true an unnamed-button [major] on the chat page becomes a
    kind='visual' gate problem and a repair task."""
    cfg = _cfg(tmp_path, a11y_gate=True)
    orch, store = _orch(tmp_path, cfg, sample_repo)
    _patch_visual(monkeypatch)
    calls = []

    async def fake_audit_pages(ctx, pv, *, log=None, min_ratio=4.5):
        calls.append((ctx, pv))
        return [dict(MAJOR), dict(MINOR)]

    monkeypatch.setattr(visual, "audit_pages", fake_audit_pages)

    findings = asyncio.run(orch._visual_review(1))
    assert calls, "audit_pages must run when a11y_gate is enabled"
    assert any(f["a11y"] and f["severity"] == "major" for f in findings)
    assert any(f["a11y"] and f["severity"] == "minor" for f in findings)

    rows = store.findings("r-a11y-gate")
    assert any(r["source"] == "visual" and "accessible name" in r["title"] for r in rows)
    events = store.events("r-a11y-gate")
    assert any("a11y audit: 2 finding(s)" in e["message"] for e in events if e["kind"] == "visual")

    orch._visual_findings = findings
    problems = orch._gate(1, _ev(), None, None, [])
    assert any(p["kind"] == "visual" and "[visual/major]" in p["summary"] for p in problems)

    tasks = state.repair_tasks_from_findings(findings, ["developer_frontend"], 1)
    assert any("accessible name" in t.title for t in tasks)


def test_a11y_gate_off_by_default_never_runs_the_audit(tmp_path, monkeypatch, sample_repo):
    """AC: with the flag off (the default) audit_pages is never called and behavior is unchanged."""
    cfg = _cfg(tmp_path, a11y_gate=False)
    assert cfg.review.a11y_gate is False
    orch, _ = _orch(tmp_path, cfg, sample_repo)
    _patch_visual(monkeypatch)

    async def boom(*a, **k):
        raise AssertionError("audit_pages must not run when a11y_gate is off")

    monkeypatch.setattr(visual, "audit_pages", boom)
    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []


def test_audit_pages_failure_is_skipped_not_fatal(tmp_path, monkeypatch, sample_repo):
    """AC: any exception from the audit logs 'a11y audit skipped' and the cycle continues."""
    cfg = _cfg(tmp_path, a11y_gate=True)
    orch, store = _orch(tmp_path, cfg, sample_repo)
    _patch_visual(monkeypatch)

    async def broken(ctx, pv, *, log=None, min_ratio=4.5):
        raise RuntimeError("preview did not start")

    monkeypatch.setattr(visual, "audit_pages", broken)
    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    events = store.events("r-a11y-gate")
    skips = [e for e in events if "a11y audit skipped" in e["message"]]
    assert len(skips) == 1 and skips[0]["level"] == "warn"
    assert "preview did not start" in skips[0]["message"]
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []


def test_minor_contrast_findings_do_not_block(tmp_path, monkeypatch, sample_repo):
    """AC: minor contrast findings are recorded for the judge/PR but do not block the gate."""
    cfg = _cfg(tmp_path, a11y_gate=True)
    orch, store = _orch(tmp_path, cfg, sample_repo)
    _patch_visual(monkeypatch)

    async def minor_only(ctx, pv, *, log=None, min_ratio=4.5):
        return [dict(MINOR)]

    monkeypatch.setattr(visual, "audit_pages", minor_only)
    findings = asyncio.run(orch._visual_review(1))
    assert [f["severity"] for f in findings] == ["minor"]
    rows = store.findings("r-a11y-gate")
    assert any(r["source"] == "visual" and "contrast" in r["title"] for r in rows)
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []


def test_config_parses_a11y_gate_default_and_true(tmp_path, monkeypatch):
    """AC: review.a11y_gate is accepted by the parser and defaults to False."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    assert parse_config(raw).review.a11y_gate is False
    raw["review"] = {"a11y_gate": True}
    assert parse_config(raw).review.a11y_gate is True
