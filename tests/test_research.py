"""web_search / fetch_url: agents research the internet from the host, never local addresses."""
import asyncio
import json
import socket

import httpx
import pytest

from swarmforge.agent import research
from swarmforge.agent.tools import ASYNC_IMPLS, TOOL_SCHEMAS, ToolContext, ToolError


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=tmp_path)


@pytest.fixture
def public_dns(monkeypatch):
    def fake(host, port, *a, **k):
        ip = {"example.com": "93.184.215.14", "evil.example": "127.0.0.1", "lan.example": "192.168.1.5"}.get(host, "93.184.215.14")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = k.get("transport") or httpx.MockTransport(handler)
        return real(*a, **k)
    monkeypatch.setattr(research.httpx, "AsyncClient", client)

    class Guarded(httpx.AsyncBaseTransport):  # the production guard in front of a mock network
        inner = httpx.MockTransport(handler)

        async def handle_async_request(self, request):
            research.public_url(str(request.url))
            return await self.inner.handle_async_request(request)
    monkeypatch.setattr(research, "TRANSPORT_FACTORY", Guarded)


def test_tools_are_registered_for_agents_and_pi():
    from swarmforge.agent.pi_runtime import SF_TOOLS
    for name in ("web_search", "fetch_url"):
        assert name in TOOL_SCHEMAS and name in ASYNC_IMPLS and name in SF_TOOLS


def test_fetch_url_reads_a_page_as_text_and_logs_it(ctx, public_dns, monkeypatch):
    page = "<html><head><style>x{}</style></head><body><h1>BOBLOV A22</h1><p>Battery: 10 hours</p><script>evil()</script></body></html>"
    mock_http(monkeypatch, lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    out = asyncio.run(research.fetch_page(ctx, "https://example.com/a22"))
    assert "BOBLOV A22" in out and "Battery: 10 hours" in out and "evil()" not in out and "x{}" not in out
    assert ctx.commands_run[-1]["command"].startswith("[research] fetch_url https://example.com/a22")


@pytest.mark.parametrize("url", ["http://localhost:8602/api/v1/projects", "http://127.0.0.1:8602/", "https://evil.example/",
                                 "https://lan.example/router", "file:///etc/passwd", "http://169.254.169.254/latest/meta-data"])
def test_fetch_url_refuses_local_and_private_addresses(ctx, public_dns, url):
    with pytest.raises(ToolError, match="public internet|http"):
        asyncio.run(research.fetch_page(ctx, url))


def test_redirects_to_private_addresses_are_refused(ctx, public_dns, monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(302, headers={"location": "https://evil.example/secret"}))
    with pytest.raises(ToolError, match="public internet"):
        asyncio.run(research.fetch_page(ctx, "https://example.com/go"))


def test_long_pages_come_in_parts(ctx, public_dns, monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(200, text="a" * 5000, headers={"content-type": "text/plain"}))
    first = asyncio.run(research.fetch_page(ctx, "https://example.com/big", max_chars=1000))
    assert "call again with start=1000" in first


def test_web_search_parses_duckduckgo_results(ctx, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    body = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa22&rut=x">'
            'BOBLOV <b>A22</b></a><a class="result__snippet" href="x">10 hours of 1080P</a>')
    mock_http(monkeypatch, lambda r: httpx.Response(200, text=body))
    results = json.loads(asyncio.run(research.web_search(ctx, "boblov a22 battery")).split("\n\nOpen")[0])
    assert results == [{"title": "BOBLOV A22", "url": "https://example.com/a22", "snippet": "10 hours of 1080P"}]


def test_web_search_uses_brave_when_a_key_is_set(ctx, public_dns, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    seen = {}

    def handler(r):
        seen["auth"] = r.headers.get("x-subscription-token")
        return httpx.Response(200, json={"web": {"results": [{"title": "T", "url": "https://x.example", "description": "<b>d</b>"}]}})
    mock_http(monkeypatch, handler)
    out = asyncio.run(research.web_search(ctx, "q"))
    assert seen["auth"] == "k" and '"snippet": "d"' in out


def test_research_is_bounded_per_session(ctx, monkeypatch):
    monkeypatch.setattr(research, "MAX_SEARCHES", 1)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    mock_http(monkeypatch, lambda r: httpx.Response(200, text=""))
    asyncio.run(research.web_search(ctx, "one"))
    with pytest.raises(ToolError, match="research limit"):
        asyncio.run(research.web_search(ctx, "two"))


def test_sf_cli_maps_search_and_fetch():
    import importlib.util
    from importlib import resources
    spec = importlib.util.spec_from_file_location("sf_cli", str(resources.files("swarmforge").joinpath("resources/sf.py")))
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    parse = next(getattr(sf, n) for n in ("parse", "parse_args", "translate") if hasattr(sf, n))
    assert parse(["search", "boblov", "a22", "--n", "3"]) == ("web_search", {"query": "boblov a22", "max_results": 3})
    assert parse(["fetch", "https://example.com", "--start", "100"]) == ("fetch_url", {"url": "https://example.com", "start": 100})
