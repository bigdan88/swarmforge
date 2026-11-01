"""Claude-style chat bubbles: structure, dark/light pairing, AppTest rendering and toggle safety.

Covers the acceptance criteria of the "dashboard.py + CSS: Claude-style message bubbles" issue:
- CHAT_CSS is structural and theme-neutral (no colours, so it survives any toggle);
- every chat-bubble colour rule in DARK_CSS has a matching reset in LIGHT_CSS;
- the Chat page renders distinct user/assistant chat_message bubbles;
- a dark -> light -> dark AppTest cycle leaves no stale dark colour in any <style> block;
- Material icons use the slash format (:material/name:), never the colon format.
"""

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge.config import load_config
from swarmforge.db import Store

from .conftest import base_raw

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"


def _write_cfg(tmp_path, monkeypatch):
    """Config + env isolation for one AppTest session (same pattern as tests/test_dashboard_theme.py)."""
    import streamlit as st

    st.cache_resource.clear()  # the dashboard caches config/store per process
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(base_raw(tmp_path / "work")))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    monkeypatch.delenv("SWARMFORGE_DASH_THEME", raising=False)
    return load_config(str(p))


def _dash(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    from swarmforge import dashboard

    return dashboard


def _styles(at) -> list[str]:
    """Every <style> block the app rendered on the current pass."""
    return [m.value for m in at.markdown if "<style>" in (m.value or "")]


def _rules(css: str) -> dict[str, str]:
    """Parse a CSS block into {'selector, normalized': 'body, normalized'}; comments dropped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = css.replace("<style>", " ").replace("</style>", " ")
    out: dict[str, str] = {}
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        sel = " ".join(m.group(1).split())
        out[sel] = " ".join(m.group(2).split())
    return out


def _props(body: str) -> set[str]:
    return {p.split(":")[0].strip() for p in body.split(";") if ":" in p}


def _hexes(css: str) -> set[str]:
    return set(re.findall(r"#[0-9a-fA-F]{3,8}", css))


# ------------------------------------------------------------- CHAT_CSS structure
def test_chat_css_is_structural_and_theme_neutral(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    css = dash.CHAT_CSS
    assert css.startswith("<style>") and css.rstrip().endswith("</style>")
    for needle in (
        '[data-testid="stChatMessageContent"]',
        "border-radius",
        "max-width",
        "line-height",
        "flex-direction: row-reverse",
        ':has([data-testid="stChatMessageAvatarUser"])',
    ):
        assert needle in css, f"CHAT_CSS missing {needle!r}"
    # a serif-ish heading style inside the bubbles
    assert "font-family" in css and "serif" in css
    # theme-neutral: no hex colour may hide in the structural block
    assert not _hexes(css), f"CHAT_CSS contains colour values: {_hexes(css)}"


# --------------------------------------------------- DARK/LIGHT bubble rule pairing
def test_bubble_rules_paired_between_dark_and_light(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    dark, light = _rules(dash.DARK_CSS), _rules(dash.LIGHT_CSS)
    bubble_sels = [sel for sel in dark if "stChatMessage" in sel]
    assert bubble_sels, "DARK_CSS styles no chat-bubble selector"
    for sel in bubble_sels:
        assert sel in light, f"LIGHT_CSS does not reset chat selector {sel!r}"
        leak = _props(dark[sel]) - _props(light[sel])
        assert not leak, (
            f"chat selector {sel!r}: dark properties not reset light: {leak}"
        )
        for decl in light[sel].split(";"):
            val = decl.split(":", 1)[1].strip().lower() if ":" in decl else ""
            assert val not in dash.DARK_CSS_COLORS, (
                f"light reset of {sel!r} uses a dark colour: {decl}"
            )
    # the dark bubble colours come exclusively from the dark palette sentinel
    for sel in bubble_sels:
        for h in _hexes(dark[sel]):
            assert h in dash.DARK_CSS_COLORS, (
                f"dark bubble rule {sel!r} uses non-palette hex {h}"
            )


# --------------------------------------------------------- Chat page AppTest runs
def test_chat_page_renders_distinct_bubbles(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    Store(cfg.db_path).create_run(
        "20990101-000000-aaaaaa",
        state="COMPLETED",
        issue_title="Export CSV",
        repo="x/y",
        budget_usd=5,
        state_reason="done",
        config_json="{}",
    )

    def fake_ask(c, s, question, history=None, **kw):
        return {
            "answer": "ok!",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
        }

    from swarmforge import chatbot

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="nav_chat").click().run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("how many runs?").run()
    assert not at.exception, [e.value for e in at.exception]
    # one user bubble and one assistant bubble, in order
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    texts = [m.value for m in at.markdown]
    assert any("ok!" in t for t in texts), "assistant reply did not render"


def test_chat_css_injected_in_every_theme(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dash(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # auto mode: no theme override, but the structural chat CSS is always injected
    assert any(dash.CHAT_CSS in s for s in _styles(at))
    assert not any(dash.DARK_CSS in s for s in _styles(at))
    at.session_state["theme_mode"] = "dark"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    styles = _styles(at)
    assert any(dash.CHAT_CSS in s for s in styles)
    assert any(dash.DARK_CSS in s for s in styles)


def test_toggle_dark_light_dark_leaves_no_stale_bubble_colors(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dash(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    # dark pass: the dark override renders, including the assistant bubble colour
    at.session_state["theme_mode"] = "dark"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    dark_styles = _styles(at)
    assert any(dash.DARK_CSS in s for s in dark_styles)
    assert any(
        '[data-testid="stChatMessageContent"] { background-color: #16181d' in s
        for s in dark_styles
    ), "dark assistant-bubble rule not rendered"

    # light pass: the dark block is gone and no dark hex survives in ANY style block
    at.session_state["theme_mode"] = "light"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    light_styles = _styles(at)
    assert any(dash.LIGHT_CSS in s for s in light_styles)
    assert not any(dash.DARK_CSS in s for s in light_styles)
    for c in dash.DARK_CSS_COLORS:
        assert not any(c in s for s in light_styles), (
            f"stale dark colour {c} survived in light mode"
        )
    # the light bubble reset is present with its light palette value
    light_rules = _rules(dash.LIGHT_CSS)
    assert '[data-testid="stChatMessageContent"]' in light_rules
    assert (
        "background-color: #f5f2ec"
        in light_rules['[data-testid="stChatMessageContent"]']
    )

    # back to dark: the dark override (and the user-bubble colour) return
    at.session_state["theme_mode"] = "dark"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    dark2 = _styles(at)
    assert any(dash.DARK_CSS in s for s in dark2)
    assert any("background-color: #1a1c22" in s for s in dark2), (
        "user-bubble dark rule not restored"
    )


# ------------------------------------------------------------- Material icon format
def test_material_icons_use_slash_format():
    src = APP.read_text(encoding="utf-8")
    assert ":material:" not in src, (
        "colon-format Material icon found (StreamlitAPIException risk)"
    )
    assert ":material/person:" in src
    assert ":material/auto_awesome:" in src
    from swarmforge import dashboard

    for role, avatar in dashboard.CHAT_AVATARS.items():
        assert re.fullmatch(r":material/[a-z_]+:", avatar), (
            f"avatar for {role!r} not slash format"
        )
