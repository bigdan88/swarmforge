"""Independent acceptance tests: Chat-page file/image uploader + transcript thumbnails.

Mapped to the issue's acceptance criteria:

AC1  AppTest on the Chat page: with st.file_uploader monkeypatched to return one small
     PNG and one .txt, sending a question renders an image thumbnail element and a
     chip/markdown containing the .txt name in the transcript, and the (fake) answer
     text is returned.
AC2  Uploaded files are saved through swarmforge/attachments.py save_attachments with
     collision-safe names: two same-named files -> name.txt and name_1.txt, a third
     -> name_2.txt; names sanitized to [A-Za-z0-9._-], basename-only, stem <= 80 chars.
AC3  chatbot.ask(..., attachments=[paths]) sends the image as an OpenAI image_url
     base64 data-URL block and inlines text-file contents (<= 8192 bytes) into the
     user message; without attachments the user content stays a plain string and the
     request payload is byte-identical across the no-kwarg / None / [] call forms.
AC4  Regression: sending with no uploaded files calls chatbot.ask with exactly today's
     arguments (no attachments keyword at all) and renders no image elements.
AC5  Scope: one new module (attachments.py), one optional keyword-only kwarg on
     chatbot.ask/answer, Chat-page-only dashboard changes; no CLI/orchestrator/queue
     behavior changes.

Everything is offline: `chatbot.ask` is monkeypatched for AppTest runs, and the real
ask uses tests/test_chatbot.py's FakeProvider transport.
"""
import base64
import inspect
import json
import string
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
import streamlit as st
from streamlit.testing.v1 import AppTest

from swarmforge import chatbot
from swarmforge.attachments import sanitize_name, save_attachments
from swarmforge.chatbot import build_user_content
from swarmforge.config import parse_config
from swarmforge.db import Store

from .conftest import base_raw
from .test_chatbot import FakeProvider
from .test_dashboard import APP, _go, _write_cfg

# a real 1x1 transparent PNG
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

ALLOWED_CHARS = set(string.ascii_letters + string.digits + "._-")


class FakeFile:
    """Stand-in for the object st.file_uploader returns (has .name and .getvalue())."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data

    def read(self) -> bytes:
        return self._data


def _seed(cfg) -> Store:
    """One finished run in the store the dashboard reads (same as tests/test_dashboard_chat.py)."""
    store = Store(cfg.db_path)
    store.create_run("20990101-000000-aaaaaa", state="COMPLETED", issue_title="Export CSV", repo="x/y",
                     budget_usd=5, state_reason="done", config_json="{}")
    return store


def _recording_ask(calls: list):
    def fake_ask(c, store, question, history=None, **kw):
        calls.append({"question": question, "history": list(history or []), "kw": kw})
        return {"answer": "FAKE-ANSWER: both files received", "role": "planner", "model": "qwen-fake",
                "question": question}

    return fake_ask


def _open_chat(at) -> "AppTest":
    _go(at, "Chat")
    assert not at.exception, [e.value for e in at.exception]
    return at


# ============================================================ AC1: AppTest upload -> transcript


def test_ac1_chat_upload_png_and_txt_renders_thumbnail_chip_and_answer(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls: list = []
    monkeypatch.setattr(chatbot, "ask", _recording_ask(calls))
    uploaded = [FakeFile("shot.png", PNG_1PX), FakeFile("notes.txt", b"hello notes")]
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: list(uploaded))

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("what do these files show?").run()
    assert not at.exception, [e.value for e in at.exception]

    # the fake answer text is returned and rendered
    assert len(calls) == 1 and calls[0]["question"] == "what do these files show?"
    texts = [m.value for m in at.markdown]
    assert any("FAKE-ANSWER: both files received" in t for t in texts)
    # both attachments reached chatbot.ask as existing saved paths
    atts = calls[0]["kw"].get("attachments")
    assert atts is not None and len(atts) == 2 and all(Path(p).is_file() for p in atts)
    assert {Path(p).name for p in atts} == {"shot.png", "notes.txt"}
    # an image thumbnail element is rendered in the transcript
    assert len(at.image) == 1
    # a chip/markdown containing the .txt name is rendered
    assert any("notes.txt" in t for t in texts)
    # attachment metadata is persisted on the user message in the chat history
    user_msgs = [m for m in at.session_state["chat_history"] if m.get("role") == "user"]
    kinds = {a["kind"] for a in user_msgs[0].get("attachments") or []}
    assert kinds == {"image", "text"}


def test_ac1_attachments_re_render_from_history_on_next_rerun(tmp_path, monkeypatch):
    """The thumbnail/chip of an earlier turn survive a later rerun (history-driven render)."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls: list = []
    monkeypatch.setattr(chatbot, "ask", _recording_ask(calls))
    uploaded = [FakeFile("shot.png", PNG_1PX), FakeFile("notes.txt", b"hello notes")]
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: list(uploaded))

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("first question").run()
    assert not at.exception, [e.value for e in at.exception]
    at.chat_input[0].set_value("second question").run()
    assert not at.exception, [e.value for e in at.exception]
    # turn 1 (from history) + turn 2 (live) each render one image thumbnail
    assert len(at.image) == 2
    texts = [m.value for m in at.markdown]
    # the first turn's chip persists from history; the second send saved a
    # collision-suffixed copy, so its chip shows notes_1.txt
    assert any("notes.txt" in t for t in texts)
    assert any("notes_1.txt" in t for t in texts)


