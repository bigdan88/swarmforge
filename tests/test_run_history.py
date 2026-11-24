"""Tests for run history: archived flag migration, set_archived, disk helpers and the
Work-page UI (filters, archive/unarchive, free disk space, workdir usage) via AppTest.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from swarmforge.db import Store, dir_size_bytes, free_run_disk


def test_dir_size_bytes(tmp_path: Path) -> None:
    assert dir_size_bytes(tmp_path / "missing") == 0
    (tmp_path / "a").write_bytes(b"x" * 100)
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)
    (sub / "b").write_bytes(b"y" * 23)
    assert dir_size_bytes(tmp_path) == 123


def test_free_run_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "run1"
    (run_dir / "repo" / ".git").mkdir(parents=True)
    (run_dir / "repo" / ".git" / "obj").write_bytes(b"z" * 500)
    (run_dir / "repo" / "f.txt").write_bytes(b"a" * 100)
    (run_dir / "worktrees" / "wt1").mkdir(parents=True)
    (run_dir / "worktrees" / "wt1" / "g.txt").write_bytes(b"b" * 50)
    (run_dir / "report.md").write_text("# report")
    (run_dir / "transcripts").mkdir()
    (run_dir / "transcripts" / "t1.jsonl").write_text("{}")
    (run_dir / "logs").mkdir()
    (run_dir / "logs" / "l.txt").write_text("log")

    freed = free_run_disk(run_dir)
    assert freed == 650
    assert not (run_dir / "repo").exists()
    assert not (run_dir / "worktrees").exists()
    assert (run_dir / "report.md").exists()
    assert (run_dir / "transcripts" / "t1.jsonl").exists()
    assert (run_dir / "logs" / "l.txt").exists()
    # idempotent: nothing left to free, no error
    assert free_run_disk(run_dir) == 0


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35),
    reason="ALTER TABLE DROP COLUMN requires SQLite >= 3.35",
)
def test_migration_adds_archived_on_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    store = Store(db_path)
    store.create_run("run-1", issue_title="do things", state="ISSUE_RECEIVED")
    store.close()

    # simulate an old database that predates the archived column
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE runs DROP COLUMN archived")
    conn.commit()
    conn.close()

    store = Store(db_path)
    row = store.get_run("run-1")
    assert row is not None
    assert row["archived"] == 0
    store.set_archived("run-1", True)
    assert store.get_run("run-1")["archived"] == 1
    store.set_archived("run-1", False)
    assert store.get_run("run-1")["archived"] == 0
    store.close()


# -------------------------------------------------------------------- dashboard UI
#
# AppTest coverage for the Work-page features from the "filter, archive and clean up
# runs" issue: filters above the runs table, the archived flag UI, free-disk-space and
# the workdir disk-usage display.

pytest.importorskip("streamlit")

import yaml
from streamlit.testing.v1 import AppTest

from swarmforge.config import load_config

from .conftest import base_raw

APP = Path(__file__).resolve().parents[1] / "src" / "swarmforge" / "dashboard.py"

TERMINAL_STATES = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}


def _setup(tmp_path: Path, monkeypatch):
    """Point the dashboard at a throwaway config/workdir and return the Config."""
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


def _seed_run(store: Store, rid: str, state: str, *, repo: str = "bigdan88/alpha",
              mode: str = "swarm", title: str | None = None, run_dir=None,
              pid=None, t0: float | None = None) -> None:
    t0 = time.time() if t0 is None else t0
    store.create_run(
        rid,
        state=state,
        repo=repo,
        mode=mode,
        issue_title=title or f"title of {rid}",
        run_dir=str(run_dir) if run_dir else "",
        pid=pid,
        created_at=t0,
        finished_at=t0 + 5 if state in TERMINAL_STATES else None,
    )


def _app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=180)
    at.run()
    assert len(at.exception) == 0, f"dashboard crashed: {[e.value for e in at.exception]}"
    return at


def _visible_run_ids(at: AppTest) -> set:
    """Ids of runs shown as rows in the Work list (keyed like op_r_<run_id> / op_r:<run_id>)."""
    ids = set()
    for b in at.button:
        key = b.key or ""
        for prefix in ("op_r_", "op_r:"):
            if key.startswith(prefix):
                ids.add(key[len(prefix):])
    return ids


def _page_text(at: AppTest) -> str:
    parts: list[str] = []
    for kind in ("markdown", "caption", "text", "subheader", "header",
                 "success", "info", "warning", "error"):
        parts.extend(str(getattr(el, "value", "")) for el in at.get(kind)
                     if not str(getattr(el, "value", "")).lstrip().startswith("<style"))
    return "\n".join(parts)


def _search_box(at: AppTest):
    """The text-search input above the runs table (fails loudly if absent)."""
    box = next((t for t in at.text_input
                if (t.key or "") == "w_search" or "search" in (t.label or "").lower()), None)
    assert box is not None, "no text search input above the runs table"
    return box


def _pick_widget(at: AppTest, needle: str,
                 kinds=("multiselect", "selectbox", "radio", "button_group")):
    """First widget whose label contains `needle` among the given kinds."""
    for kind in kinds:
        for el in at.get(kind):
            if needle in (getattr(el, "label", "") or "").lower():
                return el
    return None


def _set_filter(at: AppTest, el, value) -> AppTest:
    if getattr(el, "type", "") == "multiselect":
        el.set_value([value] if isinstance(value, str) else list(value))
    else:
        el.set_value(value if isinstance(value, str) else value[0])
    el.run()
    return at


def _buttons(at: AppTest, needle: str) -> list:
    return [b for b in at.button if needle in (b.label or "").lower()]


def _open_run(at: AppTest, rid: str) -> AppTest:
    rows = [b for b in at.button if (b.key or "") in (f"op_r_{rid}", f"op_r:{rid}")]
    assert rows, f"run {rid} is not listed in the Work table"
    rows[0].click().run()
    return at


# --------------------------------------------------------------------------- AC1


def test_state_multiselect_filters_work_list(tmp_path, monkeypatch) -> None:
    """A state multi-select above the runs table narrows the list to chosen states."""
    cfg = _setup(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed_run(store, "r-done", "COMPLETED", title="Done export csv", t0=1000.0)
    _seed_run(store, "r-fail", "FAILED", title="Failed export csv", t0=1001.0)
    _seed_run(store, "r-live", "TASK_EXECUTION", title="Live export csv", t0=1002.0)
    store.close()

    at = _app()
    assert _visible_run_ids(at) == {"r-done", "r-fail", "r-live"}

    ms = _pick_widget(at, "state")
    assert ms is not None, "no state filter widget above the runs table"
    opts = [str(o) for o in ms.options]
    done = next((o for o in opts if "COMPLETED" in o.upper()), None)
    assert done is not None, f"state filter options have no COMPLETED entry: {opts}"
    _set_filter(at, ms, done)
    assert _visible_run_ids(at) == {"r-done"}

    ms = _pick_widget(at, "state")
    fails = next(str(o) for o in ms.options if "FAILED" in str(o).upper())
    _set_filter(at, ms, [done, fails])
    assert _visible_run_ids(at) == {"r-done", "r-fail"}


def test_repo_filter_narrows_work_list(tmp_path, monkeypatch) -> None:
    """A repository filter narrows the runs table to one repo's runs."""
    cfg = _setup(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed_run(store, "r-alpha", "COMPLETED", repo="bigdan88/alpha", title="Alpha task", t0=1000.0)
    _seed_run(store, "r-beta", "COMPLETED", repo="bigdan88/beta", title="Beta task", t0=1001.0)
    store.close()

    at = _app()
    assert _visible_run_ids(at) == {"r-alpha", "r-beta"}

    el = _pick_widget(at, "repo")
    assert el is not None, "no repository filter widget above the runs table"
    opt = next((str(o) for o in el.options if "alpha" in str(o)), None)
    assert opt is not None, f"repository filter has no bigdan88/alpha option: {list(el.options)}"
    _set_filter(at, el, opt)
    assert _visible_run_ids(at) == {"r-alpha"}


def test_mode_filter_narrows_work_list(tmp_path, monkeypatch) -> None:
    """A mode filter (swarm/single) narrows the runs table to matching runs."""
    cfg = _setup(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed_run(store, "r-swarm", "COMPLETED", mode="swarm", title="Swarm task", t0=1000.0)
    _seed_run(store, "r-single", "COMPLETED", mode="single", title="Single task", t0=1001.0)
    store.close()

    at = _app()
    assert _visible_run_ids(at) == {"r-swarm", "r-single"}

    el = _pick_widget(at, "mode") or _pick_widget(at, "team")
    assert el is not None, "no mode filter widget above the runs table"
    opt = next((str(o) for o in el.options if "single" in str(o).lower()), None)
    assert opt is not None, f"mode filter has no single option: {list(el.options)}"
    _set_filter(at, el, opt)
    assert _visible_run_ids(at) == {"r-single"}


def test_text_search_filters_by_issue_title(tmp_path, monkeypatch) -> None:
    """The text search above the runs table filters on the issue title."""
    cfg = _setup(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed_run(store, "r-1", "COMPLETED", title="Export inventory as CSV", t0=1000.0)
    _seed_run(store, "r-2", "COMPLETED", title="Add zebra-unicorn rate limiter", t0=1001.0)
    _seed_run(store, "r-3", "FAILED", title="Rotate signing keys", t0=1002.0)
    store.close()

    at = _app()
    assert len(_visible_run_ids(at)) == 3

    box = _search_box(at)
    box.set_value("zebra-unicorn").run()
    assert _visible_run_ids(at) == {"r-2"}

    _search_box(at).set_value("rotate").run()
    assert _visible_run_ids(at) == {"r-3"}


# --------------------------------------------------------------------------- AC2


def test_archived_run_hidden_until_show_archived_ticked(tmp_path, monkeypatch) -> None:
    """Archived runs disappear from the list unless 'Show archived' is ticked."""
    cfg = _setup(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed_run(store, "r-vis", "COMPLETED", title="Visible run task", t0=1000.0)
    _seed_run(store, "r-arc", "COMPLETED", title="Archived run task", t0=1001.0)
    store.set_archived("r-arc", True)
    assert store.get_run("r-arc")["archived"] == 1
    store.close()

    at = _app()
    assert _visible_run_ids(at) == {"r-vis"}, "archived run must be hidden by default"
    assert "Archived run task" not in _page_text(at)

    cb = next((c for c in at.checkbox if "archiv" in (c.label or "").lower()), None)
    assert cb is not None, "no 'Show archived' checkbox on the Work page"
    cb.check().run()
    assert _visible_run_ids(at) == {"r-vis", "r-arc"}


def test_archive_and_unarchive_buttons_toggle_the_flag(tmp_path, monkeypatch) -> None:
    """An Archive button sets runs.archived; an Unarchive button clears it."""
    cfg = _setup(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _seed_run(store, "r-1", "COMPLETED", title="Task to archive", t0=1000.0)

    at = _app()
    _open_run(at, "r-1")
    arch = [b for b in _buttons(at, "archive")
            if "unarchive" not in (b.label or "").lower() and "archived" not in (b.label or "").lower()]
    assert arch, "no Archive button for a run"
    arch[0].click().run()
    assert store.get_run("r-1")["archived"] == 1

    # hidden by default again after archiving
    at.run()
    assert "r-1" not in _visible_run_ids(at)

    cb = next((c for c in at.checkbox if "archiv" in (c.label or "").lower()), None)
    assert cb is not None, "no 'Show archived' checkbox on the Work page"
    cb.check().run()
    _open_run(at, "r-1")
    un = _buttons(at, "unarchive")
    assert un, "no Unarchive button for an archived run"
    un[0].click().run()
    assert store.get_run("r-1")["archived"] == 0
    store.close()


# --------------------------------------------------------------------------- AC3


def _make_run_dir(workdir: Path, rid: str) -> Path:
    run_dir = workdir / "runs" / rid
    (run_dir / "repo" / ".git").mkdir(parents=True)
    (run_dir / "repo" / ".git" / "pack.idx").write_bytes(b"0" * (2 * 1024 * 1024))
    (run_dir / "repo" / "README.md").write_text("x")
    (run_dir / "worktrees" / "wt1").mkdir(parents=True)
    (run_dir / "worktrees" / "wt1" / "f.txt").write_bytes(b"1" * 1024)
    (run_dir / "report.md").write_text("# report")
    (run_dir / "transcripts").mkdir()
    (run_dir / "transcripts" / "t1.jsonl").write_text("{}\n")
    (run_dir / "logs").mkdir()
    (run_dir / "logs" / "run.log").write_text("log")
    return run_dir


def _confirm_checkbox(at: AppTest):
    return next((c for c in at.checkbox
                 if any(k in (c.label or "").lower() for k in ("confirm", "free", "delete"))), None)


def test_free_disk_space_requires_confirmation_and_frees_only_clones(tmp_path, monkeypatch) -> None:
    """Free disk space needs the confirmation tick, deletes repo/worktrees only,
    keeps the DB row, report, transcripts and logs, and reports the MB freed."""
    cfg = _setup(tmp_path, monkeypatch)
    run_dir = _make_run_dir(cfg.workdir, "r-1")
    store = Store(cfg.db_path)
    _seed_run(store, "r-1", "COMPLETED", title="Finished run", run_dir=run_dir, t0=1000.0)
    store.close()

    at = _app()
    _open_run(at, "r-1")
    btns = _buttons(at, "free disk") or _buttons(at, "free space")
    assert btns, "no 'Free disk space' button for a finished run"
    btns[0].click().run()

    confirm = _confirm_checkbox(at)
    assert confirm is not None, "no confirmation checkbox for the free-disk-space action"

    # without the confirmation ticked, nothing is deleted
    btns = _buttons(at, "free disk") or _buttons(at, "free space")
    btns[0].click().run()
    assert (run_dir / "repo").exists(), "clones deleted without confirmation"
    assert (run_dir / "worktrees").exists(), "worktrees deleted without confirmation"

    # with confirmation: clones/worktrees go, everything else stays
    confirm = _confirm_checkbox(at)
    confirm.check().run()
    btns = _buttons(at, "free disk") or _buttons(at, "free space")
    btns[0].click().run()

    assert not (run_dir / "repo").exists()
    assert not (run_dir / "worktrees").exists()
    assert (run_dir / "report.md").exists()
    assert (run_dir / "transcripts" / "t1.jsonl").exists()
    assert (run_dir / "logs" / "run.log").exists()

    store = Store(cfg.db_path)
    assert store.get_run("r-1") is not None, "database row must survive a disk cleanup"
    store.close()

    assert re.search(r"\d+(?:[.,]\d+)?\s*M(?:i)?B", _page_text(at)), \
        "the freed amount in MB is not shown after cleaning up"


def test_free_disk_space_refused_for_active_run(tmp_path, monkeypatch) -> None:
    """An active run's clone is never deleted by the free-disk-space action."""
    cfg = _setup(tmp_path, monkeypatch)
    run_dir = _make_run_dir(cfg.workdir, "r-live")
    store = Store(cfg.db_path)
    _seed_run(store, "r-live", "TASK_EXECUTION", title="Running task",
              run_dir=run_dir, pid=os.getpid(), t0=1000.0)
    store.close()

    at = _app()
    _open_run(at, "r-live")
    # premise: the dashboard really treats the seeded run as active (live settings panel shown)
    assert any((b.key or "").startswith("cancel_") for b in at.button), \
        "the seeded run was not treated as active by the dashboard"

    btns = _buttons(at, "free disk") or _buttons(at, "free space")
    if btns:
        # if the action is offered anyway, it must refuse to delete anything
        confirm = _confirm_checkbox(at)
        if confirm is not None:
            confirm.check().run()
            btns = _buttons(at, "free disk") or _buttons(at, "free space")
        btns[0].click().run()
    else:
        # refusal by omission: the panel must explain the action waits for the run to finish
        assert "free disk space" in _page_text(at).lower(), \
            "active run detail neither offers nor explains the free-disk-space action"

    assert (run_dir / "repo").exists(), "free disk space deleted the clone of an active run"
    assert (run_dir / "worktrees").exists(), "free disk space deleted the worktrees of an active run"
    assert (run_dir / "report.md").exists()


# --------------------------------------------------------------------------- AC4


def test_work_page_shows_total_workdir_disk_usage(tmp_path, monkeypatch) -> None:
    """The Work page displays the total disk usage of the workdir."""
    cfg = _setup(tmp_path, monkeypatch)
    run_dir = _make_run_dir(cfg.workdir, "r-1")  # ~2 MB of files inside the workdir
    store = Store(cfg.db_path)
    _seed_run(store, "r-1", "COMPLETED", title="Run with files", run_dir=run_dir, t0=1000.0)
    store.close()

    at = _app()
    text = _page_text(at)
    assert re.search(r"\d+(?:[.,]\d+)?\s(?:KB|MB|GB|TB|KiB|MiB|GiB|bytes)\b", text), \
        "the Work page does not show the workdir's total disk usage"
    # the figure must reflect the ~2 MB of files just placed in the workdir, not a placeholder
    mbs = [float(m.replace(",", ".")) for m in re.findall(r"(\d+(?:[.,]\d+)?)\s*MB", text)]
    assert any(v >= 2.0 for v in mbs), \
        f"no shown MB figure accounts for the seeded ~2 MB of workdir files: {mbs}"
