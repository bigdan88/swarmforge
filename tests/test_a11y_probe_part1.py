"""Offline tests for the sandbox browser probe's opt-in a11y mode (browse_raw passthrough).

The Chat-page AppTest and label_findings regression live in tests/test_a11y_probe.py (T2-2);
these tests cover the devtools flag forwarding and the probe script contract.
"""
import json
from pathlib import Path

import pytest

from swarmforge.agent import devtools
from swarmforge.agent.tools import ToolContext
from swarmforge.config import SandboxConfig
from swarmforge.sandbox import CommandResult, LocalSandbox

BROWSE_PY = Path(devtools.__file__).parent.parent / "resources" / "browse.py"


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(workspace=ws, sandbox=LocalSandbox(SandboxConfig(mode="local")),
                       artifacts_dir=tmp_path / "artifacts")


class _ProbeSession:
    """Fake sandbox session: records SF_BROWSE_ARGS and echoes a minimal probe payload."""
    python = "python"
    artifacts_path = None

    def __init__(self):
        self.captured = []

    async def exec(self, command, *, timeout_s=60, stdin=None, env=None):
        self.captured.append(json.loads(env["SF_BROWSE_ARGS"]))
        payload = {"url": "http://localhost:8501/", "status": 200, "text": "ok",
                   "a11y": {"role": "WebArea", "name": "", "children": []}, "contrast": []}
        out = devtools.BROWSE_MARK + json.dumps(payload)
        return CommandResult(command, 0, out, 0.1)


@pytest.mark.asyncio
async def test_browse_raw_forwards_a11y_flag_only_when_requested(ctx, monkeypatch):
    s = _ProbeSession()
    monkeypatch.setattr(devtools, "open_session", lambda c: s)
    data = await devtools.browse_raw(ctx, "http://localhost:8501/", a11y=True)
    assert s.captured[0]["a11y"] is True
    assert data["a11y"] == {"role": "WebArea", "name": "", "children": []}
    assert data["contrast"] == []
    # default: flag absent (opt-in)
    await devtools.browse_raw(ctx, "http://localhost:8501/")
    assert not s.captured[1]["a11y"]  # opt-in: falsy by default


@pytest.mark.asyncio
async def test_browse_raw_without_a11y_keeps_payload_shape(ctx, monkeypatch):
    s = _ProbeSession()
    monkeypatch.setattr(devtools, "open_session", lambda c: s)
    data = await devtools.browse_raw(ctx, "http://localhost:8501/")
    assert data["status"] == 200 and data["text"] == "ok"


def test_probe_script_has_guarded_a11y_block():
    src = BROWSE_PY.read_text()
    # the a11y block sits after the controls capture and is failure-guarded
    assert 'result["controls"] = page.evaluate(CONTROL_JS)' in src
    assert 'if args.get("a11y"):' in src
    assert 'page.accessibility.snapshot() or {}' in src
    assert 'aria_snapshot()' in src  # modern Playwright ARIA snapshot, with legacy fallback
    assert 'result["a11y"] = None' in src
    assert 'result["contrast"] = []' in src
    # both keys are initialized up front so the single-line emit contract is unchanged
    assert '"a11y": None, "contrast": []' in src


def test_probe_contrast_js_contract():
    src = BROWSE_PY.read_text()
    start = src.index("CONTRAST_JS = ")
    block = src[start:src.index('"""', start + len("CONTRAST_JS = ") + 3)]
    for key in ("selector", "text", "color", "background"):
        assert key in block
    # documented behavior: skip hidden/zero-size/empty-text, cap at 200, white fallback
    assert "getBoundingClientRect" in block and "visibility" in block
    assert "out.length >= 200" in block
    assert "'#ffffff'" in block
