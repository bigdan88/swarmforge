"""Acceptance tests for AC5 of "Pi harness: loadable skills/playbooks from the knowledge base".

AC5 requires the full suite to be green on the current base, and in particular that
tests/test_visual.py::test_run_screenshots_light_and_dark_and_vision_review_is_listed_not_blocking
(a) no longer depends on the exact ``c0-*`` screenshot filename prefix and (b) skips cleanly
(not fails) when headless Chromium is unavailable.

These fast, offline tests cover the two product mechanisms that make that true:

* ``swarmforge.visual.capture`` names screenshots ``visual/c{cycle}-...`` using the cycle it
  is *given* (browser and app fully faked -- no Chromium, no server, no network);
* ``Orchestrator._visual_review`` turns any capture failure (e.g. headless Chromium not
  installed) into a logged ``could not take screenshots`` warning and an empty findings list
  instead of a failed run -- exactly the event the slow visual test checks to skip rather
  than fail, and the prefix its filename assertion derives from is the cycle actually run.
"""

import asyncio
import json

import pytest

from swarmforge import visual
from swarmforge.agent.tools import ToolError
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator

# ---------------------------------------------------------------- capture naming (cycle-agnostic)


def _pv():
    return visual.parse_preview({"command": "x", "port": 8501, "pages": [{"path": "/", "name": "home"}]})


def _fake_devtools(monkeypatch, browse_calls):
    """Replace the browser/process layer so capture() never touches Chromium or a real app."""
    from swarmforge.agent import devtools

    async def fake_start(ctx, name, command, **kw):
        return f"process '{name}' RUNNING and ready on port {kw.get('ready_port')}"

    async def fake_browse(ctx, url, **kw):
        browse_calls.append(kw)
        return {"screenshot_file": f"/artifacts/{kw['screenshot_name']}", "exceptions": [],
                "app_errors": [], "page_errors": [], "action_errors": [], "console": [],
                "text": "Inventory"}

    async def fake_stop(ctx, name):
        return f"stopped '{name}'"

    monkeypatch.setattr(devtools, "start_process", fake_start)
    monkeypatch.setattr(devtools, "browse_raw", fake_browse)
    monkeypatch.setattr(devtools, "stop_process", fake_stop)


def test_capture_names_screenshots_with_the_cycle_it_is_given(monkeypatch):
    """AC5: filenames carry the *actual* cycle number -- nothing is hardcoded to a c0 prefix."""
    calls = []
    _fake_devtools(monkeypatch, calls)
    shots = asyncio.run(visual.capture(object(), _pv(), 7))

    assert [c["screenshot_name"] for c in calls] == [
        "visual/c7-home-light.png", "visual/c7-home-dark.png"]
    assert [s["file"] for s in shots] == [
        "/artifacts/visual/c7-home-light.png", "/artifacts/visual/c7-home-dark.png"]
    # The old test assumed every shot started with "c0-"; that assumption must not hold
    # for a later cycle, which is why a cycle-agnostic derivation is required.
    assert not any(c["screenshot_name"].startswith("visual/c0-") for c in calls)


def test_capture_names_cycle_0_shots_with_c0_prefix(monkeypatch):
    """Same mechanism at cycle 0 (what the current e2e visual test exercises)."""
    calls = []
    _fake_devtools(monkeypatch, calls)
    asyncio.run(visual.capture(object(), _pv(), 0))
    assert calls and all(c["screenshot_name"].startswith("visual/c0-") for c in calls)


def test_capture_raises_tool_error_when_the_app_never_comes_up(monkeypatch):
    """A dead preview surfaces as ToolError, which _visual_review converts into a skip."""
    from swarmforge.agent import devtools

    async def failing_start(ctx, name, command, **kw):
        return "ERROR: process exited before becoming ready: playwright not usable"

    monkeypatch.setattr(devtools, "start_process", failing_start)
    with pytest.raises(ToolError):
        asyncio.run(visual.capture(object(), _pv(), 1))


# ------------------------------------------------- orchestrator skips when Chromium is unavailable


class _FakeWorkspace:
    repo = None  # set per test via the fixture's tmp_path
    base_sha = "0" * 40

    def __init__(self, repo):
        self.repo = repo

    def changed_files(self):
        return ["app.py"]  # should_capture: no when_changed -> always capture


def _orch(tmp_path, cfg):
    store = Store(tmp_path / "db")
    orch = Orchestrator(cfg, store, echo=None)
    orch.run_id = "r-visual-skip"
    orch.ws = _FakeWorkspace(tmp_path)
    orch.artifacts = tmp_path / "artifacts"
    orch.sandbox = None
    orch.preview = _pv()
    return orch, store


@pytest.mark.parametrize("exc", [
    OSError("Playwright browser is not installed; run: playwright install chromium"),
    RuntimeError("headless chromium crashed"),
    ToolError("the app did not come up for screenshots"),
])
def test_visual_review_skips_cleanly_when_capture_fails(tmp_path, cfg, monkeypatch, exc):
    """AC5: no headless Chromium -> logged 'could not take screenshots', run continues, no findings.

    tests/test_visual.py skips (instead of failing) precisely when no screenshot event exists
    and this 'could not take screenshots' warning is present; verify that mechanism here.
    """
    orch, store = _orch(tmp_path, cfg)

    async def broken_capture(ctx, pv, cycle, log=None):
        raise exc

    monkeypatch.setattr(visual, "capture", broken_capture)
    findings = asyncio.run(orch._visual_review(2))  # cycle 2: repair cycle must not break it either

    assert findings == []
    events = store.events("r-visual-skip")
    skips = [e for e in events if e["kind"] == "visual" and "could not take screenshots" in e["message"]]
    assert len(skips) == 1 and skips[0]["level"] == "warn"
    assert str(exc) in skips[0]["message"]


def test_visual_review_records_shots_with_the_actual_cycle(tmp_path, cfg, monkeypatch):
    """On success the visual event carries the cycle it was given (c1 here, not a fixed c0)."""
    orch, store = _orch(tmp_path, cfg)

    async def ok_capture(ctx, pv, cycle, log=None):
        return [{"page": "home", "path": "/", "theme": t, "url": "http://localhost:8501/",
                 "file": f"/artifacts/visual/c{cycle}-home-{t}.png", "problems": [], "text": "ok"}
                for t in ("light", "dark")]

    monkeypatch.setattr(visual, "capture", ok_capture)
    findings = asyncio.run(orch._visual_review(1))

    assert findings == []  # no page errors, no vision model configured -> nothing blocking
    events = store.events("r-visual-skip")
    shot_events = [e for e in events if "screenshot(s) taken" in e["message"]]
    assert len(shot_events) == 1 and "(cycle 1)" in shot_events[0]["message"]
    data = json.loads(shot_events[0]["data"])
    assert data["cycle"] == 1
    assert all("/visual/c1-" in s["file"] for s in data["shots"])
