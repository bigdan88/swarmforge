"""Acceptance tests for chatbot attachment support (issue: pass attached
images/files into the status-chat answer path).

Fully offline through the conftest FakeProvider. Maps to AC1..AC6.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import re
import time

import pytest

from swarmforge import chatbot
from swarmforge.attachments import (
    IMAGE_EXTENSIONS,
    MAX_FILES,
    MAX_INLINE_BYTES,
    TEXT_EXTENSIONS,
    classify,
    sanitize_name,
    save_attachments,
)
from swarmforge.chatbot import (
    MAX_INLINE_ATTACHMENT_BYTES,
    MAX_SNAPSHOT_CHARS,
    ChatError,
    _cap,
    answer,
    ask,
    status_snapshot,
)
from swarmforge.config import parse_config
from swarmforge.db import Store
from swarmforge.llm import model_supports_vision, normalize_messages

from .conftest import FakeProvider, base_raw
from .test_chatbot import _make_run, _make_task

PNG = b"\x89PNG\r\n\x1a\n" + b"imgdata" * 8


@pytest.fixture
def seeded_store(tmp_path) -> Store:
    store = Store(tmp_path / "db.sqlite")
    _make_run(store, "run-competed-1234", state="COMPLETED", state_reason="done",
              finished_at=time.time(), created_at=time.time() - 200)
    _make_task(store, "run-competed-1234", "T1", state="SUCCEEDED")
    _make_run(store, "run-failed-5678", state="FAILED", state_reason="tests failed",
              finished_at=time.time(), created_at=time.time() - 100)
    store.failure_save("run-failed-5678", title="Add export", repo="o/r", state="FAILED",
                       category="agent_failure", root_cause="developer task failed",
                       outcome="retry", retry=1)
    store.queue_add("Fix login", "body", "o/r")
    return store


def _cfg(tmp_path, monkeypatch, *, vision=None, planner_model="qwen-fake"):
    monkeypatch.setenv("FAKE_KEY", "k")
    raw = base_raw(tmp_path / "work")
    raw["roles"]["planner"]["model"] = planner_model
    if vision is not None:
        raw["vision_models"] = ["qwen-fake"] if vision else []
    return parse_config(raw)


def _fake(answer_text="noted", model="qwen-fake"):
    return FakeProvider({model: lambda body: {"role": "assistant", "content": answer_text}})


_TS_RE = re.compile(r"generated \d{4}-\d\d-\d\d(?: \d\d:\d\d(?::\d\d)?)? UTC")


def _de_ts(content: str) -> str:
    """Neutralise the snapshot/improvement generation timestamps."""
    return _TS_RE.sub("generated <TS>", content)


# ------------------------------------------------------------------------ AC1

def test_ac1_ask_with_attachments_builds_messages_alongside_status(seeded_store, tmp_path, monkeypatch):
    """ask(..., attachments=[...]) sends file + image content in the same request
    that still carries the <status> snapshot as system message."""
    cfg = _cfg(tmp_path, monkeypatch, vision=True)
    fake = _fake()
    res = ask(cfg, seeded_store, "What do the attachments say?",
              attachments=[{"name": "notes.md", "data": b"UNIQUE-TEXT-CONTENT"},
                           {"name": "pic.png", "data": PNG}],
              transport=fake.transport)
    assert res["answer"] == "noted"
    msgs = fake.requests[0]["messages"]
    # system message unchanged: <status> snapshot with real store data
    assert msgs[0]["role"] == "system"
    assert "<status>" in msgs[0]["content"] and "</status>" in msgs[0]["content"]
    assert "run-comp" in msgs[0]["content"] and "Fix login" in msgs[0]["content"]
    # attached text file content present
    assert any("UNIQUE-TEXT-CONTENT" in str(m.get("content")) for m in msgs)
    assert any("notes.md" in str(m.get("content")) for m in msgs)
    # attached image present as a multimodal block
    att = msgs[-1]
    assert att["role"] == "user" and isinstance(att["content"], list)
    blocks = [b for b in att["content"] if isinstance(b, dict) and b.get("type") == "image_url"]
    assert len(blocks) == 1
    url = blocks[0]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


def test_ac1_system_message_identical_with_and_without_attachments(tmp_path, monkeypatch):
    """The <status> snapshot injection stays byte-identical (modulo its timestamp)
    whether or not attachments are given."""
    cfg = _cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    _make_run(store, "run-quiet-00000")  # RUNNING, no timestamps -> deterministic
    fake1 = _fake()
    ask(cfg, store, "q?", transport=fake1.transport)
    fake2 = _fake()
    ask(cfg, store, "q?", attachments=[{"name": "a.txt", "data": b"hello"}],
        transport=fake2.transport)
    m1 = fake1.requests[0]["messages"][0]["content"]
    m2 = fake2.requests[0]["messages"][0]["content"]
    assert _de_ts(m1) == _de_ts(m2)
    assert "<status>" in m2 and "<improvement_context>" in m2


def test_ac1_async_answer_path_accepts_attachments(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    res = asyncio.run(
        answer(cfg, seeded_store, "read this",
               attachments=[{"name": "log.txt", "data": b"ANSWER-PATH-TEXT"}],
               transport=fake.transport))
    assert res["answer"] == "noted"
    assert any("ANSWER-PATH-TEXT" in str(m.get("content"))
               for m in fake.requests[0]["messages"])


# ------------------------------------------------------------------------ AC2

def test_ac2_text_attachment_content_appears_in_prompt(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(cfg, seeded_store, "summarise", attachments=[{"name": "s.txt", "data": b"line one\nline two"}],
        transport=fake.transport)
    content = fake.requests[0]["messages"][-1]["content"]
    assert "line one\nline two" in content


def test_ac2_oversized_text_never_exceeds_stated_cap(seeded_store, tmp_path, monkeypatch):
    """The inlined content is capped at the stated limit (8192) — strictly,
    with no slack — and carries the truncation marker."""
    assert MAX_INLINE_ATTACHMENT_BYTES == MAX_INLINE_BYTES == 8192
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    data = b"A" * 20_000
    ask(cfg, seeded_store, "summarise", attachments=[{"name": "big.txt", "data": data}],
        transport=fake.transport)
    content = fake.requests[0]["messages"][-1]["content"]
    assert isinstance(content, str)
    header = f'Attached file "big.txt" (first {MAX_INLINE_ATTACHMENT_BYTES} bytes):\n'
    assert content.startswith(header)
    inlined = content[len(header):]
    assert len(inlined) <= MAX_INLINE_ATTACHMENT_BYTES  # cap respected, not exceeded
    assert "A" * 20_000 not in content
    assert "truncated" in inlined
    # kept head + tail around the marker
    assert inlined.startswith("A") and inlined.endswith("A")


def test_ac2_multibyte_text_respects_character_cap(seeded_store, tmp_path, monkeypatch):
    """Non-ASCII oversized files are capped too (documented as characters)."""
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(cfg, seeded_store, "summarise",
        attachments=[{"name": "é.txt", "data": "é".encode("utf-8") * 10_000}],
        transport=fake.transport)
    content = fake.requests[0]["messages"][-1]["content"]
    inlined = content.split(":\n", 1)[1]
    assert len(inlined) <= MAX_INLINE_ATTACHMENT_BYTES


def test_ac2_more_than_max_files_are_skipped(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    atts = [{"name": f"f{i}.txt", "data": f"body {i}".encode()} for i in range(12)]
    ask(cfg, seeded_store, "q", attachments=atts, transport=fake.transport)
    content = " ".join(str(m.get("content")) for m in fake.requests[0]["messages"])
    for i in range(MAX_FILES):
        assert f"f{i}.txt" in content
    assert "f10.txt" not in content and "f11.txt" not in content
    files = list((tmp_path / "work" / "chat" / "attachments").rglob("f*.txt"))
    assert len(files) == MAX_FILES


# ------------------------------------------------------------------------ AC3

def test_ac3_vision_model_receives_image_url_block(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, vision=True)
    fake = _fake()
    res = ask(cfg, seeded_store, "what is in the picture?",
              attachments=[{"name": "pic.png", "data": PNG}], transport=fake.transport)
    assert res["answer"] == "noted"
    last = fake.requests[0]["messages"][-1]
    blocks = [b for b in last["content"] if b.get("type") == "image_url"]
    assert blocks and blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_ac3_vision_detected_by_model_name_heuristic(seeded_store, tmp_path, monkeypatch):
    """A model whose name matches the vision hints gets the image even without
    an explicit vision_models list."""
    cfg = _cfg(tmp_path, monkeypatch, vision=False, planner_model="gpt-4o-fake")
    fake = _fake(model="gpt-4o-fake")
    ask(cfg, seeded_store, "look", attachments=[{"name": "shot.png", "data": PNG}],
        transport=fake.transport)
    last = fake.requests[0]["messages"][-1]
    assert isinstance(last["content"], list)
    assert any(b.get("type") == "image_url" for b in last["content"])


def test_ac3_non_vision_model_gets_placeholder_and_call_succeeds(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, vision=False)
    fake = _fake()
    res = ask(cfg, seeded_store, "what is in the picture?",
              attachments=[{"name": "pic.png", "data": PNG}], transport=fake.transport)
    assert res["answer"] == "noted"  # call still succeeds
    msgs = fake.requests[0]["messages"]
    # nothing that looks like an image block reaches the wire
    assert "image_url" not in str(msgs)
    assert "does not support vision" in str(msgs[-1])
    assert "pic.png" not in str(msgs[-1]) or "removed" in str(msgs[-1])


def test_ac3_normalize_messages_unit():
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "hi"},
                                         {"type": "image_url",
                                          "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    kept, stripped = normalize_messages(msgs, True)
    assert kept == msgs and stripped == 0
    out, stripped = normalize_messages(msgs, False)
    assert stripped == 1
    assert out[0] == msgs[0]
    assert not any(b.get("type") == "image_url" for b in out[1]["content"])
    assert "does not support vision" in out[1]["content"][0]["text"]
    assert any(b.get("type") == "text" and b.get("text") == "hi" for b in out[1]["content"])
    assert msgs[1]["content"][1]["type"] == "image_url"  # input not mutated


def test_ac3_model_supports_vision_predicate():
    assert model_supports_vision("qwen-fake") is False
    assert model_supports_vision("qwen-fake", ["qwen-fake"]) is True
    assert model_supports_vision("qwen-fake", ["qwen"]) is True
    assert model_supports_vision("gpt-4o") is True
    assert model_supports_vision("llava-vl-plus") is True


# ------------------------------------------------------------------------ AC4

def test_ac4_cap_never_exceeds_limit():
    marker = "\n…[snapshot truncated]…\n"
    for limit in (1, 5, 23, 24, 25, 26, 50, 100, 8192, 14_000):
        for n in (limit - 1, limit, limit + 1, limit + len(marker), limit * 2 + 997):
            text = "x" * max(0, n)
            out = _cap(text, limit)
            assert len(out) <= limit, f"_cap exceeded: limit={limit} n={n} got={len(out)}"
            if n <= limit:
                assert out == text
            elif limit > len(marker):
                assert "truncated" in out


def test_ac4_cap_head_and_tail_kept():
    text = "H" * 5000 + "M" * 5000 + "T" * 5000
    out = _cap(text, 1000)
    assert len(out) <= 1000 and "truncated" in out
    assert out.startswith("H") and out.endswith("T")


def test_ac4_status_snapshot_capped_with_marker(tmp_path):
    store = Store(tmp_path / "big.sqlite")
    for i in range(30):
        _make_run(store, f"run-{i:04d}-aaaaaaaaaaaa", issue_title="x" * 400)
    snap = status_snapshot(store, max_runs=30)
    assert len(snap) <= MAX_SNAPSHOT_CHARS
    assert "truncated" in snap


def test_ac4_improvement_context_capped_with_marker(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    for i in range(40):
        _make_run(store, f"run-{i:04d}-aaaaaaaaaaaa", state="FAILED", issue_title="z" * 400)
        if i < 10:
            store.failure_save(f"run-{i:04d}-aaaaaaaaaaaa", outcome="needs you",
                               category="timeout", root_cause="R" * 400, fix="F" * 300,
                               title="t" * 120)
    for i in range(30):
        store.queue_add("queued item " + "q" * 80, "b", "o/r")
    out = chatbot.improvement_context(cfg, store)
    assert len(out) <= 12_000
    assert "truncated" in out


# ------------------------------------------------------------------------ AC5

def test_ac5_constants():
    assert MAX_INLINE_BYTES == 8192
    assert MAX_FILES == 10
    assert isinstance(TEXT_EXTENSIONS, frozenset) and ".txt" in TEXT_EXTENSIONS and ".py" in TEXT_EXTENSIONS
    assert isinstance(IMAGE_EXTENSIONS, frozenset) and {".png", ".jpg", ".gif", ".webp"} <= IMAGE_EXTENSIONS
    assert not (TEXT_EXTENSIONS & IMAGE_EXTENSIONS)


@pytest.mark.parametrize("name,expect", [
    ("notes.md", "notes.md"),
    ("../../evil/x.txt", "x.txt"),                       # basename only
    (r"C:\dir\file.log", "file.log"),                    # windows path
    ("héllo wörld.txt", "h_llo w_rld.txt"),              # charset strip, spaces kept
    ("weird!@#$%.csv", "weird_____.csv"),
])
def test_ac5_sanitize_name(name, expect):
    got = sanitize_name(name)
    assert got == expect
    assert re.fullmatch(r"[A-Za-z0-9._ -]*", got)


def test_ac5_sanitize_name_caps_at_80():
    assert len(sanitize_name("a" * 200)) <= 80
    long_txt = "b" * 200 + ".txt"
    got = sanitize_name(long_txt)
    assert len(got) <= 80 and got.endswith(".txt")
    assert sanitize_name("") == "file"
    assert sanitize_name("///") == "file"


def test_ac5_classify():
    assert classify("a.txt") == "text" and classify("a.PY") == "text"
    assert classify("a.png") == "image" and classify("a.JPEG") == "image"
    assert classify("a.zip") == "other" and classify("noext") == "other"


def test_ac5_save_attachments_collision_safe_absolute_paths(tmp_path):
    target = tmp_path / "out"
    atts = [{"name": "f.txt", "data": b"one"},
            {"name": "f.txt", "data": b"two"},
            {"name": "f.txt", "data": b"three"}]
    paths = save_attachments(target, atts)
    assert [p.name for p in paths] == ["f.txt", "f_1.txt", "f_2.txt"]
    assert all(p.is_absolute() for p in paths)
    assert [p.read_bytes() for p in paths] == [b"one", b"two", b"three"]
    assert save_attachments(target, []) == []


def test_ac5_save_attachments_caps_at_max_files(tmp_path):
    atts = [{"name": f"n{i}.txt", "data": b"x"} for i in range(MAX_FILES + 3)]
    assert len(save_attachments(tmp_path / "o", atts)) == MAX_FILES


# ------------------------------------------------------------------------ AC6

def test_ac6_attachments_is_optional_keyword_only():
    for fn in (chatbot.ask, chatbot.answer):
        param = inspect.signature(fn).parameters["attachments"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None


def test_ac6_no_attachments_message_shape_unchanged(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(cfg, seeded_store, "How many runs?", transport=fake.transport)
    msgs = fake.requests[0]["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system" and "<status>" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "How many runs?"}
    # empty attachment list behaves like none
    fake2 = _fake()
    ask(cfg, seeded_store, "How many runs?", attachments=[], transport=fake2.transport)
    assert len(fake2.requests[0]["messages"]) == 2


def test_ac6_history_still_works_without_attachments(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(cfg, seeded_store, "and failed?",
        history=[{"role": "user", "content": "How many runs?"},
                 {"role": "assistant", "content": "2 runs"}], transport=fake.transport)
    msgs = fake.requests[0]["messages"]
    assert msgs[1] == {"role": "user", "content": "How many runs?"}
    assert msgs[2] == {"role": "assistant", "content": "2 runs"}
    assert msgs[-1] == {"role": "user", "content": "and failed?"}


def test_ac6_chat_error_without_key(seeded_store, tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_KEY", raising=False)
    cfg = _cfg(tmp_path, monkeypatch)
    with pytest.raises(ChatError):
        ask(cfg, seeded_store, "hi")
    with pytest.raises(ChatError):
        ask(cfg, seeded_store, "hi", attachments=[{"name": "a.txt", "data": b"x"}])


def test_ac6_attachments_saved_to_disk_under_workdir(seeded_store, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    fake = _fake()
    ask(cfg, seeded_store, "q", attachments=[{"name": "notes.md", "data": b"data here"}],
        transport=fake.transport)
    files = list((tmp_path / "work" / "chat" / "attachments").rglob("notes.md"))
    assert files and files[0].read_bytes() == b"data here"
