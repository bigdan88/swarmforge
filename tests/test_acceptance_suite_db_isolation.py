"""Regression tests for the flaky-suite fix (the one failure that sank run
20261102-001801: tests/test_theme.py died with sqlite 'duplicate column name:
max_repair_cycles').

Root cause: `from swarmforge import dashboard` opens its SQLite store at import
time (module level `store = get_store(db_path)`).  With no SWARMFORGE_DB and no
SWARMFORGE_CONFIG the db path falls back to ~/.swarmforge/swarmforge.db, so
several pytest-xdist workers that import the dashboard concurrently create and
migrate the same brand-new file; the PRAGMA-guarded ALTERs in db._migrate race
("duplicate column name", "database is locked") and the error surfaces in
whichever test imports the dashboard first (test_theme, test_chat_sidebar,
test_acceptance_chat_uploader, ...).  Nothing to do with the code under test.

Fix: tests/conftest.py pins SWARMFORGE_DB to a per-process temp database before
any test module is imported.  These tests assert that isolation is in place.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def test_swarmforge_db_is_pinned_to_a_temp_file_for_every_worker() -> None:
    """A test process must never fall back to the real user database."""
    db = os.environ.get("SWARMFORGE_DB")
    assert db, "conftest must pin SWARMFORGE_DB for every test process"
    p = Path(db)
    assert p.parent != Path.home() / ".swarmforge", p
    assert str(p).startswith(tempfile.gettempdir()), p
    assert f"swarmforge-test-db-{os.getpid()}" in str(p), p


def test_dashboard_module_store_uses_the_pinned_test_db() -> None:
    """The import-time dashboard store points at the per-process temp DB, not ~/.swarmforge."""
    from swarmforge import dashboard

    assert Path(dashboard.store.path) == Path(os.environ["SWARMFORGE_DB"])
    assert Path(dashboard.store.path).parent != Path.home() / ".swarmforge"
