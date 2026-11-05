"""Unit tests for the cross-process AppTest slot cap helper in conftest."""

import os
import threading
import time
from pathlib import Path

from .conftest import _slot_lock


def test_mutual_exclusion(tmp_path: Path):
    """At most `slots` threads hold the lock at once; all contenders run."""
    slots = 3
    lock = threading.Lock()
    state = {"current": 0, "max": 0, "ran": 0}

    def worker():
        with _slot_lock(tmp_path, "slots", slots, timeout_s=60.0):
            with lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
                state["ran"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["ran"] == 8
    assert state["max"] <= slots


def test_stale_lock_pruned(tmp_path: Path):
    """A slot file older than 900s is removed and the slot acquired."""
    lock_dir = tmp_path / "slots"
    lock_dir.mkdir()
    stale = lock_dir / "slot-0.lock"
    stale.write_text("12345")
    old = time.time() - 1000
    os.utime(stale, (old, old))

    start = time.monotonic()
    with _slot_lock(tmp_path, "slots", 1, timeout_s=10.0):
        pass
    assert time.monotonic() - start < 5.0


def test_fail_open_on_timeout(tmp_path: Path):
    """A fresh foreign lock does not block forever: the context fails open."""
    lock_dir = tmp_path / "slots"
    lock_dir.mkdir()
    foreign = lock_dir / "slot-0.lock"
    foreign.write_text("999999")  # another worker's pid

    start = time.monotonic()
    with _slot_lock(tmp_path, "slots", 1, timeout_s=0.5):
        pass
    assert time.monotonic() - start < 5.0
    # the foreign lock must not have been deleted
    assert foreign.exists()


def test_release(tmp_path: Path):
    """Exiting the context releases the slot file."""
    with _slot_lock(tmp_path, "slots", 2, timeout_s=10.0):
        files = sorted(p.name for p in (tmp_path / "slots").iterdir())
        assert len(files) == 1
        assert files[0].startswith("slot-")
    assert not (tmp_path / "slots" / files[0]).exists()
