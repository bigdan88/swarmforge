"""Light/dark theme = Streamlit's own switch (⋮ menu), driven by the sidebar; no custom colour palette.

Stability note: the real-browser test reuses the shared `visual.capture_tabs` helper (the same code
path the production visual-regression gate uses) plus a graceful skip when no headless Chromium is
available, so no xdist_group-style serialisation workaround is needed.

History: SwarmForge used to paint a dark/light palette over Streamlit with CSS. Streamlit fixes its
theme at start-up, so the overrides only covered some elements and switching left half the page in
the wrong colours. Now the native switch restyles everything and the dashboard adds no palette."""

import re
import socket
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
    import streamlit as st

    st.cache_resource.clear()
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(base_raw(tmp_path / "work")))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    monkeypatch.delenv("SWARMFORGE_DASH_THEME", raising=False)
    return load_config(str(p))


def test_no_custom_palette_anywhere():
    src = APP.read_text(encoding="utf-8")
    for gone in (
        "DARK_CSS",
        "LIGHT_CSS",
        "theme_css(",
        "apply_chart_theme",
        "theme_chart(",
        "--theme.base",
    ):
        assert gone not in src, gone
    # no rule paints the app's own background or text colour
    assert not re.search(r"\.stApp[^{]*\{[^}]*background", src)
    from swarmforge import dashboard

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", dashboard.CHAT_CSS), (
        "chat bubbles must use see-through tints"
    )


def test_theme_switch_script_clicks_the_native_menu_item():
    from swarmforge.dashboard import theme_switch_js

    js = theme_switch_js("Dark")
    assert (
        "stMainMenuItem-theme-Dark" in js
        and "pointerdown" in js
        and "stActiveTheme" not in js
    )
    assert "stActiveTheme" in theme_switch_js(
        "Light", only_if_unset=True
    )  # --theme: first visit only
    with pytest.raises(ValueError):
        theme_switch_js("Purple")


def test_sidebar_theme_control_renders_and_switches_without_errors(
    tmp_path, monkeypatch
):
    _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    ctl = at.segmented_control(key="theme_pick")
    assert ctl.options == ["Light", "Dark", "Auto"]
    ctl.set_value("Dark").run()
    assert not at.exception, [e.value for e in at.exception]


def test_chat_page_bubbles_render_material_slash_avatars(tmp_path, monkeypatch):
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
    from swarmforge import chatbot

    monkeypatch.setattr(
        chatbot,
        "ask",
        lambda c, s, question, history=None, **kw: {
            "answer": "pong!",
            "role": "planner",
            "model": "qwen-fake",
            "question": question,
        },
    )
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.button(key="nav_chat").click().run()
    at.chat_input[0].set_value("ping").run()
    assert not at.exception, [e.value for e in at.exception]
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    assert at.chat_message[0].avatar == ":material/person:"
    assert at.chat_message[1].avatar == ":material/auto_awesome:"


def test_material_icons_use_slash_format():
    from swarmforge import dashboard

    assert ":material:" not in APP.read_text(encoding="utf-8")
    for role, avatar in dashboard.CHAT_AVATARS.items():
        assert re.fullmatch(r":material/[a-z_]+:", avatar), role


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
def test_real_browser_sidebar_switches_the_whole_page(tmp_path):
    """The real thing, through the same capture path the visual-regression gate uses:
    `visual.capture_tabs` screenshots the dashboard in both themes from a real headless Chromium,
    and the light and dark shots of the same tab must differ by more than the 2 % gate threshold
    (a broken theme switch would collapse them to near-identical pixels)."""
    pytest.importorskip("playwright.sync_api")
    from swarmforge import visual

    port = _free_port()
    try:
        shots = visual.capture_tabs(
            Path(__file__).resolve().parents[1],
            tmp_path,
            tabs=["work"],
            port=port,
            timeout_s=120,
        )
    except (visual.RegressError, OSError, RuntimeError, TimeoutError) as e:
        pytest.skip(f"headless browser unavailable or timed out: {e}")
    light, dark = shots["work-light"], shots["work-dark"]
    assert light.exists() and dark.exists()
    score = visual.pixel_diff_score(light, dark)
    assert score > 0.02, (
        f"light and dark screenshots of the work tab are near-identical "
        f"({score:.1%} differing pixels) - the theme switch is broken"
    )