# ============================================================ AC2: saved via attachments.py


def test_ac2_save_attachments_collision_safe_names(tmp_path):
    d = tmp_path / "chat_attachments"
    out = save_attachments([FakeFile("report.txt", b"one"), FakeFile("report.txt", b"two")], d)
    assert [p.name for p in out] == ["report.txt", "report_1.txt"]
    assert out[0].read_bytes() == b"one" and out[1].read_bytes() == b"two"
    # a third same-named file -> name_2.txt
    out2 = save_attachments([FakeFile("report.txt", b"three")], d)
    assert out2[0].name == "report_2.txt" and out2[0].read_bytes() == b"three"
    # different names are untouched (extension preserved)
    out3 = save_attachments([FakeFile("a.txt", b"x"), FakeFile("b.png", PNG_1PX)], d)
    assert [p.name for p in out3] == ["a.txt", "b.png"]


def test_ac2_sanitize_name_rules():
    # basename only (posix and windows paths)
    assert sanitize_name("../../evil/dir/report file (1).txt") == "report_file_1.txt"
    assert sanitize_name("C:\\tmp\\photos\\shot.png") == "shot.png"
    # charset is [A-Za-z0-9._-]
    weird = sanitize_name("wéird $name#! .txt")
    assert set(weird) <= ALLOWED_CHARS and weird.endswith(".txt")
    # stem capped at 80 chars, extension preserved
    capped = sanitize_name("x" * 200 + ".txt")
    assert capped == "x" * 80 + ".txt"
    # degenerate names still produce a usable file name
    assert sanitize_name("...") == "file"


def test_ac2_save_attachments_sanitizes_upload_names(tmp_path):
    d = tmp_path / "att"
    out = save_attachments([FakeFile("../../tmp/dir/evil name!.png", PNG_1PX)], d)
    assert out[0].name == "evil_name.png"
    assert out[0].is_file() and out[0].read_bytes() == PNG_1PX


def test_ac2_dashboard_sends_uploads_to_workdir_chat_attachments_collision_safe(tmp_path, monkeypatch):
    """End-to-end through the Chat page: two sends of the same filename save name.txt then name_1.txt."""
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls: list = []
    monkeypatch.setattr(chatbot, "ask", _recording_ask(calls))
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: [FakeFile("notes.txt", b"hello")])

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("first").run()
    at.chat_input[0].set_value("second").run()
    assert not at.exception, [e.value for e in at.exception]

    d = cfg.workdir / "chat_attachments"
    names1 = [Path(p).name for p in calls[0]["kw"]["attachments"]]
    names2 = [Path(p).name for p in calls[1]["kw"]["attachments"]]
    assert names1 == ["notes.txt"]
    assert names2 == ["notes_1.txt"]
    assert Path(calls[0]["kw"]["attachments"][0]) == d / "notes.txt"
    assert (d / "notes.txt").read_bytes() == b"hello"
    assert (d / "notes_1.txt").is_file()


