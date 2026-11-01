"""Acceptance tests for the dark→light toggle fix (issue: "Fix dark-mode → light-mode toggle").

Independent of tests/test_dashboard_theme.py (the developer-authored AC4 file) and of the earlier
tests/test_acceptance_darkmode.py: one focused test per acceptance criterion of this issue.

AC1  dark → light leaves no dark-override background-color/color/hover CSS in session_state or the
     rendered HTML; sidebar background/text/hover are pinned to Streamlit's default light values.
AC2  LIGHT_CSS neutralises every selector (and every property) DARK_CSS styles: expanders, metrics,
     code blocks, buttons, captions.
AC3  Altair charts follow the current mode via the modern alt.theme API + per-chart .configure_*
     calls; the deprecated alt.themes.enable is not referenced anywhere under src/.
AC4  tests/test_dashboard_theme.py contains a Streamlit AppTest that toggles dark → light → dark and
     asserts no dark-override CSS survives on the light pass (presence check; the functional cycle is
     re-verified here independently in test_ac1/test_ac5).
AC5  No AltairDeprecationWarning is emitted during the toggle cycle: alt.themes.enable is patched to
     raise and warnings are recorded across the full AppTest dark → light → dark run.
"""
import re
import warnings
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
import pandas as pd  # noqa: E402
import altair as alt  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.config import load_config  # noqa: E402

from .conftest import base_raw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "swarmforge" / "dashboard.py"
SRC = ROOT / "src"

# Streamlit's documented default light-theme colours (config docs: theme.backgroundColor etc.).
LIGHT_BG = "#ffffff"
LIGHT_SIDEBAR_BG = "#f0f2f6"
LIGHT_TEXT = "#31333f"


def _write_cfg(tmp_path, monkeypatch):
    """Config + env isolation for one AppTest session (same pattern as tests/test_dashboard.py)."""
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


def _cycle(at, modes):
    """Set the theme through the sidebar's session key and rerun once per mode."""
    for mode in modes:
        at.session_state["theme_mode"] = mode
        at.run()
        assert not at.exception, [e.value for e in at.exception]


