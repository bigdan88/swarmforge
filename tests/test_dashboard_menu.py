"""Sidebar burger menu (grouped nav, ☰ icon rail) and attachment uploaders."""
import base64
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.config import load_config  # noqa: E402
from swarmforge.db import Store  # noqa: E402

from .conftest import base_raw  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"
PAGES = ["Work", "New task", "Failures", "Chat", "Knowledge", "Benchmarks", "Settings"]
# a real 1x1 PNG so st.image can decode it
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                       "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _slug(page: str) -> str:
    return "nav_" + page.lower().replace(" ", "-")


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


def _up(at: AppTest, key: str):
    """The file_uploader with the given key, or None (AppTest exposes them by type)."""
    ups = [u for u in at.get("file_uploader") if u.key == key]
    return ups[0] if ups else None


def test_menu_renders_grouped_sections_and_all_nav_keys(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    for p in PAGES:
        assert at.button(key=_slug(p)), p
    assert at.button(key="nav_toggle")
    assert at.button(key="nav_getting_started")
    text = " ".join(m.value for m in at.markdown)
    for section in ("WORKFLOW", "INSIGHTS", "SYSTEM"):
        assert section in text
    # the active page is primary, every other page secondary
    assert at.button(key="nav_work").proto.type == "primary"
    for p in PAGES[1:]:
        assert at.button(key=_slug(p)).proto.type == "secondary"
    # the status caption reuses the badge logic
    assert any("autonomous team" in c.value for c in at.caption)


def test_nav_toggle_collapses_to_icon_rail_and_back(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    before = at.button(key="nav_work").label
    at.button(key="nav_toggle").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["nav_rail"] is True
    for p in PAGES:  # every nav key survives the collapse
        assert at.button(key=_slug(p)), p
    after = at.button(key="nav_work").label
    assert after != before and "Work" not in after  # icon-only label
    assert "WORKFLOW" not in " ".join(m.value for m in at.markdown)  # captions collapse too
    # navigation still works from the rail
    at.button(key=_slug("New task")).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "New task"
    # and the menu expands back to full labels
    at.button(key="nav_toggle").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["nav_rail"] is False
    assert at.button(key="nav_work").label == before


def test_nav_click_navigates_and_marks_the_page_active(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    at = _app()
    at.run()
    at.button(key="nav_chat").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "Chat"
    assert at.button(key="nav_chat").proto.type == "primary"
    assert at.button(key="nav_work").proto.type == "secondary"
    # the Work page still renders after navigating
    at.button(key="nav_work").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page"] == "Work"
    assert any(h.value == "Work" for h in at.header)


def test_new_task_exposes_both_file_uploaders(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch)
    at = _app()
    at.run()
    at.button(key=_slug("New task")).click().run()
    assert not at.exception, [e.value for e in at.exception]
    up = _up(at, "nr_files")
    assert up is not None and up.accept_multiple_files
    at.radio(key="nt_how").set_value("Add to the queue").run()
    assert not at.exception, [e.value for e in at.exception]
    q_up = _up(at, "qa_files")
    assert q_up is not None and q_up.accept_multiple_files


def test_start_run_passes_uploaded_files_to_the_launcher(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    Store(cfg.db_path).remember_repo(str(sample_repo))
    captured: dict = {}

    def fake_start_run(cfg_, **kw):
        captured.update(kw)
        return "20990101-000000-fake", tmp_path / "run.log"

    monkeypatch.setattr("swarmforge.launcher.start_run", fake_start_run)
    at = _app()
    at.run()
    at.button(key=_slug("New task")).click().run()
    at.text_input(key="nr_title").input("Add export")
    at.text_area(key="nr_body").input("Add CSV export with tests.")
    at.selectbox(key="nr_repo").set_value(str(sample_repo))
    _up(at, "nr_files").set_value([("spec.md", b"# spec", "text/markdown"),
                                   ("shot.png", PNG, "image/png")])
    next(b for b in at.button if "Start run" in b.label).click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert captured["attachments"] == [("spec.md", b"# spec"), ("shot.png", PNG)]
    assert at.session_state["page"] == "Work"


def test_queue_add_saves_uploaded_attachments(tmp_path, monkeypatch, sample_repo):
    cfg = _write_cfg(tmp_path, monkeypatch)
    Store(cfg.db_path).remember_repo(str(sample_repo))
    at = _app()
    at.run()
    at.button(key=_slug("New task")).click().run()
    at.radio(key="nt_how").set_value("Add to the queue").run()
    at.text_input(key="qa_title").input("With files")
    at.text_area(key="qa_body").input("Body with acceptance criteria.")
    at.selectbox(key="qa_repo").set_value(str(sample_repo))
    _up(at, "qa_files").set_value([("notes.txt", b"hello attachment", "text/plain"),
                                   ("shot.png", PNG, "image/png")])
    next(b for b in at.button if b.label == "Add to queue").click().run()
    assert not at.exception, [e.value for e in at.exception]
    items = Store(cfg.db_path).queue_items()
    assert len(items) == 1
    saved = json.loads(items[0]["attachments"] or "[]")
    assert {Path(p).name for p in saved} == {"notes.txt", "shot.png"}
    for p in saved:
        assert Path(p).is_file()
    assert Path(saved[0]).parent.resolve() == (cfg.workdir / "attachments" / "queue").resolve()


def test_run_detail_shows_attachment_thumbnails(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    rid = "20990101-000000-aaaaaa"
    store.create_run(rid, state="COMPLETED", issue_title="With shots", repo="x/y", budget_usd=1.0,
                     config_json="{}")
    att = cfg.workdir / "issues" / "attachments" / rid
    att.mkdir(parents=True)
    (att / "shot.png").write_bytes(PNG)
    (att / "log.txt").write_bytes(b"hello log")
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    rows = [b for b in at.button if (b.key or "").startswith("op_")]
    assert rows
    rows[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Attached:" in c.value and "shot.png" in c.value and "log.txt" in c.value
               for c in at.caption)
    assert len(at.image) >= 1


def test_run_detail_without_attachments_renders(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run("20990101-000000-bbbbbb", state="COMPLETED", issue_title="Plain run", repo="x/y",
                     budget_usd=1.0, config_json="{}")
    at = _app()
    at.run()
    rows = [b for b in at.button if (b.key or "").startswith("op_")]
    rows[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any("Attached:" in c.value for c in at.caption)
    assert not at.image