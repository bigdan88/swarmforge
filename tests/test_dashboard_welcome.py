"""First-run welcome panel on the Work page (AppTest)."""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge import welcome  # noqa: E402
from swarmforge.config import load_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402

from .conftest import base_raw  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"


def _write_cfg(tmp_path, monkeypatch):
    import streamlit as st
    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return load_config(str(p))


def _app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=60)


def _text(at) -> str:
    """Everything the panel renders as headings/markdown/captions, in order."""
    return " ".join([*(h.value for h in at.header), *(s.value for s in at.subheader),
                     *(m.value for m in at.markdown), *(c.value for c in at.caption)])


def test_welcome_replaces_empty_table(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda cfg: [])  # never touch docker/network here
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = _text(at)
    assert "Welcome to SwarmForge" in text
    assert "How SwarmForge works" in text
    assert any("Try the sample" in b.label for b in at.button)
    assert any("Planner" in g.proto.spec for g in at.get("graphviz_chart"))
    assert not at.dataframe  # the empty runs table is gone
    assert any("Nothing here yet" in i.value for i in at.info)


def test_checklist_missing_key_and_image(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    canned = [
        welcome.CheckItem("Docker running", True),
        welcome.CheckItem("Sandbox image built", False, fix="swarmforge sandbox build"),
        welcome.CheckItem("DeepInfra API key", False, fix="DEEPINFRA_API_KEY=... in .env"),
        welcome.CheckItem("GitHub login", False, optional=True, fix="gh auth login"),
    ]
    monkeypatch.setattr(welcome, "health_checks", lambda cfg: canned)
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    fixes = " ".join(c.value for c in at.code)
    assert "swarmforge sandbox build" in fixes
    assert "DEEPINFRA_API_KEY" in fixes
    text = _text(at)
    assert "✅ Docker running" in text
    assert "❌ Sandbox image built" in text
    assert "❌ DeepInfra API key" in text
    assert "⬜ GitHub login (optional)" in text


def test_sample_button_creates_repo_and_starts_run(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda c: [])
    monkeypatch.setenv("HOME", str(tmp_path))  # the sample repo lands in the temp home
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    calls: list[dict] = []

    def fake_start_run(c, **kw):
        calls.append(kw)
        return "20990101-000000-sample", tmp_path / "log.txt"

    monkeypatch.setattr("swarmforge.launcher.start_run", fake_start_run)
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if "Try the sample" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(calls) == 1
    assert calls[0]["budget"] == 3.0 and calls[0]["max_cycles"] == 1
    assert calls[0]["repo"] == str(tmp_path / "swarmforge-sample")
    assert (tmp_path / "swarmforge-sample" / ".git").is_dir()
    assert Store(cfg.db_path).recent_repos()[0] == str(tmp_path / "swarmforge-sample")
    assert at.session_state["sel_item"] == "r:20990101-000000-sample"
    assert at.session_state["page"] == "Work"


def test_getting_started_sidebar_link(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda c: [])
    Store(cfg.db_path).create_run("20990101-000000-aaaaaa", state="COMPLETED", issue_title="Existing run",
                                  repo="x/y", budget_usd=1.0, config_json="{}")
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any("Welcome to SwarmForge" in h.value for h in at.header)  # hidden while runs exist
    at.button(key="nav_getting_started").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "Work"
    assert any("Welcome to SwarmForge" in h.value for h in at.header)
    assert "How SwarmForge works" in _text(at)
    assert any("Try the sample" in b.label for b in at.button)
    assert not any("Nothing here yet" in i.value for i in at.info)  # runs exist: no empty-state hint