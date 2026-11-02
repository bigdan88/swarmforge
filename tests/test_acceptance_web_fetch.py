"""Acceptance tests for the web-fetch chatbot tool (issue: tools/web.py).

One test group per acceptance criterion (AC1..AC6). All HTTP is mocked with
httpx.MockTransport; nothing touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from swarmforge.chat_tools import ToolRegistry
from swarmforge.chatbot import ask, build_chat_registry
from swarmforge.db import Store
from swarmforge.tools import web as web_module
from swarmforge.tools.web import (
    DEFAULT_TIMEOUT_S,
    WebToolError,
    fetch_url,
)

from .conftest import FakeProvider, tool_call

HTML_PAGE = """<html><head><title>T</title><script>var x = 'secret';</script></head>
<body><nav>menu</nav>
<h1>Hello Docs</h1>
<p>Check the <a href="https://example.com/guide">guide</a> for details.</p>
<ul><li>one</li><li>two</li></ul>
<footer>footer text</footer></body></html"""


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "db.sqlite")


def transport_for(handler):
    """MockTransport plus a recorder of every requested URL."""
    requested: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    return httpx.MockTransport(wrapped), requested


# ------------------------------------------------------------------ AC1
# fetch_url(url, max_chars=8000, extract="markdown") returns clean Markdown,
# truncated to max_chars with an explicit truncation marker.


def test_ac1_signature_defaults():
    import inspect

    sig = inspect.signature(fetch_url)
    assert sig.parameters["url"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert sig.parameters["max_chars"].default == 8000
    assert sig.parameters["extract"].default == "markdown"
    assert inspect.iscoroutinefunction(fetch_url)


async def test_ac1_generic_url_returns_clean_markdown():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text=HTML_PAGE, headers={"content-type": "text/html"}
        )

    transport, _ = transport_for(handler)
    result = await fetch_url("https://example.com/docs", transport=transport)
    assert "# Hello Docs" in result  # heading converted
    assert "[guide](https://example.com/guide)" in result  # link kept as Markdown
    assert "- one" in result and "- two" in result  # list items
    assert "secret" not in result  # <script> dropped
    assert "menu" not in result  # <nav> dropped
    assert "footer text" not in result  # <footer> dropped


async def test_ac1_truncates_with_explicit_marker():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="y" * 10_000, headers={"content-type": "text/plain"}
        )

    transport, _ = transport_for(handler)
    result = await fetch_url(
        "https://example.com/long", max_chars=200, transport=transport
    )
    assert result.startswith("y" * 200)
    assert result.endswith("...[truncated]")
    assert len(result) == 200 + len("\n...[truncated]")


# ------------------------------------------------------------------ AC2
# github.com/<owner>/<repo> -> README via raw.githubusercontent HEAD
# candidates; blob URLs -> raw file content.


async def test_ac2_github_repo_url_resolves_readme():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "raw.githubusercontent.com":
            if path == "/owner/repo/HEAD/README.md":
                return httpx.Response(200, text="# Owner Repo\n\nReadme body.")
            return httpx.Response(404)
        return httpx.Response(404)

    transport, requested = transport_for(handler)
    result = await fetch_url("https://github.com/owner/repo", transport=transport)
    assert "# Owner Repo" in result
    assert "Readme body." in result
    # HEAD candidates are tried in order, README.md first
    assert requested[0] == "https://raw.githubusercontent.com/owner/repo/HEAD/README.md"
    # the github.com HTML page itself is never fetched
    assert not any(r.startswith("https://github.com/") for r in requested)


async def test_ac2_github_blob_url_resolves_raw():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "raw.githubusercontent.com"
        assert request.url.path == "/owner/repo/main/src/app.py"
        return httpx.Response(200, text="print('hi')")

    transport, requested = transport_for(handler)
    result = await fetch_url(
        "https://github.com/owner/repo/blob/main/src/app.py", transport=transport
    )
    assert result == "print('hi')"
    assert requested == ["https://raw.githubusercontent.com/owner/repo/main/src/app.py"]


# ------------------------------------------------------------------ AC3
# 10 s timeout enforced on the client; robots.txt disallow -> clear error and
# NO page fetch.


async def test_ac3_ten_second_timeout_enforced():
    seen_timeouts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(dict(request.extensions["timeout"]))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    assert DEFAULT_TIMEOUT_S == 10.0
    transport, _ = transport_for(handler)
    await fetch_url("https://example.com/page", transport=transport)
    assert seen_timeouts, "no request reached the transport"
    for t in seen_timeouts:
        assert set(t) == {"connect", "read", "write", "pool"}
        assert all(v == 10.0 for v in t.values())


async def test_ac3_robots_disallow_yields_error_and_blocks_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /\n",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(200, text="must not be fetched")

    transport, requested = transport_for(handler)
    with pytest.raises(WebToolError, match="robots.txt"):
        await fetch_url("https://private.example.com/page", transport=transport)
    # only robots.txt was requested; the page itself was never fetched
    assert requested == ["https://private.example.com/robots.txt"]


# ------------------------------------------------------------------ AC4
# Every failure mode surfaces as WebToolError with a clear message.


async def test_ac4_unsupported_scheme():
    with pytest.raises(WebToolError, match="unsupported URL scheme"):
        await fetch_url("ftp://example.com/file")


async def test_ac4_http_status_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(500, text="boom")

    transport, _ = transport_for(handler)
    with pytest.raises(WebToolError, match="HTTP 500"):
        await fetch_url("https://example.com/boom", transport=transport)


async def test_ac4_timeout_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.TimeoutException("timed out")

    transport, _ = transport_for(handler)
    with pytest.raises(WebToolError, match="timed out after 10.0 s"):
        await fetch_url("https://slow.example.com/", transport=transport)


async def test_ac4_connection_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("name resolution failed")

    transport, _ = transport_for(handler)
    with pytest.raises(WebToolError, match="could not fetch"):
        await fetch_url("https://nowhere.example.com/", transport=transport)


async def test_ac4_unknown_extract_value():
    with pytest.raises(WebToolError, match="unknown extract mode"):
        await fetch_url("https://example.com/", extract="pdf")


# ------------------------------------------------------------------ AC5
# Mocked-HTTP coverage of the required scenarios, incl. non-HTML passthrough.


async def test_ac5_non_html_json_passthrough():
    payload = json.dumps({"hello": "world"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text=payload, headers={"content-type": "application/json"}
        )

    transport, _ = transport_for(handler)
    result = await fetch_url("https://api.example.com/data", transport=transport)
    assert result == payload  # untouched, no HTML extraction attempted


# ------------------------------------------------------------------ AC6
# fetch_url registered in a ToolRegistry whose execute never raises; chatbot
# answer() runs the tool loop; plain answers keep working.


def test_ac6_chatbot_registry_exposes_fetch_url(cfg):
    registry = build_chat_registry(cfg)
    assert "fetch_url" in registry.names()
    schema = next(s for s in registry.schemas() if s["function"]["name"] == "fetch_url")
    assert schema["function"]["parameters"]["required"] == ["url"]
    assert "url" in schema["function"]["parameters"]["properties"]


async def test_ac6_registry_execute_never_raises(cfg, monkeypatch):
    async def boom(url, max_chars=8000, extract="markdown", **kw):
        raise WebToolError("robots.txt at example.com disallows fetching this URL")

    monkeypatch.setattr(web_module, "fetch_url", boom)
    registry = build_chat_registry(cfg)
    out = await registry.execute("fetch_url", {"url": "https://example.com/x"})
    assert out.startswith("ERROR: WebToolError:")
    assert "robots.txt" in out
    # unknown tool also becomes an ERROR string, not an exception
    assert (await registry.execute("nope", {})).startswith("ERROR: unknown tool")


async def test_ac6_registry_forwards_arguments(cfg, monkeypatch):
    calls: list[dict] = []

    async def fake_fetch(url, max_chars=8000, extract="markdown", **kw):
        calls.append({"url": url, "max_chars": max_chars, "extract": extract})
        return "content"

    monkeypatch.setattr(web_module, "fetch_url", fake_fetch)
    registry = build_chat_registry(cfg)
    out = await registry.execute(
        "fetch_url", {"url": "https://github.com/o/r", "max_chars": 500}
    )
    assert out == "content"
    assert calls == [
        {"url": "https://github.com/o/r", "max_chars": 500, "extract": "markdown"}
    ]


def test_ac6_answer_runs_tool_loop(cfg, store, monkeypatch):
    calls: list[dict] = []

    async def fake_fetch(url, max_chars=8000, extract="markdown", **kw):
        calls.append(url)
        return "# Repo\n\nHello from the README"

    monkeypatch.setattr(web_module, "fetch_url", fake_fetch)
    responses = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("fetch_url", {"url": "https://github.com/o/r"})
                ],
            },
            {"role": "assistant", "content": "The repo says: Hello from the README"},
        ]
    )
    fake = FakeProvider({"qwen-fake": lambda body: next(responses)})
    res = ask(cfg, store, "What is https://github.com/o/r?", transport=fake.transport)
    assert calls == ["https://github.com/o/r"]
    assert res["answer"] == "The repo says: Hello from the README"
    # the model saw the tool schema and later the tool result
    assert any(t["function"]["name"] == "fetch_url" for t in fake.requests[0]["tools"])
    tool_msgs = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "Hello from the README" in tool_msgs[0]["content"]


def test_ac6_plain_answer_unchanged(cfg, store):
    fake = FakeProvider(
        {
            "qwen-fake": lambda body: {
                "role": "assistant",
                "content": "2 runs: 1 completed, 1 failed",
            }
        }
    )
    res = ask(cfg, store, "How many runs?", transport=fake.transport)
    assert res["answer"] == "2 runs: 1 completed, 1 failed"
    assert res["actions"] == {"tasks": [], "fixes": []}
    assert len(fake.requests) == 1  # no tool round-trips when no tool is called


async def test_ac6_bare_registry_handler_exception():
    """ToolRegistry.execute converts even async-handler exceptions to ERROR."""

    async def bad():
        raise ValueError("nope")

    r = ToolRegistry()
    r.register("bad", "Always fails", {"type": "object"}, bad)
    assert await r.execute("bad", {}) == "ERROR: ValueError: nope"
