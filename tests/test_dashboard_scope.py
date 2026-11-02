"""AppTest coverage for the Review tab's out-of-scope findings caption (dashboard.py)."""

import json
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

    st.cache_resource.clear()  # the dashboard caches config/store per process
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    monkeypatch.setenv("SWARMFORGE_CONFIG", str(p))
    monkeypatch.setenv("SWARMFORGE_DASH_AUTOREFRESH", "0")
    monkeypatch.setenv("SWARMFORGE_DASH_UPDATE_CHECK", "0")
    monkeypatch.delenv("SWARMFORGE_DB", raising=False)
    return load_config(str(p))


def _seed(store, run_id):
    run = {
        "state": "COMPLETED",
        "issue_title": "Theme switch",
        "repo": "/repo",
        "budget_usd": 5.0,
        "config_json": json.dumps(
            {"limits": {"max_repair_cycles": 2, "max_runtime_s": 3600}}
        ),
    }
    store.create_run(run_id, **run)


def _open(at, run_id):
    """Open the run's detail panel from the Work list."""
    at.button(key=f"op_r_{run_id}").click().run()
    return at


def test_review_tab_shows_out_of_scope_caption(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    run_id = "20990101-000000-aaaaaa"
    _seed(store, run_id)
    store.event(run_id, "review", "review cycle 1", level="info")
    store.record_finding(
        run_id,
        cycle=1,
        source="review",
        severity="major",
        file="src/swarmforge/ui.py",
        line=10,
        title="bad contrast",
        description="text unreadable",
        recommendation="fix",
    )
    for _ in range(2):
        store.event(
            run_id,
            "scope",
            "skipped out-of-scope finding: [major] GIT env (file src/swarmforge/repos.py)",
            level="warn",
        )
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _open(at, run_id)
    assert not at.exception, [e.value for e in at.exception]
    assert any(c.value == "skipped out-of-scope findings: 2" for c in at.caption)


def test_review_tab_no_out_of_scope_caption_without_scope_events(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    run_id = "20990101-010000-bbbbbb"
    _seed(store, run_id)
    store.event(run_id, "review", "review cycle 1", level="info")
    store.record_finding(
        run_id,
        cycle=1,
        source="review",
        severity="minor",
        file="src/swarmforge/ui.py",
        line=3,
        title="typo",
        description="typo in label",
        recommendation="fix",
    )
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _open(at, run_id)
    assert not at.exception, [e.value for e in at.exception]
    assert not [c for c in at.caption if "out-of-scope" in c.value]
