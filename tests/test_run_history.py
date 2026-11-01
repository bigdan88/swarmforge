"""DB-level tests for run history: archived flag migration, set_archived, disk helpers.

Dashboard AppTest tests are added separately; this module covers db.py only.
"""

from __future__ import annotations

import sqlite3
import sys
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