# ============================================================ AC3: multimodal ask payload


class _Fake(FakeProvider):
    """Also capture the full request body for byte-comparison."""

    def __init__(self):
        super().__init__()
        self.bodies: list[str] = []

    async def handle(self, request):
        body = json.loads(request.content)
        self.bodies.append(json.dumps(body, sort_keys=True))
        return await super().handle(request)


def _fake_cfg(tmp_path, monkeypatch, vision: bool = False):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    raw = base_raw(tmp_path)
    if vision:
        raw["vision_models"] = ["qwen-fake"]
    return parse_config(raw)


def test_ac3_ask_sends_image_data_url_block_and_inlines_text(tmp_path, monkeypatch):
    cfg = _fake_cfg(tmp_path, monkeypatch, vision=True)
    store = Store(cfg.db_path)
    png = tmp_path / "pic.png"
    png.write_bytes(PNG_1PX)
    txt = tmp_path / "notes.txt"
    txt.write_bytes(b"hello notes")
    fake = _Fake()
    res = chatbot.ask(cfg, store, "what do these show?", attachments=[png, txt], transport=fake.transport)
    assert res["answer"] == "ok, saw the files"
    user = fake.requests[-1]["messages"][-1]
    assert user["role"] == "user" and isinstance(user["content"], list)
    texts = [b["text"] for b in user["content"] if b.get("type") == "text"]
    assert any(t.startswith("what do these show?") for t in texts)
    assert any("hello notes" in t for t in texts)  # text file inlined
    imgs = [b for b in user["content"] if b.get("type") == "image_url"]
    assert len(imgs) == 1
    url = imgs[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_1PX


def test_ac3_inline_text_limit_exact(tmp_path):
    # exactly 8192 bytes is inlined; 8193 is not (<=8192 per AC3)
    at_limit = tmp_path / "at_limit.txt"
    at_limit.write_bytes(b"B" * 8192)
    over = tmp_path / "over.txt"
    over.write_bytes(b"C" * 8193)
    content = build_user_content("explain", [at_limit, over])
    assert isinstance(content, list)
    text = content[0]["text"]
    assert "BBB" in text  # 8192-byte file inlined
    assert "CCC" not in text  # 8193-byte file NOT inlined
    assert "- over.txt (text)" in text  # ...but still listed by name


def test_ac3_no_attachments_user_content_is_plain_string_and_payload_byte_identical(tmp_path, monkeypatch):
    cfg = _fake_cfg(tmp_path, monkeypatch)
    store = Store(cfg.db_path)
    fake = _Fake()
    # freeze the time-dependent snapshot so bodies can be compared byte-for-byte
    monkeypatch.setattr(chatbot, "status_snapshot", lambda store_, max_runs=12: "FROZEN-SNAPSHOT")
    monkeypatch.setattr(chatbot, "improvement_context", lambda cfg_, store_: "FROZEN-IMPROVEMENT")

    chatbot.ask(cfg, store, "how are the runs?", transport=fake.transport)
    chatbot.ask(cfg, store, "how are the runs?", transport=fake.transport, attachments=None)
    chatbot.ask(cfg, store, "how are the runs?", transport=fake.transport, attachments=[])

    assert len(fake.bodies) == 3
    assert fake.bodies[0] == fake.bodies[1] == fake.bodies[2]  # byte-identical
    msgs = fake.requests[0]["messages"]
    assert msgs[-1] == {"role": "user", "content": "how are the runs?"}  # plain string, as today
    assert all(isinstance(m["content"], str) for m in msgs)
    assert "image_url" not in fake.bodies[0]


# ============================================================ AC4: no-upload regression


def test_ac4_no_upload_send_calls_ask_with_exactly_todays_arguments(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path, monkeypatch)
    _seed(cfg)
    calls: list = []
    monkeypatch.setattr(chatbot, "ask", _recording_ask(calls))
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: None)

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    _open_chat(at)
    at.chat_input[0].set_value("still works?").run()
    assert not at.exception, [e.value for e in at.exception]

    assert len(calls) == 1
    kw = calls[0]["kw"]
    assert "attachments" not in kw
    # exactly today's call shape: history + conversation, nothing else
    assert set(kw) == {"conversation"}
    assert calls[0]["history"] == []
    # nothing image-like is rendered, and the answer still comes through
    assert not at.image
    texts = [m.value for m in at.markdown]
    assert any("still works?" in t for t in texts)
    assert any("FAKE-ANSWER: both files received" in t for t in texts)


