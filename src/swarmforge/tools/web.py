"""Web-fetch tool: fetch a URL and return clean Markdown.

Uses httpx with a 10 s timeout, respects robots.txt, and special-cases
GitHub repository URLs so they resolve to their README content.
Optional extraction backends (trafilatura, readability) are used when
installed; a stdlib-only ``html_to_markdown`` fallback keeps the module
working without them.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import ClassVar
from urllib import robotparser
from urllib.parse import urlparse

import httpx

USER_AGENT = "SwarmForge-Chatbot/0.1"
DEFAULT_TIMEOUT_S = 10.0

README_NAMES = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "Readme.md",
    "readme.md",
]


class WebToolError(Exception):
    """Raised when fetching or extracting a URL fails."""


def github_readme_candidates(owner: str, repo: str) -> list[str]:
    """Return raw.githubusercontent README URLs for a repo, best guess first."""
    return [
        f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}"
        for name in README_NAMES
    ]


def github_blob_to_raw(url: str) -> str | None:
    """Map a github.com blob URL to its raw.githubusercontent equivalent."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc != "github.com" or len(parts) < 5 or parts[2] != "blob":
        return None
    owner, repo, _blob, ref = parts[0], parts[1], parts[2], parts[3]
    path = "/".join(parts[4:])
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


class _MarkdownParser(HTMLParser):
    """Very small HTML -> Markdown converter (stdlib only)."""

    _SKIP: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "nav", "footer", "noscript", "svg", "head"}
    )

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_link = False
        self._in_li = False
        self._in_heading = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._in_link = True
            self._link_text = []
            self._href = dict(attrs).get("href")
        elif tag == "li":
            self._in_li = True
            self.out.append("\n- ")
        elif tag in (
            "p",
            "div",
            "section",
            "article",
            "blockquote",
            "table",
            "tr",
            "br",
            "ul",
            "ol",
        ):
            self.out.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = int(tag[1])
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._in_link = False
            text = "".join(self._link_text).strip()
            href = self._href or ""
            if text and href:
                self.out.append(f"[{text}]({href})")
            elif text:
                self.out.append(text)
            self._href = None
        elif tag == "li":
            self._in_li = False
            self.out.append("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n")
            self._in_heading = 0
        elif tag in (
            "p",
            "div",
            "section",
            "article",
            "blockquote",
            "table",
            "tr",
            "ul",
            "ol",
        ):
            self.out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_link:
            self._link_text.append(data)
            return
        if self._in_heading:
            self.out.append("#" * self._in_heading + " ")
            self._in_heading = 0
        self.out.append(data)


def html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown using only the standard library."""
    parser = _MarkdownParser()
    parser.feed(html)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_tags(html: str) -> str:
    class _TextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self._skip = 0

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag in _MarkdownParser._SKIP:
                self._skip += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in _MarkdownParser._SKIP:
                self._skip = max(0, self._skip - 1)

        def handle_data(self, data: str) -> None:
            if not self._skip:
                self.parts.append(data)

    p = _TextParser()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise WebToolError("empty URL")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebToolError(f"unsupported URL scheme {parsed.scheme!r} in {url!r}")
    return url


def _github_repo_readme(url: str) -> str | None:
    """Return the repo URL if `url` is a bare github.com/<owner>/<repo> URL."""
    parsed = urlparse(url)
    if parsed.netloc != "github.com":
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2:
        return None
    owner, repo = parts
    repo = repo.removesuffix(".git")
    return f"{owner}/{repo}"


async def _robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url)
    except httpx.HTTPError:
        return True  # network problems fetching robots.txt are treated as 'allow'
    if resp.status_code in (404, 403):
        return True
    if resp.status_code >= 400:
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp.can_fetch(USER_AGENT, url)


async def fetch_url(
    url: str,
    max_chars: int = 8000,
    extract: str = "markdown",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Fetch `url` and return extracted content as a string.

    Raises WebToolError with a clear, user-readable message on failure.
    """
    if max_chars < 1:
        raise WebToolError(f"max_chars must be >= 1, got {max_chars}")
    if extract not in ("markdown", "text", "html"):
        raise WebToolError(
            f"unknown extract mode {extract!r} (expected markdown, text or html)"
        )

    url = _normalize_url(url)

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_s),
        headers=headers,
        transport=transport,
    ) as client:
        # GitHub special-casing.
        blob_raw = github_blob_to_raw(url)
        if blob_raw:
            content, content_type = await _get(client, blob_raw, timeout_s)
            return _finish(_decode(content), content_type, extract, max_chars, blob_raw)

        repo = _github_repo_readme(url)
        if repo:
            owner, name = repo.split("/", 1)
            for candidate in github_readme_candidates(owner, name):
                try:
                    content, content_type = await _get(client, candidate, timeout_s)
                except WebToolError:
                    continue
                return _finish(
                    _decode(content), content_type, extract, max_chars, candidate
                )
            # No README found: fall back to fetching the page itself.
            content, content_type = await _get(client, url, timeout_s)
            return _finish(_decode(content), content_type, extract, max_chars, url)

        if not await _robots_allows(client, url):
            host = urlparse(url).netloc
            raise WebToolError(f"robots.txt at {host} disallows fetching this URL")

        content, content_type = await _get(client, url, timeout_s)
        return _finish(_decode(content), content_type, extract, max_chars, url)


def _decode(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


async def _get(
    client: httpx.AsyncClient, url: str, timeout_s: float
) -> tuple[bytes, str]:
    try:
        resp = await client.get(url)
    except httpx.TimeoutException as exc:
        raise WebToolError(f"request to {url} timed out after {timeout_s} s") from exc
    except httpx.HTTPError as exc:
        raise WebToolError(f"could not fetch {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise WebToolError(f"HTTP {resp.status_code}: {resp.reason_phrase} for {url}")
    return resp.content, resp.headers.get("content-type", "")


def _finish(
    body: str, content_type: str, extract: str, max_chars: int, url: str
) -> str:
    if "html" in content_type:
        if extract == "html":
            result = body
        elif extract == "text":
            result = _strip_tags(body)
        else:  # markdown
            result = _extract_markdown(body)
    else:
        # json / plain text / markdown / anything else: return as-is
        result = body
    if len(result) > max_chars:
        result = result[:max_chars] + "\n...[truncated]"
    return result


def _extract_markdown(html: str) -> str:
    try:
        import trafilatura  # type: ignore[import-not-found]

        extracted = trafilatura.extract(
            html, output_format="markdown", include_links=True
        )
        if extracted and extracted.strip():
            return extracted.strip()
    except ImportError:
        pass
    except Exception:  # noqa: BLE001, S110 - fall back if extraction fails
        pass
    try:
        from lxml import html as lxml_html  # type: ignore[import-not-found]
        from readability import Document  # type: ignore[import-not-found]

        doc = Document(lxml_html.fromstring(html).getroottree().getroot()).summary()
        if doc and doc.strip():
            return html_to_markdown(doc)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001, S110
        pass
    return html_to_markdown(html)
