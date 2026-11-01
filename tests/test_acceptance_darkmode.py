"""Acceptance tests for the dashboard dark mode (issue "Darkmode", AC1-AC5).

Independent of tests/test_cli_theme.py / tests/test_dashboard_theme.py: these map one test per
acceptance criterion and check the exact surfaces the criteria name (CSS coverage, the sidebar
'Theme' segmented control with auto/light/dark options persisted under the 'dash_theme' kv key,
the `--theme` CLI flag forwarding `--theme.base` + SWARMFORGE_DASH_THEME, the Altair chart theme
following the effective palette, and unchanged auto/light defaults).
"""
import argparse
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
import altair as alt
from streamlit.testing.v1 import AppTest

from swarmforge.cli import build_parser, cmd_dashboard
from swarmforge.config import load_config
from swarmforge.db import Store

from .conftest import base_raw

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"
DARK_BG = "#0e1117"  # a dark-palette-only colour used as the sentinel below


def _write_cfg(tmp_path, monkeypatch, launch_theme=None):
    """Config + env isolation for one AppTest session (same pattern as tests/test_dashboard.py)."""
    import streamlit as st
    st.cache_resource.clear()  # the dashboard caches config/store per process
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(base_raw(tmp_path / "work")))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    if launch_theme is None:
        monkeypatch.delenv("SWARMFORGE_DASH_THEME", raising=False)
    else:
        monkeypatch.setenv("SWARMFORGE_DASH_THEME", launch_theme)
    return load_config(str(p))


def _dash(tmp_path, monkeypatch):
    """Import the dashboard module (it runs once, in bare mode) to reach its helpers/constants."""
    _write_cfg(tmp_path, monkeypatch)
    from swarmforge import dashboard
    return dashboard


def _markdown_values(at):
    return [m.value for m in at.markdown]


def _theme_control(at, in_sidebar=True):
    segs = at.sidebar.segmented_control if in_sidebar else at.segmented_control
    matches = [s for s in segs if s.key == "theme_mode"]
    assert matches, f"no segmented control with key 'theme_mode' in {'sidebar' if in_sidebar else 'app'}"
    return matches[0]


# --------------------------------------------------------------------- AC1
def test_ac1_dark_css_covers_every_named_surface(tmp_path, monkeypatch):
    """theme_css('dark') returns injected CSS covering background, text, sidebar, header,
    inputs, containers, buttons and code blocks with a dark palette."""
    dash = _dash(tmp_path, monkeypatch)
    css = dash.theme_css("dark")
    assert css.startswith("<style>") and css.rstrip().endswith("</style>")
    # app background + header + sidebar surfaces
    for selector in (".stApp", 'data-testid="stAppViewContainer"', 'data-testid="stHeader"',
                     'data-testid="stSidebar"'):
        assert selector in css, selector
    # inputs, containers, buttons, code blocks
    for selector in ('data-testid="stTextInput"', 'data-testid="stNumberInput"',
                     'data-testid="stTextArea"', 'data-testid="stExpander"',
                     'data-testid="stVerticalBlockBorderWrapper"', 'data-testid="stButton"',
                     'data-testid="stCode"'):
        assert selector in css, selector
    # dark palette values for background and text
    assert DARK_BG in css and "#fafafa" in css


def test_ac1_selecting_dark_injects_the_css_into_the_rendered_app(tmp_path, monkeypatch):
    """Selecting 'dark' renders the app with the dark CSS injected via dashboard.py."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    Store(cfg.db_path).kv_set("dash_theme", "dark")  # equivalent to picking 'dark' in the control
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    from swarmforge import dashboard
    assert dashboard.theme_css("dark") in _markdown_values(at)


# --------------------------------------------------------------------- AC2
def test_ac2_sidebar_theme_control_offers_auto_light_dark(tmp_path, monkeypatch):
    """The theme choice is a persistent UI control: a segmented control 'Theme' with options
    auto/light/dark living in the sidebar."""
    _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    seg = _theme_control(at)  # raises if it is not inside the sidebar
    assert seg.label == "Theme"
    assert list(seg.options) == ["auto", "light", "dark"]


def test_ac2_choice_is_stored_under_dash_theme_kv_and_restored_next_run(tmp_path, monkeypatch):
    """Switching the control persists to the DB kv store under 'dash_theme' and a brand-new
    session (next run) restores that choice."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _theme_control(at).set_value("dark").run()
    assert not at.exception, [e.value for e in at.exception]
    assert store.kv_get("dash_theme") == "dark"
    assert _theme_control(at).value == "dark"
    # a fresh AppTest session (no launch env): the kv store alone decides
    at2 = AppTest.from_file(str(APP), default_timeout=60)
    at2.run()
    assert not at2.exception, [e.value for e in at2.exception]
    assert _theme_control(at2).value == "dark"
    from swarmforge import dashboard
    assert dashboard.theme_css("dark") in _markdown_values(at2)


