"""Supplementary acceptance tests for the Claude-style chat-bubbles issue.

tests/test_chat_styling.py (AC4) already checks CHAT_CSS structure, DARK/LIGHT rule pairing,
injection on the Work page and the dark->light->dark toggle cycle. This file closes the remaining
gaps in the acceptance criteria:

- AC1: DARK_CSS and LIGHT_CSS each give *user* and *assistant* bubbles distinct background colors
  (pairing alone does not prove distinctness), and the light bubble colors are not dark-palette hexes.
- AC3: the Chat page actually renders the slash-format Material avatars on the chat_message bubbles
  (AppTest exposes ChatMessage.avatar).
- AC2/AC1: on the Chat page itself, CHAT_CSS is injected in light mode, a full dark->light->dark
  cycle there leaves no stale DARK_CSS_COLORS hex in any rendered <style> block, and the user and
  assistant bubbles keep rendering through the cycle.

Also included: test_user_bubble_css_matches_dom_rendered_with_material_avatars, a DOM-fidelity
check for AC1's *rendered* alignment. It currently FAILS and documents a defect: Streamlit renders
data-testid="stChatMessageAvatarCustom" (not stChatMessageAvatarUser) whenever chat_message is
called with a ':material/...' avatar -- which the Chat page does -- so the user-bubble rules never
match and user bubbles stay left-aligned with the assistant background (verified with Playwright
computed styles against the running dashboard).
"""

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge.config import load_config

from .conftest import base_raw

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"


def _write_cfg(tmp_path, monkeypatch):
    """Config + env isolation for one AppTest session (same pattern as tests/test_chat_styling.py)."""
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


def _styles(at) -> list[str]:
    """Every <style> block the app rendered on the current pass."""
    return [m.value for m in at.markdown if "<style>" in (m.value or "")]


def _bubble_backgrounds(css: str):
    """(assistant_bg, user_bg) lowercased hexes from a theme block's chat-bubble rules."""
    assistant = user = ""
    for line in css.splitlines():
        s = line.strip()
        m = re.search(r"background-color:\s*(#[0-9a-fA-F]+)", s)
        if not m:
            continue
        if s.startswith('[data-testid="stChatMessageContent"] {'):
            assistant = m.group(1).lower()
        elif (
            "stChatMessageAvatarUser" in s
            and '[data-testid="stChatMessageContent"] {' in s
        ):
            user = m.group(1).lower()
    return assistant, user


def _fake_chat(monkeypatch):
    from swarmforge import chatbot

    def fake_ask(c, s, question, history=None, **kw):
        return {
            "answer": "pong!",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
        }

    monkeypatch.setattr(chatbot, "ask", fake_ask)


# ------------------------------------------------------------------ AC1 distinct bubble colors
def test_user_and_assistant_bubbles_get_distinct_colors_in_each_palette(
    tmp_path, monkeypatch
):
    _write_cfg(tmp_path, monkeypatch)
    from swarmforge import dashboard as dash

    for css, name in ((dash.DARK_CSS, "dark"), (dash.LIGHT_CSS, "light")):
        assistant, user = _bubble_backgrounds(css)
        assert assistant and user, (
            f"{name} theme defines no user+assistant bubble background colors"
        )
        assert user != assistant, (
            f"{name} palette: user and assistant bubbles share background {user}"
        )

    # dark bubble colors must come from the declared dark palette
    d_assistant, d_user = _bubble_backgrounds(dash.DARK_CSS)
    assert d_assistant in dash.DARK_CSS_COLORS and d_user in dash.DARK_CSS_COLORS

    # the light reset must not reuse any dark-palette hex on the bubbles
    l_assistant, l_user = _bubble_backgrounds(dash.LIGHT_CSS)
    assert l_assistant not in dash.DARK_CSS_COLORS, (
        "light assistant bubble keeps a dark hex"
    )
    assert l_user not in dash.DARK_CSS_COLORS, "light user bubble keeps a dark hex"


# ------------------------------------------------- AC1 alignment: does the user selector ever match?
#
# DOM fact, verified live against the running dashboard (Playwright computed styles) and against
# the installed Streamlit 1.66.1 frontend bundle (static/**/js/index.*.js):
#   * st.chat_message("user", avatar=":material/person:") renders the avatar with
#     data-testid="stChatMessageAvatarCustom" — NOT stChatMessageAvatarUser. The
#     stChatMessageAvatarUser testid only appears for the built-in avatar string "user".
#   * the only role discriminator the row DOM exposes for custom avatars is
#     [data-testid="stChatMessageContent"][aria-label="Chat message from user"].
# Consequence (defect): every user-bubble rule keyed solely on stChatMessageAvatarUser is dead
# code on the Chat page — user bubbles stay left-aligned and share the assistant background
# (measured: flex-direction row, background rgb(245,242,236) == assistant bg, in both palettes).
def _user_bubble_rules(dash) -> list[str]:
    rules = []
    for name in ("CHAT_CSS", "DARK_CSS", "LIGHT_CSS"):
        for line in getattr(dash, name).splitlines():
            s = line.strip()
            if s.startswith("[data-testid") and "stChatMessageAvatarUser" in s:
                rules.append(f"{name}: {s}")
    return rules