# ---------------------------------------------------------------------- AC1
def test_ac1_dark_to_light_leaves_no_dark_css_in_state_or_html(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    _cycle(at, ["dark"])
    assert at.session_state["theme_css"] == dash.DARK_CSS
    assert any(dash.DARK_CSS in s for s in _styles(at))

    _cycle(at, ["light"])
    state = at.session_state["theme_css"]
    styles = _styles(at)
    # session_state holds the light override only — no dark colour survives anywhere in it
    assert state == dash.LIGHT_CSS
    for c in dash.DARK_CSS_COLORS:
        assert c not in state, f"dark colour {c} survived in st.session_state['theme_css']"
    # rendered HTML: the dark block is gone (no stacking, no residue) and the light block is there
    assert not any(c in s for s in styles for c in dash.DARK_CSS_COLORS), \
        "a dark-override colour survived in the rendered <style> blocks"
    assert any(dash.LIGHT_CSS in s for s in styles)
    # no rendered style block may set a background-color/color rule to a dark-palette value
    for s in styles:
        for rule in _rules(s).values():
            for decl in rule.split(";"):
                if ":" not in decl:
                    continue
                prop, _, val = decl.partition(":")
                if prop.strip() in ("background", "background-color", "color") and "inherit" not in val:
                    assert val.strip().lower() not in dash.DARK_CSS_COLORS, f"dark rule leaked: {decl}"


def test_ac1_sidebar_and_hover_pinned_to_streamlit_default_light_values(tmp_path, monkeypatch):
    """After toggle-back the sidebar background/text/hover CSS matches Streamlit's default light theme."""
    dash = _dash(tmp_path, monkeypatch)
    light = _rules(dash.LIGHT_CSS)
    dark = _rules(dash.DARK_CSS)
    app = light['.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"]']
    assert f"background-color: {LIGHT_BG}" in app and f"color: {LIGHT_TEXT}" in app
    side = light['[data-testid="stSidebar"]']
    assert f"background-color: {LIGHT_SIDEBAR_BG}" in side and f"color: {LIGHT_TEXT}" in side
    hover_sel = ('[data-testid="stSidebar"] [data-testid="stSidebarNav"] a:hover, '
                 '[data-testid="stSidebar"] .stButton button:hover')
    hover = light[hover_sel]
    assert f"background: {LIGHT_BG}" in hover and f"color: {LIGHT_TEXT}" in hover
    # sanity: the dark block really sets those same selectors to dark-palette values
    assert dark['[data-testid="stSidebar"]'] != side
    assert any(c in dark[hover_sel] for c in dash.DARK_CSS_COLORS)


def test_ac1_second_light_pass_after_retoggle_is_still_clean(tmp_path, monkeypatch):
    """dark → light → dark → light: the light override returns without any dark residue."""
    dash = _dash(tmp_path, monkeypatch)
    _write_cfg(tmp_path, monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _cycle(at, ["dark", "light", "dark", "light"])
    assert at.session_state["theme_css"] == dash.LIGHT_CSS
    for c in dash.DARK_CSS_COLORS:
        assert not any(c in s for s in _styles(at)), f"dark colour {c} leaked on the second light pass"


# ---------------------------------------------------------------------- AC2
def test_ac2_light_css_neutralises_every_dark_selector_and_property(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    dark, light = _rules(dash.DARK_CSS), _rules(dash.LIGHT_CSS)
    assert dark, "DARK_CSS parsed to no rules"
    missing = [sel for sel in dark if sel not in light]
    assert not missing, f"LIGHT_CSS does not reset selectors: {missing}"
    for sel, body in dark.items():
        dark_props = _props(body)
        light_props = _props(light[sel])
        leak = dark_props - light_props
        assert not leak, f"selector {sel!r}: properties styled dark but not reset light: {leak}"
        for decl in light[sel].split(";"):
            val = decl.split(":", 1)[1].strip().lower() if ":" in decl else ""
            assert val not in dash.DARK_CSS_COLORS, f"light reset of {sel!r} uses a dark colour: {decl}"


def test_ac2_named_components_are_reset_in_light_css(tmp_path, monkeypatch):
    """Expanders, metric blocks, code blocks, buttons and captions are covered on both sides."""
    dash = _dash(tmp_path, monkeypatch)
    for component in ("stExpander", "stMetricValue", "stMetricLabel", "stCode",
                      "stButton", "stCaptionContainer"):
        needle = f'data-testid="{component}"'
        assert needle in dash.DARK_CSS, component
        assert needle in dash.LIGHT_CSS, component


# ---------------------------------------------------------------------- AC3
def test_ac3_deprecated_alt_themes_enable_not_referenced_in_source():
    hits = []
    for py in sorted(SRC.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for m in re.finditer(r"\bthemes\.enable\b|alt\.themes\b", text):
            hits.append(f"{py.relative_to(ROOT)}:{text[:m.start()].count(chr(10)) + 1}: {m.group(0)}")
    assert not hits, f"deprecated alt.themes API still referenced: {hits}"


def test_ac3_apply_chart_theme_uses_modern_alt_theme_api(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(alt.theme, "enable", seen.append)

    def forbidden(_name):
        raise AssertionError("alt.themes.enable must not be called")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # merely touching alt.themes emits the deprecation shim
        monkeypatch.setattr(alt.themes, "enable", forbidden)
    dash.apply_chart_theme("dark")
    dash.apply_chart_theme("light")
    assert seen == ["dark", "default"]  # charts follow the current mode via alt.theme.enable


def test_ac3_charts_are_styled_per_chart_according_to_mode(tmp_path, monkeypatch):
    dash = _dash(tmp_path, monkeypatch)
    df = pd.DataFrame({"x": ["a", "b"], "y": [1, 2]})
    base = alt.Chart(df).mark_bar().encode(x="x", y="y")
    dark_cfg = dash.theme_chart(base, "dark").to_dict()["config"]
    assert dark_cfg["axis"]["labelColor"] == "#fafafa"
    assert dark_cfg["legend"]["labelColor"] == "#fafafa"
    assert dark_cfg["title"]["color"] == "#fafafa"
    assert dark_cfg["view"]["strokeWidth"] == 0
    light = dash.theme_chart(base, "light")
    assert light is base  # light mode: default theme, nothing dark layered on
    assert "#fafafa" not in str(light.to_dict().get("config", {}))


def test_ac3_every_altair_chart_call_in_the_dashboard_wraps_theme_chart():
    src = APP.read_text(encoding="utf-8")
    total = len(re.findall(r"st\.altair_chart\(", src))
    wrapped = len(re.findall(r"st\.altair_chart\(\s*theme_chart\(", src))
    assert total >= 1 and wrapped == total, f"{wrapped}/{total} altair_chart calls follow the palette"
    assert "eff_theme" in src  # charts are styled with the *current* effective mode


# ---------------------------------------------------------------------- AC4
def test_ac4_dashboard_theme_file_has_dark_light_dark_apptest_cycle():
    """tests/test_dashboard_theme.py must contain the AppTest dark → light → dark toggle test."""
    text = (ROOT / "tests" / "test_dashboard_theme.py").read_text(encoding="utf-8")
    assert "AppTest" in text
    assert re.search(r'for mode in \("dark", "light", "dark"\)', text), "no dark→light→dark cycle"
    assert "theme_css" in text and "DARK_CSS_COLORS" in text, "light pass does not assert absence"
    # the file itself runs green as part of this suite; this check pins its required shape


# ---------------------------------------------------------------------- AC5
def test_ac5_no_altair_deprecation_warning_during_toggle_cycle(tmp_path, monkeypatch):
    _dash(tmp_path, monkeypatch)
    _write_cfg(tmp_path, monkeypatch)
    try:
        from altair.utils.deprecation import AltairDeprecationWarning
    except ImportError:  # older altair layout
        class AltairDeprecationWarning(UserWarning):  # type: ignore[no-redef]
            pass

    def forbidden(_name):
        raise AssertionError("alt.themes.enable must not be called during the toggle cycle")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # setup itself touches the deprecated shim
        monkeypatch.setattr(alt.themes, "enable", forbidden)

    at = AppTest.from_file(str(APP), default_timeout=60)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        _cycle(at, ["dark", "light", "dark"])
    altair_deprecations = [w for w in caught
                          if isinstance(w.message, AltairDeprecationWarning)
                          or ("Deprecation" in type(w.message).__name__
                              and "altair" in str(w.message).lower())]
    assert not altair_deprecations, [str(w.message) for w in altair_deprecations]


def test_ac5_apply_chart_theme_raises_no_deprecation_even_as_error(tmp_path, monkeypatch):
    """Escalate AltairDeprecationWarning to an exception: the theme switch must survive it."""
    dash = _dash(tmp_path, monkeypatch)
    try:
        from altair.utils.deprecation import AltairDeprecationWarning
    except ImportError:
        pytest.skip("altair too old to expose AltairDeprecationWarning")
    with warnings.catch_warnings():
        warnings.simplefilter("error", AltairDeprecationWarning)
        dash.apply_chart_theme("dark")
        dash.apply_chart_theme("light")
        dash.apply_chart_theme("auto")
