"""Offline tests for the sandbox browser probe's opt-in a11y mode (visual._browse_probe).

The Chat-page AppTest and label_findings regression live in tests/test_a11y_probe.py;
these tests cover the probe flag forwarding, the probe script contract, the F1
empty-snapshot regression, the protected-file guard and the preview.a11y key.
"""

import json
from pathlib import Path

import pytest

from swarmforge import visual
from swarmforge.agent import devtools
from swarmforge.agent.tools import ToolContext
from swarmforge.config import SandboxConfig
from swarmforge.sandbox import CommandResult, LocalSandbox

BROWSE_PY = Path(devtools.__file__).parent.parent / "resources" / "browse.py"
PKG_DIR = Path(devtools.__file__).parent.parent


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(
        workspace=ws,
        sandbox=LocalSandbox(SandboxConfig(mode="local")),
        artifacts_dir=tmp_path / "artifacts",
    )


class _ProbeSession:
    """Fake sandbox session: records SF_BROWSE_ARGS and echoes a minimal probe payload."""

    python = "python"
    artifacts_path = None

    def __init__(self):
        self.captured = []

    async def exec(self, command, *, timeout_s=60, stdin=None, env=None):
        self.captured.append(json.loads(env["SF_BROWSE_ARGS"]))
        payload = {
            "url": "http://localhost:8501/",
            "status": 200,
            "text": "ok",
            "a11y": {"role": "WebArea", "name": "", "children": []},
            "contrast": [],
        }
        out = devtools.BROWSE_MARK + json.dumps(payload)
        return CommandResult(command, 0, out, 0.1)


@pytest.mark.asyncio
async def test_browse_probe_forwards_a11y_flag_only_when_requested(ctx, monkeypatch):
    s = _ProbeSession()
    monkeypatch.setattr(devtools, "open_session", lambda c: s)
    data = await visual._browse_probe(ctx, "http://localhost:8501/", a11y=True)
    assert s.captured[0]["a11y"] is True
    assert data["a11y"] == {"role": "WebArea", "name": "", "children": []}
    assert data["contrast"] == []
    # default: flag absent (opt-in)
    await visual._browse_probe(ctx, "http://localhost:8501/")
    assert not s.captured[1].get("a11y")  # opt-in: falsy by default


@pytest.mark.asyncio
async def test_browse_probe_without_a11y_keeps_payload_shape(ctx, monkeypatch):
    s = _ProbeSession()
    monkeypatch.setattr(devtools, "open_session", lambda c: s)
    data = await visual._browse_probe(ctx, "http://localhost:8501/")
    assert data["status"] == 200 and data["text"] == "ok"


def test_probe_script_has_guarded_a11y_block():
    src = BROWSE_PY.read_text()
    # the a11y block sits after the controls capture and is failure-guarded
    assert 'result["controls"] = page.evaluate(CONTROL_JS)' in src
    assert 'if args.get("a11y"):' in src
    assert "page.accessibility.snapshot() or {}" in src
    assert (
        "aria_snapshot()" in src
    )  # modern Playwright ARIA snapshot, with legacy fallback
    assert 'result["a11y"] = None' in src
    assert 'result["contrast"] = []' in src
    # both keys are initialized up front so the single-line emit contract is unchanged
    assert '"a11y": None, "contrast": []' in src


def test_probe_contrast_js_contract():
    src = BROWSE_PY.read_text()
    start = src.index("CONTRAST_JS = ")
    block = src[start : src.index('"""', start + len("CONTRAST_JS = ") + 3)]
    for key in ("selector", "text", "color", "background"):
        assert key in block
    # documented behavior: skip hidden/zero-size/empty-text, cap at 200, white fallback
    assert "getBoundingClientRect" in block and "visibility" in block
    assert "out.length >= 200" in block
    assert "'#ffffff'" in block


# --------------------------------------------------------------- F1 regression
def test_empty_a11y_snapshot_still_runs_label_findings(monkeypatch):
    """F1: an empty-but-successful snapshot ({}) must NOT be treated as a probe failure."""
    from swarmforge import a11y

    calls = {"label": [], "contrast": []}
    monkeypatch.setattr(
        a11y,
        "label_findings",
        lambda tab, theme, snap: calls["label"].append(snap) or [],
    )
    monkeypatch.setattr(
        a11y,
        "contrast_findings",
        lambda tab, theme, pairs, min_ratio=4.5: calls["contrast"].append(pairs) or [],
    )
    visual._a11y_findings_for("chat", "light", {"ok": True, "a11y": {}, "contrast": []})
    assert calls["label"] == [{}], "the empty snapshot must still be audited"
    assert calls["contrast"] == [[]]


def test_none_a11y_snapshot_skips_label_findings(monkeypatch):
    """None means the probe failed (or a11y was not requested): label findings are skipped."""
    from swarmforge import a11y

    calls = {"label": [], "contrast": []}
    monkeypatch.setattr(
        a11y,
        "label_findings",
        lambda tab, theme, snap: calls["label"].append(snap) or [],
    )
    monkeypatch.setattr(
        a11y,
        "contrast_findings",
        lambda tab, theme, pairs, min_ratio=4.5: calls["contrast"].append(pairs) or [],
    )
    visual._a11y_findings_for("chat", "light", {"ok": True, "a11y": None})
    assert calls["label"] == [], "a None snapshot must skip label findings"
    assert calls["contrast"] == [[]]


# --------------------------------------------------------------- protected files
@pytest.mark.parametrize("rel", ["agent/devtools.py", "config.py", "orchestrator.py"])
def test_protected_files_contain_no_a11y_code(rel):
    """The a11y seam lives in visual.py/a11y.py; the protected core files must stay clean."""
    src = (PKG_DIR / rel).read_text(encoding="utf-8")
    assert "a11y" not in src, f"{rel} must not contain a11y code (protected file)"


# --------------------------------------------------------------- preview.a11y key
def test_parse_preview_a11y_key():
    base = {"command": "streamlit run app.py", "port": 8501}
    assert visual.parse_preview(base).a11y is False  # default: off
    assert visual.parse_preview({**base, "a11y": True}).a11y is True
    with pytest.raises(visual.PreviewError):
        visual.parse_preview({**base, "a11y": "yes"})