def test_streamlit_material_avatars_render_custom_testid():
    """Premise of the next test, checked against the installed Streamlit frontend bundle."""
    pytest.importorskip("streamlit")
    import streamlit

    js = None
    for cand in sorted(Path(streamlit.__file__).parent.glob("static/**/index.*.js")):
        try:
            text = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "stChatMessageAvatarUser" in text:
            js = text
            break
    if js is None:
        pytest.skip("Streamlit frontend bundle not found in this install")
    i = js.index("stChatMessageAvatarUser")
    window = js[max(0, i - 2000) : i + 2000]
    # the avatar component maps ':material/...' avatar strings to stChatMessageAvatarCustom,
    # while stChatMessageAvatarUser is reserved for the built-in `avatar="user"` icon
    assert "stChatMessageAvatarCustom" in window, (
        "expected ':material' avatars to render a Custom testid"
    )
    assert ":material" in window


def test_user_bubble_css_matches_dom_rendered_with_material_avatars(
    tmp_path, monkeypatch
):
    """AC1: user messages must *render* right-aligned with a distinct bubble color.

    The rules key on stChatMessageAvatarUser, which the DOM never contains when chat_message is
    called with the ':material/person:' avatar the avatars use (see the bundle test above). They
    must also match the rendered DOM, e.g. via the content aria-label discriminator.
    """
    _write_cfg(tmp_path, monkeypatch)
    from swarmforge import dashboard as dash

    assert dash.CHAT_AVATARS["user"].startswith(":material/"), (
        "premise: user avatar is a custom Material icon"
    )
    rules = _user_bubble_rules(dash)
    assert rules, "no user-bubble rules found in CHAT_CSS/DARK_CSS/LIGHT_CSS"
    dead = [r for r in rules if "Chat message from user" not in r]
    assert not dead, (
        'these user-bubble rules key only on [data-testid="stChatMessageAvatarUser"], a testid '
        "Streamlit does not render when chat_message gets a custom :material avatar (as CHAT_AVATARS "
        "does): in the live app the user bubbles stay left-aligned and keep the assistant background. "
        "Match the rendered DOM instead, e.g. "
        '[data-testid="stChatMessageContent"][aria-label="Chat message from user"]. '
        f"offending rules: {dead}"
    )


# ------------------------------------------------------------------ AC3 rendered slash avatars
def test_chat_page_bubbles_render_material_slash_avatars(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    _fake_chat(monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="nav_chat").click().run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("ping").run()
    assert not at.exception, [e.value for e in at.exception]
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    assert at.chat_message[0].avatar == ":material/person:"
    assert at.chat_message[1].avatar == ":material/auto_awesome:"
    # no colon-format Material icon ever reaches a rendered avatar
    for m in at.chat_message:
        assert ":material:" not in (m.avatar or "")


# ------------------------------------------------- AC1/AC2 toggle cycle on the Chat page itself
def test_chat_page_toggle_cycle_resets_bubble_colors(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    from swarmforge import dashboard as dash

    _fake_chat(monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="nav_chat").click().run()
    at.chat_input[0].set_value("ping").run()
    assert not at.exception, [e.value for e in at.exception]

    # dark pass on the Chat page: dark override + always-on structural CSS
    at.session_state["theme_mode"] = "dark"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    styles = _styles(at)
    assert any(dash.DARK_CSS in s for s in styles), (
        "DARK_CSS not rendered on the Chat page"
    )
    assert any(dash.CHAT_CSS in s for s in styles), "CHAT_CSS missing on the Chat page"
    assert [m.name for m in at.chat_message] == ["user", "assistant"]

    # light pass: no DARK_CSS and no dark-palette hex survives anywhere in the rendered styles
    at.session_state["theme_mode"] = "light"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    styles = _styles(at)
    assert any(dash.LIGHT_CSS in s for s in styles), (
        "LIGHT_CSS not rendered on the Chat page"
    )
    assert any(dash.CHAT_CSS in s for s in styles), "CHAT_CSS missing in light mode"
    assert not any(dash.DARK_CSS in s for s in styles)
    for c in dash.DARK_CSS_COLORS:
        assert not any(c in s for s in styles), (
            f"stale dark colour {c} survived on the Chat page (light)"
        )
    # bubbles still render (from persisted history) with their slash avatars
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    assert at.chat_message[0].avatar == ":material/person:"

    # back to dark: DARK_CSS restored, LIGHT_CSS gone
    at.session_state["theme_mode"] = "dark"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    styles = _styles(at)
    assert any(dash.DARK_CSS in s for s in styles), (
        "DARK_CSS not restored after dark->light->dark"
    )
    assert not any(dash.LIGHT_CSS in s for s in styles)
