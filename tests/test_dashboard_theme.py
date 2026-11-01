"""Dashboard dark mode: the theme helpers, the injected CSS and the persistent sidebar toggle."""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
import altair as alt  # noqa: E402
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
    assert dark.startswith("<style>") and dark.rstrip().endswith("</style>")
    assert "#0e1117" in dark and "#fafafa" in dark and "#ff4b4b" in dark
    light = dash.theme_css("light")
    assert light.startswith("<style>") and "#ffffff" in light and "#31333f" in light
    assert "#0e1117" not in light


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
    original = alt.themes.active  # restore whatever was active before the test
    seen: list[str] = []
    monkeypatch.setattr(alt.themes, "enable", seen.append)
    try:
        dash.apply_chart_theme("dark")
        dash.apply_chart_theme("light")
        dash.apply_chart_theme("auto")
        assert seen == ["dark", "vox", "vox"]
    finally:
        monkeypatch.undo()
        alt.themes.enable(original)
    # an unknown/broken theme name must never crash the app
    def boom(_name):
        raise ValueError("no such theme")
    monkeypatch.setattr(alt.themes, "enable", boom)
    dash.apply_chart_theme("dark")
    dash.apply_chart_theme("light")


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