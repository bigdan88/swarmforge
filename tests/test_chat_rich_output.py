"""Streaming, download isolation and interruption recovery with fake providers only."""
import base64
import json
from pathlib import Path
import threading

import httpx
import pytest

from swarmforge import chatbot
from swarmforge.chat_files import ReplyFiles
from swarmforge.db import Store
from swarmforge.llm import LLMClient, LLMError
from swarmforge.services.chat_requests import start_reply
from swarmforge.streaming import CompletionStream, JSONReplyStream, StreamInterrupted
from swarmforge.webapp.workspace import WorkspaceReader
from .test_project_kickoff_forms import finish


def event(delta=None, *, finish_reason=None, usage=None):
    return 'data: ' + json.dumps({"choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}], "usage": usage}) + '\n\n'


class Bytes(httpx.AsyncByteStream):
    def __init__(self, values):
        self.values = values

    async def __aiter__(self):
        for value in self.values:
            yield value.encode()


def test_stream_assembles_text_tools_and_usage_without_exposing_reasoning():
    seen = []
    stream = CompletionStream(seen.append)
    stream.feed(json.dumps({"choices": [{"delta": {"reasoning_content": "private reasoning", "content": "Hello "}}]}))
    assert seen == ["Hello "]  # Delivered before the final response exists.
    stream.feed(json.dumps({"choices": [{"delta": {"content": "world", "tool_calls": [{"index": 0, "id": "f1", "function": {"name": "create_", "arguments": '{"name":'}}]}}]}))
    stream.feed(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '"x.txt"}'}}]}, "finish_reason": "tool_calls"}]}))
    stream.feed(json.dumps({"choices": [], "usage": {"estimated_cost": .02, "total_tokens": 10}}))
    stream.feed('[DONE]')
    result = stream.result()
    assert result["choices"][0]["message"]["content"] == "Hello world"
    assert result["choices"][0]["message"]["tool_calls"][0]["function"] == {"name": "create_file", "arguments": '{"name":"x.txt"}'}
    assert result["usage"]["estimated_cost"] == .02 and seen == ["Hello ", "world"]


def test_real_client_streams_from_fake_sse_and_records_provider_usage(cfg):
    chunks, requests = [], []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] and body["stream_options"]["include_usage"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes([
            event({"content": "One "}), event({"content": "two"}), event(finish_reason="stop"),
            event(usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "estimated_cost": .03}), 'data: [DONE]\n\n']))
    store = Store(cfg.db_path)
    try:
        llm = LLMClient(cfg, store=store, run_id="chat:test", transport=httpx.MockTransport(respond))
        reply = finish(llm.chat("planner", [{"role": "user", "content": "Hello"}], on_delta=chunks.append))
        assert reply.content == "One two" and chunks == ["One ", "two"] and len(requests) == 1
        assert store.cost_summary("chat:test")["total"] == .03
    finally:
        store.close()


def test_interrupted_stream_is_not_retried_or_replaced_by_a_fallback(cfg):
    requests, chunks = [], []
    def respond(request):
        requests.append(True)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes([event({"content": "Partial"})]))
    llm = LLMClient(cfg, transport=httpx.MockTransport(respond))
    with pytest.raises(LLMError, match="before completion"):
        finish(llm.chat("planner", [{"role": "user", "content": "Hello"}], on_delta=chunks.append))
    assert chunks == ["Partial"] and len(requests) == 1


def test_interrupted_stream_keeps_known_cost_in_conversation_accounting(cfg):
    store = Store(cfg.db_path)
    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes([
            event({"content": "Partial"}), event(usage={"estimated_cost": .04, "prompt_tokens": 20, "completion_tokens": 10})]))
    try:
        llm = LLMClient(cfg, store=store, run_id="chat:c1", record_errors=False, transport=httpx.MockTransport(respond))
        with pytest.raises(LLMError):
            finish(llm.chat("planner", [{"role": "user", "content": "Hi"}], on_delta=lambda _: None))
        assert store.cost_summary("chat:c1")["total"] == .04
        assert store.llm_calls("chat:c1")[0]["status"] == "error"
    finally:
        store.close()


