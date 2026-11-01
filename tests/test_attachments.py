"""Tests for the attachment pipeline (module, CLI, launcher, db, queue plumbing)."""
import json
import subprocess
from pathlib import Path

import yaml

from swarmforge.attachments import (
    IMAGE_EXTENSIONS,
    MAX_FILES,
    MAX_INLINE_BYTES,
    TEXT_EXTENSIONS,
    classify,
    render_section,
    sanitize_name,
    save_attachments,
)
from swarmforge.cli import build_parser
from swarmforge.config import load_config
from swarmforge.db import Store
from tests.conftest import base_raw


# ------------------------------------------------------------------ module
def test_sanitize_name_strips_paths_and_traversal():
    assert sanitize_name("..\\..\\evil.exe") == "evil.exe"
    assert sanitize_name("a/b c.txt") == "b c.txt"
    assert sanitize_name("/etc/passwd") == "passwd"
    assert sanitize_name("weird/*name?.md") == "name.md"  # basename first, then charset strip
    assert sanitize_name("weird name?.md") == "weird name.md"
    assert sanitize_name("   ") == "file"
    assert sanitize_name("") == "file"
    assert len(sanitize_name("x" * 200 + ".txt")) <= 80


def test_save_attachments_writes_and_resolves_collisions(tmp_path):
    dest = tmp_path / "att"
    paths = save_attachments(dest, [("a/b.txt", b"one"), ("b.txt", b"two"), ("b.txt", b"three")])
    assert [p.name for p in paths] == ["b.txt", "b_1.txt", "b_2.txt"]
    assert all(p.is_absolute() for p in paths)
    assert (dest / "b.txt").read_bytes() == b"one"
    assert (dest / "b_1.txt").read_bytes() == b"two"
    assert (dest / "b_2.txt").read_bytes() == b"three"


def test_classify():
    assert classify(Path("x.md")) == "text"
    assert classify(Path("x.LOG")) == "text"
    assert classify(Path("x.png")) == "image"
    assert classify(Path("x.bin")) == "other"
    assert ".md" in TEXT_EXTENSIONS and ".png" in IMAGE_EXTENSIONS
    assert MAX_INLINE_BYTES == 8192 and MAX_FILES == 10


def test_render_section_inlines_text_and_references_images(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello attachment", encoding="utf-8")
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    sec = render_section([txt, img])
    assert sec.startswith("## Attached files")
    assert "### notes.txt" in sec and "hello attachment" in sec
    assert "```" in sec
    assert "pic.png" in sec and str(img) in sec
    assert "PNG fake" not in sec  # images are never inlined


def test_render_section_tolerates_missing_and_empty(tmp_path):
    assert render_section([]) == ""
    missing = tmp_path / "nope.txt"
    sec = render_section([missing])  # must not raise
    assert sec.startswith("## Attached files")
    assert "nope.txt" in sec


def test_render_section_truncates_oversized_text(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("x" * (MAX_INLINE_BYTES + 100), encoding="utf-8")
    sec = render_section([big])
    assert "big.log" in sec and str(big) in sec
    assert "x" * MAX_INLINE_BYTES not in sec or "truncated" in sec or "open it to view" in sec


# ------------------------------------------------------------------ cli
def test_cli_run_accepts_repeatable_attachments():
    p = build_parser()
    args = p.parse_args(["run", "https://github.com/o/r/issues/1",
                         "--attachment", "a.txt", "--attachment", "b.png"])
    assert args.attachment == ["a.txt", "b.png"]
    args = p.parse_args(["run", "https://github.com/o/r/issues/1"])
    assert args.attachment is None


# ------------------------------------------------------------------ launcher
def _cfg(tmp_path, monkeypatch):
    raw = base_raw(tmp_path / "work")
    p = tmp_path / "swarmforge.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return load_config(str(p))


def test_start_run_forwards_attachments(tmp_path, monkeypatch, sample_repo):
    from swarmforge.launcher import start_run
    cfg = _cfg(tmp_path, monkeypatch)
    argvs = []

    class FakeProc:
        pid = 4242

        def __init__(self, args, **kwargs):
            argvs.append(args)

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    run_id, _ = start_run(
        cfg, repo=str(sample_repo), issue_title="T", issue_body="B",
        attachments=[("notes.txt", b"data"), ("pic.png", b"img")])
    assert len(argvs) == 1
    argv = argvs[0]
    atts = [argv[i + 1] for i, a in enumerate(argv) if a == "--attachment"]
    assert len(atts) == 2
    base = (cfg.workdir / "issues" / "attachments" / run_id).resolve()
    for a in atts:
        assert Path(a).parent == base and Path(a).is_file()
    assert Path(atts[1]).name == "pic.png"


def test_start_run_without_attachments_has_no_flag(tmp_path, monkeypatch, sample_repo):
    from swarmforge.launcher import start_run
    cfg = _cfg(tmp_path, monkeypatch)
    argvs = []

    class FakeProc:
        pid = 4242

        def __init__(self, args, **kwargs):
            argvs.append(args)

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    start_run(cfg, repo=str(sample_repo), issue_title="T", issue_body="B")
    assert "--attachment" not in argvs[0]


# ------------------------------------------------------------------ db / queue
def test_queue_attachments_column_roundtrip(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(queue_items)").fetchall()}
    assert "attachments" in cols
    qid = store.queue_add("t", "b", "r", attachments=json.dumps(["/x/y.png"]))
    item = store.queue_item(qid)
    assert item["attachments"] == '["/x/y.png"]'
    assert json.loads(item["attachments"]) == ["/x/y.png"]
    # opening the same db again (migration idempotent)
    store2 = Store(db)
    assert store2.queue_item(qid)["attachments"] == '["/x/y.png"]'


def test_runqueue_parses_attachments(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "s.db")
    qid = store.queue_add("t", "b", "r", attachments=json.dumps(["/a.txt", "/b.png"]))
    item = store.queue_item(qid)
    calls = {}

    class FakeOrch:
        def __init__(self, *a, **k):
            pass

        async def execute(self, *, run_id=None, **k):
            calls["attachments"] = k.get("attachments")
            calls["run_id"] = run_id
            store.create_run(run_id, mode="swarm", state="COMPLETED")
            return run_id

    import swarmforge.orchestrator as orch_mod
    import swarmforge.runqueue as rq
    monkeypatch.setattr(orch_mod, "Orchestrator", FakeOrch)
    import swarmforge.approvals as appr
    monkeypatch.setattr(appr, "dashboard_approver", lambda *a, **k: lambda q: True)

    async def fake_heal(c, s, run_id, launch, attempts=None, echo=None):
        return run_id

    monkeypatch.setattr("swarmforge.postmortem.heal", fake_heal)
    import asyncio
    out = asyncio.run(rq.run_item(cfg, store, item))
    assert calls["attachments"] == ["/a.txt", "/b.png"]
    assert out["status"] == "done"
