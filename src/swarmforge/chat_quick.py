"""Pure helpers for the dashboard Chat page quick-actions.

Detects URLs and ``/repo``, ``/code``, ``/issues`` slash-commands in chat
input and expands them to structured prompts with a fetch_url tool hint.
"""

from __future__ import annotations

import re

URL_RE = re.compile(r"https?://[^\s<>\"'()\]]+", re.IGNORECASE)
SLASH_COMMANDS = ("/repo", "/code", "/issues")

_REPO_SUMMARY = (
    "Use the fetch_url tool on https://github.com/{slug} (it resolves to the "
    "repository README) and give a structured summary of the repository "
    "{slug}: what it does, its tech stack, how to install and run it, and "
    "the first issues a contributor could tackle."
)
_CODE_EXPLAIN = (
    'Explain the code relevant to "{query}": name the files involved, read '
    "them (use the fetch_url tool when the message contains URLs), and "
    "answer with file paths and short excerpts."
)
_ISSUES = (
    "Use the fetch_url tool on https://github.com/{slug}/issues and list "
    "the open issues of {slug}: number, title and a one-line summary for "
    "each."
)
URL_HINT_MARKER = "[chat quick-action] Detected link(s):"
_URL_HINT = (
    f"\n\n{URL_HINT_MARKER} {{urls}}. Use the fetch_url "
    "tool on each URL and answer from the fetched Markdown, citing the URL."
)


def detect_urls(text: str) -> list[str]:
    """Return unique URLs in *text*, order-preserving, trailing punctuation stripped."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:!?)")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_slash(text: str) -> tuple[str, str] | None:
    """Return (command, argument) when *text* starts with a slash-command."""
    stripped = text.lstrip()
    lowered = stripped.lower()
    for command in SLASH_COMMANDS:
        if not lowered.startswith(command):
            continue
        rest = stripped[len(command) :]
        if rest and not rest[0].isspace():
            continue
        argument = rest.strip()
        if argument:
            return (command, argument)
    return None


def _repo_slug(arg: str) -> str:
    """Extract an ``owner/repo`` slug from *arg* (a slug or a github.com URL)."""
    candidate = arg.strip()
    if "github.com/" in candidate.lower():
        candidate = candidate.lower().split("github.com/", 1)[1]
    elif "://" in candidate:
        return ""
    candidate = candidate.strip("/")
    candidate = candidate.removesuffix(".git")
    parts = [p for p in candidate.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def expand_prompt(text: str) -> str:
    """Expand slash-commands / URL-bearing text into a structured prompt."""
    parsed = parse_slash(text)
    if parsed is not None:
        command, arg = parsed
        if command == "/code":
            return _CODE_EXPLAIN.format(query=arg)
        slug = _repo_slug(arg)
        if command == "/repo":
            if slug:
                return _REPO_SUMMARY.format(slug=slug)
            return (
                f"Use the fetch_url tool on {arg} and give a structured "
                f"summary of the repository {arg}: what it does, its tech "
                "stack, how to install and run it, and the first issues a "
                "contributor could tackle."
            )
        if command == "/issues":
            if slug:
                return _ISSUES.format(slug=slug)
            return (
                f"Use the fetch_url tool on {arg} and list the open issues "
                f"of {arg}: number, title and a one-line summary for each."
            )
    urls = detect_urls(text)
    if urls and URL_HINT_MARKER not in text:
        return text + _URL_HINT.format(urls=", ".join(urls[:3]))
    return text


def hint_for(text: str) -> str | None:
    """Return a short UI hint when a quick-action applies, else None."""
    parsed = parse_slash(text)
    if parsed is not None:
        command = parsed[0]
        if command == "/code":
            return "💻 expanded to a code-explainer prompt"
        if command == "/issues":
            return "🐙 expanded to an issue-triage prompt"
        return "🔗 fetch_url hint added"
    if detect_urls(text):
        return "🔗 fetch_url hint added"
    return None