def test_ac4_cli_ask_never_passes_attachments(tmp_path, monkeypatch):
    from swarmforge.cli import main

    cfg_path = tmp_path / "swarmforge.yaml"
    cfg_path.write_text(yaml.safe_dump(base_raw(tmp_path / "work")))
    recorded = {}

    def fake_ask(cfg, store, question, history=None, *, conversation=None, transport=None, max_runs=12):
        recorded["ok"] = True
        return {"answer": "All good.", "role": "chat", "model": "m1", "question": question}

    monkeypatch.setattr(chatbot, "ask", fake_ask)
    monkeypatch.setenv("FAKE_KEY", "test-key")
    code = main(["--config", str(cfg_path), "ask", "how are runs doing?"])
    assert code == 0
    assert recorded.get("ok") is True  # the strict fake signature has no **kw: any
    # "attachments" kwarg from the CLI would have raised TypeError


# ============================================================ AC5: scope checks


def test_ac5_ask_and_answer_gained_exactly_one_optional_keyword_only_param():
    for fn in (chatbot.ask, chatbot.answer):
        params = inspect.signature(fn).parameters
        assert "attachments" in params, fn.__name__
        assert params["attachments"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["attachments"].default is None
    assert set(inspect.signature(chatbot.ask).parameters) == {
        "cfg", "store", "question", "history", "conversation", "transport", "max_runs", "attachments"}
    assert set(inspect.signature(chatbot.answer).parameters) == {
        "cfg", "store", "question", "history", "conversation", "transport", "max_runs", "budget",
        "attachments"}


def test_ac5_only_one_new_module_and_no_attachments_outside_the_three_files():
    pkg = Path(chatbot.__file__).parent
    assert (pkg / "attachments.py").is_file()
    touched = {"attachments.py", "chatbot.py", "dashboard.py"}
    for f in sorted(pkg.rglob("*.py")):
        if f.name == "__init__.py" or f.name in touched:
            continue
        assert "attachments" not in f.read_text(encoding="utf-8"), (
            f"{f}: attachment pipeline must stay out of {f.name}")
    # ...and the two consumers really import it (single shared module, no duplication)
    assert "from . import attachments" in (pkg / "chatbot.py").read_text(encoding="utf-8")
    dash = (pkg / "dashboard.py").read_text(encoding="utf-8")
    assert "from swarmforge.attachments import" in dash


def test_ac5_dashboard_attachment_code_is_chat_page_only():
    from swarmforge import dashboard

    lines = Path(dashboard.__file__).read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("def _chat_save_attachments"))
    end = next(i for i, l in enumerate(lines) if "NEW TASK page" in l)
    assert start < end
    hits = [i for i, l in enumerate(lines) if "attachment" in l.lower()]
    assert hits, "expected the attachment pipeline in the dashboard"
    assert all(start <= i < end for i in hits), [
        (i + 1, lines[i]) for i in hits if not (start <= i < end)]
