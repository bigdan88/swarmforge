"""Acceptance tests for the optional a11y audit wired through the visual review cycle.

The gate is opt-in via the preview config (`a11y: true`): `visual.capture` runs the a11y
probe per page/theme, collects Finding dicts into each shot's 'a11y_findings', and the
existing `visual.page_error_findings` line in `Orchestrator._visual_review` turns them
into run findings (majors block via assess_gate; all of them feed
`state.repair_tasks_from_findings`).

Fast and offline: the browser/process layer (`agent.devtools`) is faked and
`visual._browse_probe` is monkeypatched, so no Chromium, no server, no network.
"""

import asyncio

from swarmforge import state, visual
from swarmforge.agent.tools import ToolError
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


def _pv(a11y):
    raw = {"command": "x", "port": 8501, "pages": [{"path": "/", "name": "chat"}]}
    if a11y:
        raw["a11y"] = True
    return visual.parse_preview(raw)


class _FakeWorkspace:
    base_sha = "0" * 40

    def __init__(self, repo):
        self.repo = repo

    def changed_files(self):
        return ["src/app.py"]


def _orch(tmp_path, cfg, repo, a11y=True):
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, echo=None)
    orch.run_id = "r-a11y-gate"
    orch.ws = _FakeWorkspace(repo)
    orch.artifacts = tmp_path / "artifacts"
    orch.sandbox = None
    orch.preview = _pv(a11y)
    return orch, store


def _fake_devtools(monkeypatch, browse_calls):
    """Replace the browser/process layer so capture() never touches Chromium or a real app."""
    from swarmforge.agent import devtools

    async def fake_start(ctx, name, command, **kw):
        return f"process '{name}' RUNNING and ready on port {kw.get('ready_port')}"

    async def fake_browse(ctx, url, **kw):
        browse_calls.append(kw)
        return {
            "screenshot_file": f"/artifacts/{kw['screenshot_name']}",
            "exceptions": [],
            "app_errors": [],
            "page_errors": [],
            "action_errors": [],
            "console": [],
            "text": "Chat",
        }

    async def fake_stop(ctx, name):
        return f"stopped '{name}'"

    class _FakeSession:
        python = "python"

        async def exec(self, command, **kw):
            from swarmforge.sandbox import CommandResult

            return CommandResult(command, 0, "", 0.0)

    def fake_open_session(ctx):
        return _FakeSession()

    monkeypatch.setattr(devtools, "start_process", fake_start)
    monkeypatch.setattr(devtools, "browse_raw", fake_browse)
    monkeypatch.setattr(devtools, "stop_process", fake_stop)
    monkeypatch.setattr(devtools, "open_session", fake_open_session)


# One probe result: an unnamed button (label [major]) + a low-contrast pair ([minor]).
_PROBE_DATA = {
    "a11y": {
        "role": "WebArea",
        "name": "Chat",
        "children": [{"role": "button", "name": ""}],
    },
    "contrast": [
        {
            "selector": "p.dim",
            "text": "dim text",
            "color": "#999999",
            "background": "#ffffff",
        }
    ],
}


def test_a11y_preview_feeds_findings_into_the_gate_and_repair_tasks(
    tmp_path, monkeypatch, sample_repo
):
    """AC3/AC4: with preview a11y=true an unnamed-button [major] and a low-contrast [minor]
    flow through capture -> page_error_findings -> run findings, gate problems and repair tasks."""
    cfg = parse_config(base_raw(tmp_path / "work"))
    orch, store = _orch(tmp_path, cfg, sample_repo, a11y=True)
    _fake_devtools(monkeypatch, [])
    probe_calls = []

    async def fake_probe(ctx, url, *, color_scheme=None, a11y=False, **kw):
        assert a11y is True
        probe_calls.append((url, color_scheme))
        return dict(_PROBE_DATA)

    monkeypatch.setattr(visual, "_browse_probe", fake_probe)

    findings = asyncio.run(orch._visual_review(1))

    assert len(probe_calls) == 2, "the probe must run once per theme"
    assert {c[1] for c in probe_calls} == {"light", "dark"}
    assert any(
        f["a11y"] and f["severity"] == "major" and "accessible name" in f["title"]
        for f in findings
    )
    assert any(
        f["a11y"] and f["severity"] == "minor" and "contrast" in f["title"]
        for f in findings
    )

    rows = store.findings("r-a11y-gate")
    assert any(
        r["source"] == "visual" and "accessible name" in r["title"] for r in rows
    )
    assert any(r["source"] == "visual" and "contrast" in r["title"] for r in rows)
    events = store.events("r-a11y-gate")
    assert any(
        "a11y audit: 4 finding(s)" in e["message"]
        for e in events
        if e["kind"] == "visual"
    )

    orch._visual_findings = findings
    problems = orch._gate(1, _ev(), None, None, [])
    assert any(
        p["kind"] == "visual" and "[visual/major]" in p["summary"] for p in problems
    )

    tasks = state.repair_tasks_from_findings(findings, ["developer_frontend"], 1)
    assert tasks and any(
        "accessible name" in t.title or "contrast" in t.title for t in tasks
    )


def test_a11y_off_by_default_and_probe_never_runs(tmp_path, monkeypatch, sample_repo):
    """AC: with a11y absent (the default) the probe is never called and capture behaves as before."""
    cfg = parse_config(base_raw(tmp_path / "work"))
    orch, _store = _orch(tmp_path, cfg, sample_repo, a11y=False)
    assert orch.preview.a11y is False
    browse_calls = []
    _fake_devtools(monkeypatch, browse_calls)

    async def boom(*a, **k):
        raise AssertionError("the a11y probe must not run when preview.a11y is off")

    monkeypatch.setattr(visual, "_browse_probe", boom)

    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    # the screenshot flow itself is unchanged: both themes were still captured
    assert len(browse_calls) == 2
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []


def test_probe_failure_is_skipped_per_page_and_theme(
    tmp_path, monkeypatch, sample_repo
):
    """AC: a probe exception inside capture is swallowed per page/theme ('a11y audit skipped')
    and the remaining screenshot flow still returns its shots."""
    cfg = parse_config(base_raw(tmp_path / "work"))
    orch, store = _orch(tmp_path, cfg, sample_repo, a11y=True)
    _fake_devtools(monkeypatch, [])

    async def broken(ctx, url, **kw):
        raise ToolError("the browser failed: no Chromium")

    monkeypatch.setattr(visual, "_browse_probe", broken)

    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    events = store.events("r-a11y-gate")
    skips = [e for e in events if "a11y audit skipped" in e["message"]]
    assert len(skips) == 2 and all(e["level"] == "info" for e in skips)
    assert all("no Chromium" in e["message"] for e in skips)
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []


def test_review_config_has_no_a11y_gate_field(tmp_path, monkeypatch):
    """AC: the old review.a11y_gate flag is gone; parse_config tolerates a review section
    without it and the parsed ReviewConfig has no a11y_gate attribute."""
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path / "work")
    raw["review"] = {"blocking_severities": ["critical"]}
    cfg = parse_config(raw)
    assert cfg.review.blocking_severities == ["critical"]
    assert getattr(cfg.review, "a11y_gate", None) is None
    assert not hasattr(cfg.review, "a11y_gate")
