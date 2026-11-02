"""Dashboard: 'skipped out-of-scope findings: N' caption on the Review tab."""
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from swarmforge.db import Store  # noqa: E402

from .test_dashboard import APP, _open, _write_cfg  # noqa: E402


def _seed(tmp_path, monkeypatch, events):
    cfg = _write_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    store.create_run("r-scope", issue_title="Scope caption run", state="COMPLETED")
    for kind, message in events:
        store.event("r-scope", kind, message, level="warn")
    return cfg


def _review_captions(cfg):
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open(at)
    at.tabs[4].run()
    assert not at.exception, [e.value for e in at.exception]
    return [c.value for c in at.caption]


def test_review_tab_counts_out_of_scope_skips(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch, [
        ("scope", "out-of-scope finding skipped from repair creation (not in original plan file set): "
                  "GIT env (src/swarmforge/repos.py)"),
        ("scope", "out-of-scope finding skipped from repair creation (not in original plan file set): "
                  "ruff in Dockerfile"),
        ("scope", "plan scope computed from 3 task file sets"),  # decoy: no 'out-of-scope'
    ])
    captions = _review_captions(cfg)
    assert "skipped out-of-scope findings: 2" in captions


def test_review_tab_no_caption_without_scope_skips(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch, [("scope", "plan scope computed from 3 task file sets")])
    captions = _review_captions(cfg)
    assert not any("out-of-scope findings" in c for c in captions)
