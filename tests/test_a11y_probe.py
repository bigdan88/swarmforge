"""Accessibility probe tests: browse_raw's a11y passthrough, the sandbox probe script contract,
and the Chat page's accessible names. Offline except for the optional real-browser probe test."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from swarmforge.agent import devtools
from swarmforge.sandbox import CommandResult

from .test_dashboard import APP, _go, _write_cfg

BROWSE_PY = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "swarmforge"
    / "resources"
    / "browse.py"
)


# --------------------------------------------------------------- fake session
class _FakeSession(devtools.Session):
    """Captures the SF_BROWSE_ARGS env browse_raw sends and echoes a probe result line."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.python = sys.executable
        self.artifacts_path = None
        self.env: dict | None = None

    async def exec(self, command, *, timeout_s=60, stdin=None, env=None):
        self.env = env or {}
        line = devtools.BROWSE_MARK + json.dumps(self.payload)
        return CommandResult(command=command, exit_code=0, output=line, duration_s=0.01)


@pytest.fixture
def ctx(tmp_path):
    from swarmforge.agent.tools import ToolContext
    from swarmforge.config import SandboxConfig
    from swarmforge.sandbox import LocalSandbox

    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(
        workspace=ws,
        sandbox=LocalSandbox(SandboxConfig(mode="local")),
        artifacts_dir=tmp_path / "artifacts",
    )


async def _captured_args(ctx, monkeypatch, payload, **kwargs):
    sess = _FakeSession(payload)
    monkeypatch.setattr(devtools, "open_session", lambda _ctx: sess)
    data = await devtools.browse_raw(ctx, "http://localhost:8501/", **kwargs)
    assert sess.env is not None, "browse_raw must pass SF_BROWSE_ARGS to the probe"
    return json.loads(sess.env["SF_BROWSE_ARGS"]), data


async def test_browse_raw_sends_a11y_flag_only_when_requested(ctx, monkeypatch):
    payload = {
        "url": "http://localhost:8501/",
        "status": 200,
        "title": "App",
        "text": "ok",
        "a11y": {"role": "WebArea", "name": "App", "children": []},
        "contrast": [
            {
                "selector": "p",
                "text": "ok",
                "color": "rgb(0, 0, 0)",
                "background": "rgb(255, 255, 255)",
            }
        ],
    }
    args, data = await _captured_args(ctx, monkeypatch, payload, a11y=True)
    assert args["a11y"] is True
    # the raw passthrough returns the probe's a11y snapshot and contrast pairs untouched
    assert data["a11y"] == payload["a11y"]
    assert data["contrast"] == payload["contrast"]

    args, data = await _captured_args(
        ctx, monkeypatch, {**payload, "a11y": None, "contrast": []}
    )
    assert args["a11y"] is False  # opt-in: off unless requested
    assert data["a11y"] is None and data["contrast"] == []


# --------------------------------------------------------------- probe contract
def test_probe_script_a11y_contract():
    src = BROWSE_PY.read_text(encoding="utf-8")
    # both keys are initialized up front so the single-line __SF_BROWSE__ emit never breaks
    assert '"a11y": None, "contrast": []' in src
    # the a11y block only runs when requested and every step is guarded (never a fatal error)
    assert 'if args.get("a11y"):' in src
    a11y_block = src.split('if args.get("a11y"):')[1].split(
        'shot = args.get("screenshot")'
    )[0]
    assert 'result["a11y"]' in a11y_block and 'result["a11y"] = None' in a11y_block
    assert (
        'result["contrast"]' in a11y_block and 'result["contrast"] = []' in a11y_block
    )
    assert (
        a11y_block.count("except Exception") >= 2
    )  # snapshot and contrast are each guarded
    # the contrast JS is inlined (the probe cannot import swarmforge.a11y) and returns the
    # documented keys for visible text only
    assert "CONTRAST_JS" in src
    contrast_js = src.split('CONTRAST_JS = """')[1].split('"""')[0]
    for key in ("selector", "text", "color", "background"):
        assert key in contrast_js
    for guard in (
        "getComputedStyle",
        "display === 'none'",
        "visibility === 'hidden'",
        "r.width",
        "r.height",
        "'#ffffff'",
    ):
        assert guard in contrast_js
    assert "out.length >= 200" in contrast_js  # capped at 200 pairs
    assert "slice(0, 60)" in contrast_js  # text capped at 60 chars


