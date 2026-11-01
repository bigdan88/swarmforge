"""Independent acceptance tests for the first-run welcome panel (issue: "First-run welcome and
guided setup in the dashboard").

Covers the acceptance criteria with tests written from scratch against the public behaviour:

- AC1: empty database -> welcome panel (checklist, sample button, graphviz diagram) instead of
  the empty runs table (streamlit.testing.v1.AppTest).
- AC2: the checklist reflects a missing API key / missing sandbox image / Docker status / optional
  GitHub login, each unchecked item showing the exact fix command; done by monkeypatching the
  low-level check helpers on the importable ``swarmforge.welcome`` module.
- AC3: the "Try the sample" button creates ~/swarmforge-sample (temp HOME) and starts a run via
  ``launcher.start_run`` with max_repair_cycles=1 and budget $3, then opens that run.
- AC4: the panel stays available later via the sidebar "Getting started" link even when runs exist.

AC5 (existing suite passes) is verified by running the full suite, not by a test here.
"""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from swarmforge import welcome
from swarmforge.config import load_config
from swarmforge.db import Store

from .conftest import base_raw

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"
SAMPLE_BUTTON = "Try the sample (about $0.40, 15 minutes)"


def _write_cfg(tmp_path, monkeypatch, sandbox_mode="local"):
    import streamlit as st

    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work", sandbox_mode)
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return load_config(str(p))


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _rendered_text(at) -> str:
    return " ".join([*(h.value for h in at.header), *(s.value for s in at.subheader),
                     *(m.value for m in at.markdown), *(c.value for c in at.caption),
                     *(i.value for i in at.info)])


# --------------------------------------------------------------------- AC1
def test_ac1_empty_database_shows_welcome_instead_of_runs_table(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)  # brand-new (empty) database
    canned = [welcome.CheckItem("Docker running", True),
              welcome.CheckItem("Sandbox image built", False, fix="`swarmforge sandbox build`")]
    monkeypatch.setattr(welcome, "health_checks", lambda cfg: canned)
    at = _run_app()

    text = _rendered_text(at)
    assert "Welcome to SwarmForge" in text
    assert "Getting started" in text
    # the checklist renders (checked + unchecked item from the canned checks)
    assert "✅ Docker running" in text
    assert "❌ Sandbox image built" in text
    assert any("swarmforge sandbox build" in c.value for c in at.code)
    # the sample button with its price/time promise
    assert any(SAMPLE_BUTTON in b.label for b in at.button)
    # the "How SwarmForge works" section with a static graphviz diagram
    assert "How SwarmForge works" in text
    charts = at.get("graphviz_chart")
    assert charts, "no graphviz chart rendered"
    dot = " ".join(g.proto.spec for g in charts)
    for step in ("Planner", "Tester", "Reviewer", "PR"):
        assert step in dot
    # the empty runs table is replaced by the panel
    assert not at.dataframe
    assert any("Nothing here yet" in i.value for i in at.info)


# --------------------------------------------------------------------- AC2
def test_ac2_checklist_reflects_missing_key_and_image_via_check_helpers(tmp_path, monkeypatch):
    # docker-mode config; patch the low-level helpers so no real docker/network is touched
    _write_cfg(tmp_path, monkeypatch, sandbox_mode="docker")
    monkeypatch.delenv("FAKE_KEY", raising=False)  # simulate a missing DeepInfra-style API key
    monkeypatch.setattr(welcome, "docker_running", lambda cfg: True)
    monkeypatch.setattr(welcome, "image_built", lambda cfg: False)
    monkeypatch.setattr(welcome, "github_logged_in", lambda cfg: False)
    at = _run_app()

    text = _rendered_text(at)
    assert "✅ Docker running" in text
    assert "❌ Sandbox image built" in text
    assert "❌ API key (FAKE_KEY)" in text
    assert "⬜ GitHub login (optional)" in text
    # every unchecked item shows the exact command / instructions to fix it
    fixes = " ".join(c.value for c in at.code)
    assert "swarmforge sandbox build" in fixes      # image fix command
    assert "FAKE_KEY" in fixes and ".env" in fixes  # key fix instructions
    assert "gh auth login" in fixes                 # GitHub login fix
    # and the real welcome.health_checks() data carries the same fixes
    by_name = {i.name: i for i in welcome.health_checks(load_config(str(tmp_path / "swarmforge.yaml")))}
    assert not by_name["Sandbox image built"].ok
    assert "swarmforge sandbox build" in by_name["Sandbox image built"].fix
    assert not by_name["API key (FAKE_KEY)"].ok
    assert by_name["GitHub login"].optional and not by_name["GitHub login"].ok


# --------------------------------------------------------------------- AC3
def test_ac3_sample_button_creates_repo_and_starts_run(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda c: [])
    monkeypatch.setenv("HOME", str(tmp_path))      # sample repo must land in the temp home
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    calls: list[dict] = []

    def fake_start_run(c, **kw):
        calls.append(kw)
        return "20990101-120000-acpt0003", tmp_path / "log.txt"

    monkeypatch.setattr("swarmforge.launcher.start_run", fake_start_run)
    at = _run_app()

    next(b for b in at.button if SAMPLE_BUTTON in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]

    assert len(calls) == 1
    kw = calls[0]
    assert kw["budget"] == 3.0                       # sensible default budget $3
    assert kw["max_cycles"] == 1                     # max repair cycles 1
    assert kw["repo"] == str(tmp_path / "swarmforge-sample")
    assert kw["mode"] == "swarm"
    assert (kw["issue_title"] or "").strip() and (kw["issue_body"] or "").strip()
    # the sample repository was really prepared under the temp HOME
    assert (tmp_path / "swarmforge-sample" / ".git").is_dir()
    # the repository is remembered and the new run is opened on the Work page
    assert Store(cfg.db_path).recent_repos()[0] == str(tmp_path / "swarmforge-sample")
    assert at.session_state["sel_item"] == "r:20990101-120000-acpt0003"
    assert at.session_state["page"] == "Work"


# --------------------------------------------------------------------- AC4
def test_ac4_getting_started_sidebar_link_reopens_panel_with_runs(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(welcome, "health_checks", lambda c: [])
    Store(cfg.db_path).create_run("20990101-000000-aaaaaa", state="COMPLETED", issue_title="Existing run",
                                  repo="x/y", budget_usd=1.0, config_json="{}")
    at = _run_app()

    # with runs present the welcome panel is not shown automatically
    assert not any("Welcome to SwarmForge" in h.value for h in at.header)

    at.button(key="nav_getting_started").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "Work"
    assert any("Welcome to SwarmForge" in h.value for h in at.header)
    assert "How SwarmForge works" in _rendered_text(at)
    assert any(SAMPLE_BUTTON in b.label for b in at.button)
    # runs still exist: no empty-state hint, and the existing run is still listed
    assert not any("Nothing here yet" in i.value for i in at.info)
    assert ("Existing run" in _rendered_text(at)
            or any("Existing run" in b.label for b in at.button))
