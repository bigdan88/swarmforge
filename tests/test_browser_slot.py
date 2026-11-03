"""Unit tests for the cross-process browser slot lock (tests/conftest.py)."""

import os
import threading
import time
from pathlib import Path

from conftest import _file_mutex_slot, _slot_lock  # noqa: F401


def test_file_mutex_released_after_exit(tmp_path: Path):
    lock = tmp_path / "slot"
    with _file_mutex_slot(lock, timeout_s=5):
        assert (Path(str(lock) + ".mutex")).exists()
    assert not (Path(str(lock) + ".mutex")).exists()
    # Reentrant-safe: sequential re-acquisition works.
    with _file_mutex_slot(lock, timeout_s=5):
        pass
    assert not (Path(str(lock) + ".mutex")).exists()


def test_file_mutex_different_paths_do_not_interfere(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    with _file_mutex_slot(a, timeout_s=5), _file_mutex_slot(b, timeout_s=5):
        pass
    assert not (Path(str(a) + ".mutex")).exists()
    assert not (Path(str(b) + ".mutex")).exists()


def test_file_mutex_contention_times_out(tmp_path: Path):
    lock = tmp_path / "slot"
    with _file_mutex_slot(lock, timeout_s=5):
        result = {}

        def contend():
            try:
                with _file_mutex_slot(lock, timeout_s=0.5):
                    result["acquired"] = True
            except TimeoutError:
                result["timeout"] = True

        t = threading.Thread(target=contend)
        t.start()
        t.join(timeout=10)
        assert result.get("timeout") is True
    # After release the contender path would succeed; verify directly.
    with _file_mutex_slot(lock, timeout_s=5):
        pass


def test_file_mutex_stale_lock_is_broken(tmp_path: Path, monkeypatch):
    lock = tmp_path / "slot"
    mutex = Path(str(lock) + ".mutex")
    mutex.write_text("12345")
    # Make it look 3h old.
    old = time.time() - 3 * 60 * 60
    os.utime(mutex, (old, old))
    with _file_mutex_slot(lock, timeout_s=5):
        pass
    assert not mutex.exists()


def test_browser_slot_returns_usable_context_manager(request):
    cm = request.getfixturevalue("browser_slot")
    with cm:
        pass  # acquiring and releasing must not raise on any platform


def test_slot_lock_selects_file_mutex_without_fcntl(tmp_path: Path, monkeypatch):
    import conftest

    monkeypatch.setattr(conftest, "fcntl", None)
    cm = conftest._slot_lock(tmp_path / "sel", timeout_s=5)
    with cm:
        assert (tmp_path / "sel.mutex").exists()
    assert not (tmp_path / "sel.mutex").exists()
