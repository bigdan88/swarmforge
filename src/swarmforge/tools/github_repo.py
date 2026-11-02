"""GitHub repository analysis tools: tree listings, file contents, code search, issues.

All four functions talk to the GitHub REST API via httpx, reuse the
existing GitHub token (``GITHUB_TOKEN`` / ``gh auth token``) and fall
back to the anonymous public API when no token is available or when a
token is rejected (401/403). Results are plain text, one entry per line,
so a chat model can read them directly.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote

import httpx

from ..config import GitHubConfig
from ..github import _headers, get_token

MAX_FILE_BYTES = 50_000
TRUNCATION_MARKER = "\n...[truncated at 50 KB]..."

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ISSUE_STATES = {"open", "closed", "all"}


class GitHubRepoError(Exception):
    """Raised when a GitHub repository request fails."""


def _validate_name(kind: str, value: str) -> str:
    value = (value or "").strip()
    if not value or not _NAME_RE.match(value) or value in (".", ".."):
        raise GitHubRepoError(f"invalid {kind}: {value!r}")
    return value


def _validate_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if "\\" in path or any(ord(c) < 32 for c in path):
        raise GitHubRepoError(f"invalid path: {path!r}")
    for segment in path.split("/"):
        if segment in (".", ".."):
            raise GitHubRepoError(f"invalid path: {path!r}")
    return path


def _client(
    token: str | None, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.github.com",
        headers=_headers(token),
        timeout=30,
        transport=transport,
    )


async def _request(
    client: httpx.AsyncClient,
    token: str | None,
    url: str,
    params: dict | None = None,
) -> httpx.Response:
    """GET `url`, retrying anonymously on 401/403 when a token was used."""
    try:
        r = await client.get(url, params=params)
        if r.status_code in (401, 403) and token:
            req = client.build_request("GET", url, params=params)
            req.headers.pop("Authorization", None)
            r = await client.send(req)
        return r
    except httpx.HTTPError as exc:
        raise GitHubRepoError(f"GitHub request failed: {exc}") from exc


def _check(r: httpx.Response, what: str, token: str | None) -> None:
    if r.status_code == 404:
        raise GitHubRepoError(f"not found: {what}")
    if r.status_code >= 400:
        hint = (
            "" if token else " (no GitHub token set; private repos need GITHUB_TOKEN)"
        )
        raise GitHubRepoError(
            f"GitHub request failed: HTTP {r.status_code} {r.reason_phrase}{hint}"
        )


async def repo_tree(
    owner: str,
    repo: str,
    path: str = "",
    ref: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """List the contents of a directory (or a single file entry) in a GitHub repo.

    Returns a header line followed by one `- name [type] size bytes`
    line per entry. Raises GitHubRepoError on invalid input or API errors.
    """
    owner = _validate_name("owner", owner)
    repo = _validate_name("repo", repo)
    path = _validate_path(path)
    quoted = quote(path, safe="/")
    url = (
        f"/repos/{owner}/{repo}/contents/{quoted}"
        if path
        else f"/repos/{owner}/{repo}/contents"
    )
    params = {"ref": ref} if ref else None

    token = get_token(GitHubConfig())
    async with _client(token, transport) as client:
        r = await _request(client, token, url, params)
        _check(r, f"{owner}/{repo}/{path}", token)
        d = r.json()

    if isinstance(d, dict):
        d = [d]
    lines = [f"Contents of {owner}/{repo}/{path}@{ref or 'HEAD'}:"]
    for entry in d:
        lines.append(
            f"- {entry.get('name')} [{entry.get('type')}] {entry.get('size', 0)} bytes"
        )
    return "\n".join(lines)


async def repo_file(
    owner: str,
    repo: str,
    path: str,
    ref: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Return the decoded content of a file in a GitHub repo.

    The content is capped at 50,000 bytes of decoded output: larger
    files are truncated to exactly 50,000 bytes and end with the line
    ``...[truncated at 50 KB]...``. Raises GitHubRepoError when the
    path is a directory or the file is too large for the contents API.
    """
    owner = _validate_name("owner", owner)
    repo = _validate_name("repo", repo)
    path = _validate_path(path)
    if not path:
        raise GitHubRepoError("path must not be empty")
    url = f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}"
    params = {"ref": ref} if ref else None

    token = get_token(GitHubConfig())
    async with _client(token, transport) as client:
        r = await _request(client, token, url, params)
        _check(r, f"{owner}/{repo}/{path}", token)
        d = r.json()

    if isinstance(d, list):
        raise GitHubRepoError(f"'{path}' is a directory; use repo_tree")
    content = d.get("content")
    if not content:
        raise GitHubRepoError(
            f"'{path}' has no inline content (file too large for the contents API); "
            f"fetch it via its download_url / raw content instead"
        )
    raw = base64.b64decode(content)
    if len(raw) > MAX_FILE_BYTES:
        raw = raw[:MAX_FILE_BYTES]
        return raw.decode("utf-8", errors="replace") + TRUNCATION_MARKER
    return raw.decode("utf-8", errors="replace")


async def repo_search(
    owner: str,
    repo: str,
    query: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Search for code in a GitHub repo; returns one `- path` line per match."""
    owner = _validate_name("owner", owner)
    repo = _validate_name("repo", repo)
    query = (query or "").strip()
    if not query:
        raise GitHubRepoError("query must not be empty")

    token = get_token(GitHubConfig())
    async with _client(token, transport) as client:
        r = await _request(
            client, token, "/search/code", params={"q": f"{query} repo:{owner}/{repo}"}
        )
    if r.status_code in (401, 403) and not token:
        raise GitHubRepoError("code search requires a GitHub token (GITHUB_TOKEN)")
    _check(r, f"{owner}/{repo}", token)
    items = r.json().get("items", [])
    if not items:
        return f"No code matches for '{query}' in {owner}/{repo}"
    lines = []
    for item in items:
        line = f"- {item.get('path')}"
        fragments = item.get("text_matches") or []
        if fragments:
            frags = " | ".join(
                " ".join(f.get("fragment", "").split())
                for f in fragments
                if f.get("fragment")
            )
            if frags:
                line += f" :: {frags}"
        lines.append(line)
    return "\n".join(lines)


async def repo_issues(
    owner: str,
    repo: str,
    state: str = "open",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """List issue summaries for a GitHub repo, skipping pull requests."""
    owner = _validate_name("owner", owner)
    repo = _validate_name("repo", repo)
    if state not in _ISSUE_STATES:
        raise GitHubRepoError(
            f"invalid state {state!r} (expected one of: open, closed, all)"
        )

    token = get_token(GitHubConfig())
    async with _client(token, transport) as client:
        r = await _request(
            client,
            token,
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": 30},
        )
        _check(r, f"{owner}/{repo}", token)
        issues = r.json()

    lines = []
    for d in issues:
        if "pull_request" in d:
            continue
        labels = ", ".join(l["name"] for l in d.get("labels", []))
        label_part = f" (labels: {labels})" if labels else ""
        lines.append(
            f"#{d['number']} [{d.get('state')}] {d.get('title')}{label_part} <{d.get('html_url')}>"
        )
    if not lines:
        return f"No {state} issues in {owner}/{repo}"
    return "\n".join(lines)
