"""Internet research for agents: web_search and fetch_url.

Both run in the SwarmForge process on the host (not in the sandbox), so agents can research
products, APIs and documentation without the sandbox itself getting network access. Every call
is logged in the run's evidence. Private and local addresses are refused, so an agent cannot use
these tools to reach SwarmForge itself, the owner's LAN or cloud metadata endpoints.

Search providers, in order: Brave Search (BRAVE_SEARCH_API_KEY), Tavily (TAVILY_API_KEY), then
DuckDuckGo's keyless HTML results.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import socket
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .tools import ToolContext, ToolError

UA = "Mozilla/5.0 (compatible; SwarmForge-research/1.0; +https://github.com/bigdan88/swarmforge)"
MAX_BYTES = 3_000_000
MAX_SEARCHES = 40   # per agent session: research is a means, not the task
MAX_FETCHES = 60


def _budget(ctx: ToolContext, kind: str, limit: int) -> None:
    used = ctx.research_counts
    used[kind] = used.get(kind, 0) + 1
    if used[kind] > limit:
        raise ToolError(f"research limit reached ({limit} {kind} calls in this session); work with what you found")


def _log(ctx: ToolContext, what: str, ok: bool) -> None:
    ctx.commands_run.append({"command": f"[research] {what}"[:500], "exit_code": 0 if ok else 1,
                             "timed_out": False, "duration_s": 0})


def public_url(url: str) -> str:
    """Return `url` if it is http(s) to a public internet address; raise ToolError otherwise."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ToolError("fetch_url needs a full http:// or https:// URL")
    host = p.hostname
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise ToolError("fetch_url only reaches public internet sites, not local or private addresses")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ToolError("fetch_url only reaches public internet sites, not local or private addresses")
        return url
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as e:
        raise ToolError(f"could not resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError("fetch_url only reaches public internet sites, not local or private addresses")
    return url


def html_to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    return re.sub(r"\n\s*\n+", "\n\n", body).strip()


async def _get(url: str, *, params=None, headers=None, timeout=20.0) -> httpx.Response:
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers={"User-Agent": UA, **(headers or {})}) as c:
        for _ in range(5):  # follow redirects ourselves so every hop is checked
            await asyncio.to_thread(public_url, url)
            r = await c.get(url, params=params)
            params = None
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = str(r.url.join(r.headers["location"]))
                continue
            return r
    raise ToolError("too many redirects")


async def _brave(query: str, n: int, key: str) -> list[dict]:
    r = await _get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": n},
                   headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": html_to_text(x.get("description", ""))}
            for x in (r.json().get("web") or {}).get("results", [])[:n]]


async def _tavily(query: str, n: int, key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.tavily.com/search", json={"api_key": key, "query": query, "max_results": n})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:400]}
            for x in r.json().get("results", [])[:n]]


async def _duckduckgo(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
    r.raise_for_status()
    out = []
    anchors = list(re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text))
    for i, m in enumerate(anchors):
        tail = r.text[m.end():anchors[i + 1].start() if i + 1 < len(anchors) else len(r.text)]
        snippet = re.search(r'(?s)class="result__snippet"[^>]*>(.*?)</a>', tail)
        href = html.unescape(m.group(1))
        if "uddg=" in href:  # DuckDuckGo wraps result links in a redirect
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        out.append({"title": html_to_text(m.group(2)), "url": href, "snippet": html_to_text(snippet.group(1) if snippet else "")})
        if len(out) >= n:
            break
    return out


async def web_search(ctx: ToolContext, query: str, max_results: int = 8) -> str:
    query = str(query or "").strip()
    if not query:
        raise ToolError("web_search needs a query")
    _budget(ctx, "web_search", MAX_SEARCHES)
    n = max(1, min(int(max_results or 8), 10))
    try:
        if os.environ.get("BRAVE_SEARCH_API_KEY"):
            results = await _brave(query, n, os.environ["BRAVE_SEARCH_API_KEY"])
        elif os.environ.get("TAVILY_API_KEY"):
            results = await _tavily(query, n, os.environ["TAVILY_API_KEY"])
        else:
            results = await _duckduckgo(query, n)
    except (httpx.HTTPError, ValueError) as e:
        _log(ctx, f"web_search {query!r}", False)
        raise ToolError(f"search failed: {type(e).__name__}: {e}") from None
    _log(ctx, f"web_search {query!r} -> {len(results)} results", True)
    if not results:
        return "No results. Try different or fewer words."
    return json.dumps(results, indent=1, ensure_ascii=False) + "\n\nOpen a result with fetch_url and cite its URL in your notes."


class _PublicOnly(httpx.AsyncBaseTransport):
    """Checks every request (including each redirect hop) against public_url before sending."""

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        await asyncio.to_thread(public_url, str(request.url))
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


async def fetch_page(ctx: ToolContext, url: str, max_chars: int = 20000, start: int = 0) -> str:
    """The agents' fetch_url tool: SwarmForge's shared web fetcher (Markdown extraction, robots.txt,
    GitHub README handling) behind a public-internet-only transport, with paging for long pages."""
    from ..tools.web import WebToolError
    from ..tools.web import fetch_url as shared_fetch
    _budget(ctx, "fetch_url", MAX_FETCHES)
    max_chars = max(1000, min(int(max_chars or 20000), 60000))
    start = max(0, int(start or 0))
    await asyncio.to_thread(public_url, str(url))
    try:
        text = await shared_fetch(str(url), max_chars=start + max_chars + 1, transport=TRANSPORT_FACTORY(), timeout_s=20)
    except WebToolError as e:
        _log(ctx, f"fetch_url {url}", False)
        raise ToolError(f"could not fetch {url}: {e}") from None
    _log(ctx, f"fetch_url {url}", True)
    truncated = text.endswith("...[truncated]")
    text = text.removesuffix("\n...[truncated]")
    part = text[start:start + max_chars]
    more = (f"\n\n[more text: call again with start={start + len(part)}]" if truncated or start + len(part) < len(text) else "")
    return f"URL: {url}\n\n{part}{more}"


TRANSPORT_FACTORY = _PublicOnly  # tests swap in a mock transport behind the same guard

def _fn(name, description, props, required):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


RESEARCH_SCHEMAS = {
    "web_search": _fn(
        "web_search",
        "Search the public internet (products, prices, datasheets, library docs, error messages, APIs). "
        "Returns titles, URLs and snippets. Use fetch_url to read a result; cite URLs for facts you rely on. "
        "Prefer current, primary sources (manufacturer pages, official docs) over guesses from memory.",
        {"query": {"type": "string"}, "max_results": {"type": "integer", "description": "1-10, default 8"}}, ["query"]),
    "fetch_url": _fn(
        "fetch_url",
        "Read a public web page or text file as plain text (HTML is converted). Long pages come in parts: "
        "pass start= to continue. Public internet only.",
        {"url": {"type": "string"}, "max_chars": {"type": "integer"}, "start": {"type": "integer"}}, ["url"]),
}
RESEARCH_IMPLS = {"web_search": web_search, "fetch_url": fetch_page}
RESEARCH_TOOLS = list(RESEARCH_SCHEMAS)


def research_tools(cfg) -> list[str]:
    """web_search/fetch_url for every role unless sandbox.research is off."""
    return RESEARCH_TOOLS if getattr(cfg.sandbox, "research", False) else []
