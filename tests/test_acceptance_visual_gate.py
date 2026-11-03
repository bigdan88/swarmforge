"""Acceptance tests for the golden-diff gate in the orchestrator review step (AC5).

Fast and offline: `visual.capture` is monkeypatched to return shots whose 'file' points at
real PNGs written into tmp artifacts, and the golden PNGs live in a tmp repo's
tests/golden. No browser, no server, no network.
"""

import asyncio
import json

from PIL import Image

from swarmforge import visual
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator
from swarmforge.testing import TestEvidence


def _pv():
    return visual.parse_preview(
        {"command": "x", "port": 8501, "pages": [{"path": "/", "name": "home"}]}
    )


def _png(path, color, size=(64, 64)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


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


class _FakeWorkspace:
    base_sha = "0" * 40

    def __init__(self, repo):
        self.repo = repo

    def changed_files(self):
        return ["app.py"]


def _orch(tmp_path, cfg):
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, echo=None)
    orch.run_id = "r-visual-gate"
    orch.ws = _FakeWorkspace(tmp_path)
    orch.artifacts = tmp_path / "artifacts"
    orch.sandbox = None
    orch.preview = _pv()
    return orch, store


def _shots(artifacts, cycle=1, colors=("red", "red")):
    shots = []
    for theme, color in zip(("light", "dark"), colors):
        f = _png(artifacts / "visual" / f"c{cycle}-home-{theme}.png", color)
        shots.append(
            {
                "page": "home",
                "path": "/",
                "theme": theme,
                "url": "http://localhost:8501/",
                "file": str(f),
                "problems": [],
                "text": "ok",
            }
        )
    return shots


def _capture(shots):
    async def fake_capture(ctx, pv, cycle, log=None):
        return shots

    return fake_capture


def test_golden_regression_produces_blocking_major_finding_and_gate_problem(
    tmp_path, cfg, monkeypatch
):
    """A >2% pixel diff vs tests/golden -> blocking [major] finding, recorded, and a gate problem."""
    orch, store = _orch(tmp_path, cfg)
    shots = _shots(orch.artifacts)
    monkeypatch.setattr(visual, "capture", _capture(shots))
    # golden: identical dark tab, very different light tab
    gdir = tmp_path / "tests" / "golden"
    _png(gdir / "home-light.png", "blue")
    _png(gdir / "home-dark.png", "red")

    findings = asyncio.run(orch._visual_review(1))

    assert len(findings) == 1
    f = findings[0]
    assert (
        f["severity"] == "major"
        and f["blocking"] is True
        and f["golden_regression"] is True
    )
    assert f["page"] == "home" and f["theme"] == "light"
    assert "visual regression" in f["title"]

    rows = store.findings("r-visual-gate")
    assert any(
        r["source"] == "visual"
        and r["severity"] == "major"
        and "visual regression" in r["title"]
        for r in rows
    )
    events = store.events("r-visual-gate")
    assert any(
        "golden diff: 2 tab(s) checked, 1 regression(s)" in e["message"]
        for e in events
        if e["kind"] == "visual"
    )

    orch._visual_findings = findings
    problems = orch._gate(1, _ev(), None, None, [])
    assert any(
        p["kind"] == "visual" and "[visual/major]" in p["summary"] for p in problems
    )


def test_identical_goldens_pass_the_gate(tmp_path, cfg, monkeypatch):
    """Identical golden PNGs -> no findings, no gate problem."""
    orch, store = _orch(tmp_path, cfg)
    shots = _shots(orch.artifacts)
    monkeypatch.setattr(visual, "capture", _capture(shots))
    gdir = tmp_path / "tests" / "golden"
    _png(gdir / "home-light.png", "red")
    _png(gdir / "home-dark.png", "red")

    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []
    events = store.events("r-visual-gate")
    assert any(
        "golden diff: 2 tab(s) checked, 0 regression(s)" in e["message"]
        for e in events
        if e["kind"] == "visual"
    )