# --------------------------------------------------------------------- AC3
def _capture_dashboard_call(monkeypatch):
    calls = []

    def fake_call(cmd, env=None, **kwargs):
        calls.append((list(cmd), dict(env or {})))
        return 0

    monkeypatch.setattr("swarmforge.cli.subprocess.call", fake_call)
    return calls


def test_ac3_parser_accepts_theme_choices_with_auto_default():
    parser = build_parser()
    assert parser.parse_args(["dashboard"]).theme == "auto"
    for value in ("auto", "light", "dark"):
        assert parser.parse_args(["dashboard", "--theme", value]).theme == value
    with pytest.raises(SystemExit):
        parser.parse_args(["dashboard", "--theme", "sepia"])


def test_ac3_cmd_dashboard_forwards_theme_base_and_env(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    calls = _capture_dashboard_call(monkeypatch)
    for theme in ("auto", "light", "dark"):
        calls.clear()
        ns = argparse.Namespace(port=8501, lan=False, no_browser=True, theme=theme)
        assert cmd_dashboard(ns, cfg) == 0
        cmd, env = calls[0]
        if theme == "auto":   # Streamlit reads any other --theme.base value as a theme FILE path
            assert "--theme.base" not in cmd
        else:
            assert "--theme.base" in cmd and cmd[cmd.index("--theme.base") + 1] == theme
        assert env["SWARMFORGE_DASH_THEME"] == theme
        # streamlit run of the dashboard app is preserved
        assert cmd[:4] == ["python", "-m", "streamlit", "run"] or cmd[1:4] == ["-m", "streamlit", "run"]
        assert cmd[cmd.index("run") + 1].endswith("dashboard.py")


# --------------------------------------------------------------------- AC4
def test_ac4_altair_theme_follows_effective_theme(tmp_path, monkeypatch):
    """Charts use the 'dark' Altair theme when the effective theme is dark, the default 'vox'
    otherwise (including auto+auto and light overrides of a dark launch)."""
    dash = _dash(tmp_path, monkeypatch)
    enabled = []
    monkeypatch.setattr(alt.themes, "enable", enabled.append)
    for sel, launch, expected in [("dark", "auto", "dark"), ("auto", "dark", "dark"),
                                  ("auto", "auto", "vox"), ("light", "auto", "vox"),
                                  ("light", "dark", "vox")]:
        dash.apply_chart_theme(dash.effective_theme(sel, launch))
    assert enabled == ["dark", "dark", "vox", "vox", "vox"]


def test_ac4_graphviz_and_badge_helpers_do_not_raise_in_either_mode(tmp_path, monkeypatch):
    """No code change was needed for graphviz/markdown badges: generating the delegation graph
    and rendering the app must not raise with the dark palette active."""
    dash = _dash(tmp_path, monkeypatch)
    run = {"issue_title": "Add CSV export", "config_json": "{}", "state": "COMPLETED"}
    tasks = [{"cycle": 0, "kind": "implement", "task_key": "t1", "title": "Export", "role": "developer_backend",
              "model": "fake-m", "state": "SUCCEEDED", "depends_on": []}]
    events = [{"kind": "review", "message": "verdict=approve ship it", "data": "{}"}]
    dot, _ = dash.delegation_graph(run, tasks, events)
    assert "digraph" in dot and "t1" in dot and "Reviewer" in dot
    assert dash._nav_badges() == {} or isinstance(dash._nav_badges(), dict)  # badges never raise
    # and the full app renders dark without exceptions (markdown badges included)
    cfg = _write_cfg(tmp_path, monkeypatch, launch_theme="dark")
    Store(cfg.db_path)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]


# --------------------------------------------------------------------- AC5
def test_ac5_auto_theme_css_is_empty(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    assert dash.theme_css("auto") == ""


def test_ac5_default_run_is_auto_with_no_override_css_injected(tmp_path, monkeypatch):
    """Default behaviour unchanged: without --theme / kv value the control shows 'auto', nothing
    is persisted, and no theme override CSS (dark or light) is injected."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert _theme_control(at).value == "auto"
    from swarmforge import dashboard
    values = _markdown_values(at)
    assert dashboard.theme_css("dark") not in values
    assert dashboard.theme_css("light") not in values
    assert not any(DARK_BG in (v or "") for v in values)
    assert Store(cfg.db_path).kv_get("dash_theme") is None