def test_project_stream_shows_only_reply_text_and_handles_split_unicode():
    seen = []
    stream = JSONReplyStream(seen.append)
    for chunk in ['{"reply":"Here is ', 'your \\uD83D', '\\uDE00 draft",', '"definition":{"goal":"not public JSON"}}']:
        stream(chunk)
    assert seen[0] == "Here is " and seen[-1] == "Here is your 😀 draft"
    assert not any("definition" in value or "not public JSON" in value for value in seen)


def test_files_attach_to_one_message_and_never_expose_server_paths(cfg):
    store = Store(cfg.db_path)
    try:
        store.conversation_create("c1", "Files")
        files = ReplyFiles(cfg, store, "c1")
        made = json.loads(files.create("../../report.csv", "name,count\nbeans,3\n"))
        assert str(cfg.workdir) not in json.dumps(made) and "/" not in made["name"]
        reader = WorkspaceReader(cfg.db_path)
        assert reader.chat_file("c1", made["id"]) is None  # Unattached files cannot be fetched.
        mid = store.conversation_append("c1", "assistant", "Your report is ready.")
        files.attach(mid)
        row = reader.chat_file("c1", made["id"])
        assert Path(row["path"]).read_text() == "name,count\nbeans,3\n"
        assert reader.chat_file("other", made["id"]) is None
        message = reader.conversation("c1")["messages"][-1]
        assert message["files"][0]["id"] == made["id"] and "path" not in message["files"][0]
        with pytest.raises(ValueError):
            files.create("bad.png", "not an image")
        with pytest.raises(ValueError):
            files.create("script.exe", "do not execute")
        svg = json.loads(files.create("diagram.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>'))
        assert svg["kind"] == "file"  # Active markup is download-only.
    finally:
        store.close()


def test_download_route_is_scoped_and_serves_markup_as_an_attachment(cfg, tmp_path):
    from fastapi import HTTPException
    from swarmforge.webapp.app import create_app
    store = Store(cfg.db_path)
    store.conversation_create("c1", "Files")
    files = ReplyFiles(cfg, store, "c1")
    made = json.loads(files.create("example.html", "<h1>Download me</h1>"))
    files.attach(store.conversation_append("c1", "assistant", "A download"))
    try:
        app = create_app(cfg.db_path, config=cfg)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/v1/conversations/{cid}/files/{file_id}")
        response = endpoint("c1", made["id"], inline=True)
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.media_type.startswith("text/plain")
        with pytest.raises(HTTPException) as error:
            endpoint("other", made["id"])
        assert error.value.status_code == 404
        outside = tmp_path / "outside.txt"
        outside.write_text("Not a chat output")
        store._exec("UPDATE chat_files SET path=? WHERE id=?", (str(outside), made["id"]))
        with pytest.raises(HTTPException):
            endpoint("c1", made["id"])
    finally:
        store.close()


def test_chat_model_can_create_downloads_while_streaming_without_executing_code(cfg):
    cfg.chat.tools_enabled = True
    store, calls, seen = Store(cfg.db_path), [], []
    store.conversation_create("c1", "File output")
    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            data = [event({"tool_calls": [{"index": 0, "id": "create-1", "function": {"name": "create_file", "arguments": json.dumps({"name": "hello.py", "content": "print('hello')"})}}]}), event(finish_reason="tool_calls")]
        else:
            assert json.loads(calls[-1]["messages"][-1]["content"])["attached"] is True
            data = [event({"content": "Here is "}), event({"content": "your file."}), event(finish_reason="stop")]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes([*data, event(usage={"estimated_cost": .001}), 'data: [DONE]\n\n']))
    try:
        result = finish(chatbot.answer(cfg, store, "Give me a Python file", conversation="c1", on_text=seen.append, transport=httpx.MockTransport(respond)))
        message = WorkspaceReader(cfg.db_path).conversation("c1")["messages"][-1]
        assert result["answer"] == "Here is your file." and seen == ["Here is ", "Here is your file."]
        assert message["files"][0]["name"] == "hello.py" and len(calls) == 2
    finally:
        store.close()


def test_reply_progress_is_saved_before_completion_and_failure_keeps_partial_history(cfg, monkeypatch):
    store = Store(cfg.db_path)
    store.conversation_create("c1", "Streaming")
    monkeypatch.setattr("swarmforge.services.chat_requests.asyncio.run", finish)
    async def broken(config, store, question, *, conversation, on_text, question_saved=False):
        assert question_saved
        on_text("Visible before completion")
        assert store._one("SELECT partial_text FROM chat_requests")["partial_text"] == "Visible before completion"
        raise RuntimeError("Fake stream disconnected")
    try:
        start_reply(store, cfg, broken, "c1", "Stream please", background=False)
        row = store._one("SELECT * FROM chat_requests")
        assert row["status"] == "failed" and row["assistant_message_id"]
        messages = store.conversation_messages("c1")
        assert len(messages) == 2 and messages[-1]["content"].startswith("Visible before completion")
        assert "interrupted" in messages[-1]["content"]
    finally:
        store.close()


def test_project_chat_runs_as_one_background_reply_with_live_progress_and_a_guarded_draft(cfg, monkeypatch):
    from swarmforge.projects.store import Projects
    from swarmforge.services.chat_requests import wait_idle
    from swarmforge.services.dashboard import DashboardActions
    from .test_projects import definition
    import hashlib
    store = Store(cfg.db_path)
    p = Projects(store).create()
    gate, entered = threading.Event(), threading.Event()
    async def interview(config, store, pid, question, *, expected_spec, on_text, question_saved=False):
        assert question_saved
        on_text("Preparing the milestones…")
        entered.set()
        assert gate.wait(3)
        Projects(store).draft(pid, definition(), expected_spec=expected_spec)
        store.conversation_append(p["conversation_id"], "assistant", "Proposal ready.")
    monkeypatch.setattr("swarmforge.services.chat_requests.asyncio.run", finish)
    actions = DashboardActions(store, cfg, project_interview=interview)
    try:
        result = actions.execute("project-stream-000001", "project_message", p["id"],
                                 {"message": "An inventory app", "expected_spec": hashlib.sha256(b"").hexdigest()})
        assert result["ok"] and not result["draft_ready"] and entered.wait(3)
        view = WorkspaceReader(cfg.db_path).project(p["id"])
        assert view["requests"][0]["status"] == "thinking"
        assert view["requests"][0]["partial_text"] == "Preparing the milestones…"
        assert not Projects(store).get(p["id"])["definition"]
        gate.set()
        assert wait_idle(store, p["conversation_id"])["status"] == "done"
        assert Projects(store).get(p["id"])["status"] == "drafting" and not store.queue_items()
        assert [m["role"] for m in store.conversation_messages(p["conversation_id"])] == ["assistant", "user", "assistant"]
    finally:
        gate.set()
        store.close()


def test_downloads_created_before_a_failure_remain_attached(cfg, monkeypatch):
    store = Store(cfg.db_path)
    store.conversation_create("c1", "Files")
    monkeypatch.setattr("swarmforge.services.chat_requests.asyncio.run", finish)
    async def broken(config, store, question, *, conversation, request_id, question_saved=False):
        ReplyFiles(config, store, conversation, request_id).create("kept.txt", "Retained evidence")
        raise RuntimeError("Fake model failure after file creation")
    try:
        start_reply(store, cfg, broken, "c1", "A file please", background=False)
        message = WorkspaceReader(cfg.db_path).conversation("c1")["messages"][-1]
        assert message["files"][0]["name"] == "kept.txt"
        assert WorkspaceReader(cfg.db_path).chat_file("c1", message["files"][0]["id"])
    finally:
        store.close()
