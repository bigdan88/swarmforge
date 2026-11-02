"""Unit tests for the web-fetch tool (all HTTP is mocked)."""

import httpx
import pytest

from swarmforge.tools.web import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    WebToolError,
    fetch_url,
    github_blob_to_raw,
    github_readme_candidates,
    html_to_markdown,
)

HTML_PAGE = """<html><head><title>T</title><script>var x = 'secret';</script></head>
<body><nav>menu</nav>
<h1>Hello Docs</h1>
<p>Check the <a href="https://example.com/guide">guide</a> for details.</p>
<ul><li>one</li><li>two</li></ul>
<footer>footer text</footer></body></html>"""


def make_transport(handler):
    return httpx.MockTransport(handler)


def record(handler):
    """Wrap a handler so requested URLs are recorded."""
    requested = []

    def wrapped(request):
        requested.append(str(request.url))
        return handler(request)

    return wrapped, requested


async def test_generic_html_page_to_markdown():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.headers["user-agent"] == USER_AGENT
        return httpx.Response(
            200, text=HTML_PAGE, headers={"content-type": "text/html"}
        )

    result = await fetch_url(
        "https://example.com/docs", transport=make_transport(handler)
    )
    assert "# Hello Docs" in result
    assert "[guide](https://example.com/guide)" in result
    assert "- one" in result
    assert "secret" not in result  # script dropped
    assert "footer text" not in result  # footer dropped
    assert "menu" not in result  # nav dropped


async def test_github_repo_url_resolves_readme():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/owner/repo/HEAD/README.md":
            return httpx.Response(200, text="# Owner Repo\n\nThis is the readme.")
        return httpx.Response(404)

    wrapped, requested = record(handler)
    result = await fetch_url(
        "https://github.com/owner/repo", transport=make_transport(wrapped)
    )
    assert "# Owner Repo" in result
    assert "https://raw.githubusercontent.com/owner/repo/HEAD/README.md" in requested
    # first candidate tried first
    assert requested[0] == "https://raw.githubusercontent.com/owner/repo/HEAD/README.md"


async def test_github_repo_url_tries_candidates_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/owner/repo/HEAD/README.rst":
            return httpx.Response(200, text="README body")
        return httpx.Response(404)

    wrapped, requested = record(handler)
    result = await fetch_url(
        "https://github.com/owner/repo/", transport=make_transport(wrapped)
    )
    assert result == "README body"
    assert requested[0].endswith("/README.md")
    assert requested[1].endswith("/README.rst")


async def test_github_blob_url_fetches_raw():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "raw.githubusercontent.com"
        assert request.url.path == "/owner/repo/main/src/app.py"
        return httpx.Response(200, text="print('hi')")

    wrapped, requested = record(handler)
    result = await fetch_url(
        "https://github.com/owner/repo/blob/main/src/app.py",
        transport=make_transport(wrapped),
    )
    assert result == "print('hi')"
    assert "https://github.com" not in "".join(requested)


async def test_robots_txt_disallow():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /\n",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(200, text="should not be fetched")

    with pytest.raises(WebToolError, match="robots.txt"):
        await fetch_url(
            "https://private.example.com/page", transport=make_transport(handler)
        )


async def test_timeout_raises_webtoolerror():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.TimeoutException("timed out")

    with pytest.raises(WebToolError, match="timed out"):
        await fetch_url("https://slow.example.com/", transport=make_transport(handler))


async def test_truncation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="x" * 500, headers={"content-type": "text/plain"}
        )

    result = await fetch_url(
        "https://example.com/big", max_chars=100, transport=make_transport(handler)
    )
    assert len(result) <= 100 + len("\n...[truncated]")
    assert result.endswith("...[truncated]")


async def test_unsupported_scheme():
    with pytest.raises(WebToolError, match="unsupported URL scheme"):
        await fetch_url("ftp://example.com/file")


async def test_plain_text_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="just plain text", headers={"content-type": "text/plain"}
        )

    result = await fetch_url(
        "https://example.com/notes.txt", transport=make_transport(handler)
    )
    assert result == "just plain text"


async def test_extract_html_returns_raw():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text=HTML_PAGE, headers={"content-type": "text/html"}
        )

    result = await fetch_url(
        "https://example.com/raw", extract="html", transport=make_transport(handler)
    )
    assert "<h1>Hello Docs</h1>" in result


async def test_http_error_mapped():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404, text="not found")

    with pytest.raises(WebToolError, match="HTTP 404"):
        await fetch_url(
            "https://example.com/missing", transport=make_transport(handler)
        )


async def test_missing_scheme_gets_https():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    result = await fetch_url("example.com/x", transport=make_transport(handler))
    assert result == "ok"


async def test_bad_max_chars_and_extract_mode():
    with pytest.raises(WebToolError):
        await fetch_url("https://example.com/", max_chars=0)
    with pytest.raises(WebToolError, match="unknown extract mode"):
        await fetch_url("https://example.com/", extract="bogus")


def test_github_helpers():
    cands = github_readme_candidates("o", "r")
    assert cands[0] == "https://raw.githubusercontent.com/o/r/HEAD/README.md"
    assert github_blob_to_raw("https://github.com/o/r/blob/main/a/b.py") == (
        "https://raw.githubusercontent.com/o/r/main/a/b.py"
    )
    assert github_blob_to_raw("https://github.com/o/r") is None


def test_html_to_markdown_fallback():
    md = html_to_markdown(HTML_PAGE)
    assert "# Hello Docs" in md
    assert "[guide](https://example.com/guide)" in md
    assert "secret" not in md


def test_constants():
    assert DEFAULT_TIMEOUT_S == 10.0
    assert USER_AGENT == "SwarmForge-Chatbot/0.1"
