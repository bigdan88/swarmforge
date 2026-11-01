"""Dashboard dark mode: the theme helpers, the injected CSS and the persistent sidebar toggle."""
import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
import warnings  # noqa: E402

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.config import load_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402

from .conftest import base_raw  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"


def _write_cfg(tmp_path, monkeypatch):
    """Config + env isolation for one test (same pattern as tests/test_dashboard.py)."""
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


def _dashboard():
    """Import the dashboard module (it runs once, in bare mode) to reach its pure helpers."""
    from swarmforge import dashboard
    return dashboard


def _styles(at) -> list[str]:
    return [m.value for m in at.markdown if "<style>" in (m.value or "")]


# ------------------------------------------------------------------ pure helpers
def test_theme_css_covers_each_mode(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dashboard()
    assert dash.theme_css("auto") == ""  # follow the Streamlit base theme: no override
    dark = dash.theme_css("dark")
    light = dash.theme_css("light")
    for css in (dark, light):
        assert css.startswith("<style>") and css.rstrip().endswith("</style>")
        # symmetric selector coverage: every selector dark touches, light resets
        for sel in ("stSidebarNav", "stMetricValue", "stCaptionContainer", "stExpander", "stCode"):
            assert sel in css
        assert ":hover" in css and "stSidebarNav" in css.split(":hover")[0] + ":hover"
    assert "#0e1117" in dark and "#fafafa" in dark and "#ff4b4b" in dark
    assert "#ffffff" in light and "color: #31333f" in light
    assert "[data-testid=\"stSidebar\"]" in light and "#31333f" in light
    for c in dash.DARK_CSS_COLORS:  # no dark hex leaks into the light override
        assert c not in light


def test_effective_theme_matrix(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dashboard()
    assert dash.effective_theme("dark", "light") == "dark"
    assert dash.effective_theme("dark", "dark") == "dark"
    assert dash.effective_theme("auto", "dark") == "dark"
    assert dash.effective_theme("auto", "light") == "light"
    assert dash.effective_theme("light", "dark") == "light"
    assert dash.effective_theme("light", "light") == "light"


def test_apply_chart_theme_switches_altair(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dashboard()
    seen: list[str] = []
    monkeypatch.setattr(alt.theme, "enable", seen.append)   # Altair >= 5.5 API (no deprecation warning)

    def forbidden(_name):  # the deprecated API must never be touched
        raise AssertionError("alt.themes.enable must not be called")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # touching alt.themes itself emits the deprecation shim
        monkeypatch.setattr(alt.themes, "enable", forbidden)
    dash.apply_chart_theme("dark")
    dash.apply_chart_theme("light")
    assert seen == ["dark", "default"]
    # an unknown/broken theme name must never crash the app
    def boom(_name):
        raise ValueError("no such theme")
    monkeypatch.setattr(alt.theme, "enable", boom)
    dash.apply_chart_theme("dark")
    dash.apply_chart_theme("light")


def test_theme_chart_dark_configures_light_unchanged(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dashboard()
    df = pd.DataFrame({"x": ["a", "b"], "y": [1, 2]})
    base = alt.Chart(df).mark_bar().encode(x="x", y="y")
    dark = dash.theme_chart(base, "dark").to_dict()
    cfg = dark["config"]
    assert cfg["axis"]["labelColor"] == "#fafafa"
    assert cfg["legend"]["labelColor"] == "#fafafa"
    assert cfg["view"]["strokeWidth"] == 0
    light = dash.theme_chart(base, "light")
    assert light is base  # light mode passes the chart through untouched
    lcfg = light.to_dict().get("config", {})
    flat = json.dumps(lcfg)
    for c in ("#fafafa", "#262730"):
        assert c not in flat  # no dark config values layered on in light mode


# ------------------------------------------------------------------- AppTest runs
def test_launch_theme_dark_renders_css_and_toggle_persists(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARMFORGE_DASH_THEME", "dark")  # what `swarmforge dashboard --theme dark` exports
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("#0e1117" in s for s in _styles(at))  # the dark override was injected
    seg = at.segmented_control(key="theme_mode")
    assert seg.value == "dark"
    # switch to light: reruns cleanly, persists, and the injected CSS flips
    seg.set_value("light").run()
    assert not at.exception, [e.value for e in at.exception]
    assert Store(cfg.db_path).kv_get("dash_theme") == "light"
    assert at.segmented_control(key="theme_mode").value == "light"
    assert not any("#0e1117" in s for s in _styles(at))
    assert any("#ffffff" in s and "#31333f" in s for s in _styles(at))


def test_persisted_dark_theme_survives_a_fresh_session(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)  # no SWARMFORGE_DASH_THEME: the kv store alone decides
    Store(cfg.db_path).kv_set("dash_theme", "dark")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("#0e1117" in s for s in _styles(at))
    assert at.segmented_control(key="theme_mode").value == "dark"


def test_default_theme_is_auto_with_no_override(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.segmented_control(key="theme_mode").value == "auto"
    assert not any("#0e1117" in s for s in _styles(at))


def _toggle_cycle(at, dash):
    """Run dark → light → dark; return (state, styles) for each of the three passes."""
    states = []
    for mode in ("dark", "light", "dark"):
        at.session_state["theme_mode"] = mode
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        states.append((dict(at.session_state), _styles(at)))
    return states


def test_dark_light_dark_toggle_leaves_no_dark_css(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dashboard()
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    (dark_ss, dark1), (light_ss, light), (dark_ss2, dark2) = _toggle_cycle(at, dash)

    # dark pass: the override is active in session state and rendered
    assert dark_ss["theme_css"] == dash.DARK_CSS
    assert any(dash.DARK_CSS in s for s in dark1)

    # light pass: no dark colour survives in session state or any rendered style block
    assert light_ss["theme_css"] == dash.LIGHT_CSS
    for c in dash.DARK_CSS_COLORS:
        assert c not in light_ss["theme_css"]
        for s in light:
            for rule in s.split(";"):
                if "background-color" in rule or re.search(r"(^|[{\s])color:", rule):
                    assert c not in rule, f"dark colour {c} leaked into light CSS rule: {rule}"
    # the light override itself is rendered, with the sidebar/hover/metric/caption resets
    assert any(dash.LIGHT_CSS in s for s in light)
    for sel in ("stSidebar", "stSidebarNav", "stMetricValue", "stCaptionContainer"):
        assert sel in light_ss["theme_css"]
    assert "color: #31333f" in light_ss["theme_css"]

    # back to dark: the dark override returns
    assert dark_ss2["theme_css"] == dash.DARK_CSS
    assert any(dash.DARK_CSS in s for s in dark2)


def test_no_altair_deprecation_warning_on_toggle(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    dash = _dashboard()

    def forbidden(_name):  # the deprecated API must never be called during the cycle
        raise AssertionError("alt.themes.enable must not be called")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # touching alt.themes itself emits the deprecation shim
        monkeypatch.setattr(alt.themes, "enable", forbidden)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _toggle_cycle(at, dash)  # asserts not at.exception at every step

    # direct calls emit no Deprecation-class warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dash.apply_chart_theme("dark")
        dash.apply_chart_theme("light")
    assert not [w for w in caught if "Deprecation" in type(w.message).__name__]