def test_no_golden_dir_is_a_noop_with_a_log_line(tmp_path, cfg, monkeypatch):
    """No tests/golden -> no findings, but the golden diff log line still appears."""
    orch, store = _orch(tmp_path, cfg)
    shots = _shots(orch.artifacts)
    monkeypatch.setattr(visual, "capture", _capture(shots))

    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    orch._visual_findings = findings
    assert orch._gate(1, _ev(), None, None, []) == []
    events = store.events("r-visual-gate")
    assert any(
        "golden diff: 2 tab(s) checked, 0 regression(s)" in e["message"]
        for e in events
        if e["kind"] == "visual"
    )
    assert not any("golden comparison skipped" in e["message"] for e in events)


def test_golden_gate_failure_is_skipped_not_fatal(tmp_path, cfg, monkeypatch):
    """A broken golden comparison logs a warning and leaves the run unaffected."""
    orch, store = _orch(tmp_path, cfg)
    shots = _shots(orch.artifacts)
    monkeypatch.setattr(visual, "capture", _capture(shots))

    def broken_gate(shots, repo, *, threshold=visual.DIFF_THRESHOLD):
        raise OSError("golden PNG is corrupt")

    monkeypatch.setattr(visual, "golden_gate", broken_gate)
    findings = asyncio.run(orch._visual_review(1))
    assert findings == []
    events = store.events("r-visual-gate")
    skips = [e for e in events if "golden comparison skipped" in e["message"]]
    assert len(skips) == 1 and skips[0]["level"] == "warn"
    assert "golden PNG is corrupt" in skips[0]["message"]


def test_vision_model_major_without_blocking_flag_stays_non_blocking(tmp_path, cfg):
    """A vision-model [major] finding without the blocking flag must not block the PR."""
    orch, _ = _orch(tmp_path, cfg)
    orch._visual_findings = [
        {
            "severity": "major",
            "page": "home",
            "theme": "light",
            "title": "layout is cramped",
            "description": "d",
            "recommendation": "r",
        }
    ]
    assert orch._gate(1, _ev(), None, None, []) == []


def test_blocking_flag_on_low_severity_visual_finding_does_not_block(tmp_path, cfg):
    """info/minor findings with blocking=True must NOT block; critical still does."""
    orch, _ = _orch(tmp_path, cfg)
    orch._visual_findings = [
        {
            "severity": "info",
            "blocking": True,
            "page": "home",
            "theme": "light",
            "title": "cosmetic nit",
            "description": "d",
            "recommendation": "r",
        },
        {
            "severity": "minor",
            "blocking": True,
            "page": "chat",
            "theme": "dark",
            "title": "minor nit",
            "description": "d",
            "recommendation": "r",
        },
    ]
    assert orch._gate(1, _ev(), None, None, []) == []

    orch._visual_findings = [
        {
            "severity": "critical",
            "page": "home",
            "theme": "light",
            "title": "blank dashboard",
            "description": "d",
            "recommendation": "r",
        }
    ]
    problems = orch._gate(1, _ev(), None, None, [])
    assert [p["kind"] for p in problems] == ["visual"]
    assert "[visual/critical]" in problems[0]["summary"]


def test_blocking_flag_alone_triggers_the_gate_problem(tmp_path, cfg):
    """The blocking flag (set by golden regressions) blocks even at 'major' severity."""
    orch, _ = _orch(tmp_path, cfg)
    orch._visual_findings = [
        {
            "severity": "major",
            "blocking": True,
            "page": "home",
            "theme": "light",
            "golden_regression": True,
            "title": "visual regression on tab 'home'",
            "description": "d",
            "recommendation": "r",
        }
    ]
    problems = orch._gate(1, _ev(), None, None, [])
    assert [p["kind"] for p in problems] == ["visual"]
    assert json.loads(problems[0]["detail"])["golden_regression"] is True