def _has_browser() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(args=["--no-sandbox"]).close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _has_browser(), reason="playwright/chromium not installed")
def test_probe_returns_a11y_snapshot_and_contrast_pairs(tmp_path):
    """End to end against the real probe script: a11y=True yields the snapshot and contrast pairs
    in one __SF_BROWSE__ line; a11y=False leaves them None/[] (the emit contract is unchanged)."""
    html = (
        "<html><head><title>A11y</title></head><body><h1>Demo</h1>"
        "<p style='color:#777777'>gray text</p>"
        "<button aria-label='Named'>n</button>"
        "<button><span class='material-symbols-rounded'>edit</span></button>"  # no accessible name
        "</body></html>"
    )
    page = tmp_path / "index.html"
    page.write_text(html)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(
        ("127.0.0.1", port),
        type(
            "H",
            (BaseHTTPRequestHandler,),
            {
                "do_GET": lambda self: (
                    self.send_response(200),
                    self.send_header("Content-Type", "text/html"),
                    self.end_headers(),
                    self.wfile.write(html.encode()),
                )
            },
        ),
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:

        def run_probe(a11y: bool) -> dict:
            env = dict(os.environ)
            env["SF_BROWSE_ARGS"] = json.dumps(
                {
                    "url": f"http://127.0.0.1:{port}/index.html",
                    "timeout_ms": 20000,
                    "a11y": a11y,
                }
            )
            r = subprocess.run(
                [sys.executable, "-"],
                input=BROWSE_PY.read_text(encoding="utf-8"),
                capture_output=True,
                text=True,
                env=env,
                timeout=90,
                check=False,
            )
            lines = [
                ln for ln in r.stdout.splitlines() if ln.startswith("__SF_BROWSE__")
            ]
            assert len(lines) == 1, "the probe must emit exactly one __SF_BROWSE__ line"
            return json.loads(lines[0][len("__SF_BROWSE__") :])

        data = run_probe(True)
        assert not data.get("fatal")
        # modern Playwright returns a YAML aria snapshot (str); older versions a dict
        assert data["a11y"], data
        assert isinstance(data["contrast"], list) and data["contrast"]
        for pair in data["contrast"]:
            assert set(pair) == {"selector", "text", "color", "background"}
            assert pair["text"] and len(pair["text"]) <= 60
        assert any(p["text"] == "gray text" and p["color"] for p in data["contrast"])

        data = run_probe(False)
        assert data["a11y"] is None and data["contrast"] == []
    finally:
        srv.shutdown()


# --------------------------------------------------------------- Chat page names
def _open_chat(at):
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


def _chat_app(tmp_path, monkeypatch):
    from swarmforge.db import Store

    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run(
        "20990101-000000-aaaaaa",
        state="COMPLETED",
        issue_title="Export CSV",
        repo="x/y",
        budget_usd=5,
        state_reason="done",
        config_json="{}",
    )
    from swarmforge import chatbot

    chatbot.create_conversation(store, "Demo thread")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return _open_chat(at)


def test_chat_page_buttons_have_accessible_names(tmp_path, monkeypatch):
    at = _chat_app(tmp_path, monkeypatch)
    unnamed = [
        b.key
        for b in at.button
        if not (b.label or "").strip() and not (b.help or "").strip()
    ]
    assert not unnamed, f"buttons without an accessible name (label or help): {unnamed}"
    # the icon-bearing controls the audit targets all carry an explicit name or help text
    by_key = {b.key: b for b in at.button}
    assert (by_key["chat_reset"].label or "").strip() == "Clear" and (
        by_key["chat_reset"].help or ""
    ).strip()
    assert any((b.label or "").startswith("Start a project") for b in at.button)
    assert all(
        (b.label or "").strip()
        for b in at.button
        if (b.key or "").startswith("chat_q_")
    )
    assert any((t.label or "").strip() for t in at.toggle)
    assert any(
        (u.label or "").strip() and (u.help or "").strip() for u in at.file_uploader
    )
    assert any((c.placeholder or "").strip() for c in at.chat_input)


def _chat_snapshot_from(at, strip_help_keys: set[str]) -> dict:
    """Hand-built Playwright-style a11y snapshot mirroring the Chat page's interactive DOM."""
    children = []
    for b in at.button:
        name = (b.label or "").strip()
        if not name and b.key in strip_help_keys:
            name = (
                ""  # icon-only button whose help/label was stripped: no accessible name
            )
        elif not name:
            name = (b.help or "").strip()
        children.append({"role": "button", "name": name})
    for t in at.toggle:
        children.append({"role": "switch", "name": (t.label or "").strip()})
    for u in at.file_uploader:
        children.append({"role": "region", "name": (u.label or "").strip()})
    for c in at.chat_input:
        children.append({"role": "textbox", "name": (c.placeholder or "").strip()})
    return {"role": "WebArea", "name": "Chat", "children": children}


def test_nameless_icon_button_is_flagged_major(tmp_path, monkeypatch):
    """Regression: an icon-only button without a label/help is a [major] a11y finding for the chat
    page, consumable by state.repair_tasks_from_findings."""
    a11y = pytest.importorskip(
        "swarmforge.a11y", reason="the a11y analysis core provides label_findings"
    )
    at = _chat_app(tmp_path, monkeypatch)
    edit_key = next(
        (b.key for b in at.button if (b.key or "").startswith("chat_edit_")), None
    )
    assert edit_key is not None, "Chat rail must render a chat_edit_<id> button"
    snapshot = _chat_snapshot_from(at, strip_help_keys={edit_key})
    assert any(c["role"] == "button" and not c["name"] for c in snapshot["children"])

    findings = a11y.label_findings("chat", "light", snapshot)
    majors = [f for f in findings if (f.get("severity") or "").lower() == "major"]
    assert len(majors) == 1, findings
    assert majors[0].get("file") == "src/swarmforge/dashboard_pages/chat.py"
    # the finding is a repair-task-ready Finding dict
    assert majors[0].get("severity") == "major" and majors[0].get("blocking") is True


def test_fully_labelled_chat_snapshot_has_no_major_label_findings(
    tmp_path, monkeypatch
):
    a11y = pytest.importorskip(
        "swarmforge.a11y", reason="the a11y analysis core provides label_findings"
    )
    at = _chat_app(tmp_path, monkeypatch)
    snapshot = _chat_snapshot_from(at, strip_help_keys=set())
    findings = a11y.label_findings("chat", "light", snapshot)
    assert not [f for f in findings if (f.get("severity") or "").lower() == "major"], (
        findings
    )
