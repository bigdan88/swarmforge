"""Deterministic attachment pipeline helpers.

Used by the dashboard Chat page and the chatbot to save uploaded files with
collision-safe names, classify them, and render them as markdown.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

TEXT_EXTENSIONS: set[str] = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".log",
    ".sql",
    ".sh",
    ".ps1",
    ".html",
    ".css",
    ".go",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".rb",
}

IMAGE_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".tiff",
}

MAX_INLINE_BYTES = 8192
MAX_FILES = 10

_MAX_STEM_CHARS = 80


def sanitize_name(name: str) -> str:
    """Return a safe filename for *name*.

    Takes the basename (stripping POSIX and Windows separators), keeps only
    ``[A-Za-z0-9._-]`` characters, collapses runs of underscores, and caps the
    stem at 80 characters while preserving the extension. Never returns ''.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        ch if (ch.isalnum() and ch.isascii()) or ch in "._-" else "_" for ch in base
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")

    suffix = Path(cleaned).suffix
    stem = cleaned[: len(cleaned) - len(suffix)] if suffix else cleaned
    stem = stem[:_MAX_STEM_CHARS].rstrip("._-") or "file"
    result = stem + suffix
    return result or "file"


def save_attachments(files, dest_dir: Path) -> list[Path]:
    """Save uploaded files into *dest_dir* with collision-safe names.

    *files* is any iterable of objects exposing ``.name: str`` and
    ``.getvalue() -> bytes`` (streamlit UploadedFile duck-type). At most
    MAX_FILES files are saved; individual write failures are skipped.
    Returns the list of absolute saved paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for f in list(files)[:MAX_FILES]:
        try:
            target = dest_dir / sanitize_name(f.name)
            if target.exists():
                suffix = target.suffix
                stem = target.stem
                n = 1
                while target.exists():
                    target = dest_dir / f"{stem}_{n}{suffix}"
                    n += 1
            target.write_bytes(f.getvalue())
            saved.append(target.resolve())
        except OSError:
            continue
    return saved


def classify(path: Path) -> str:
    """Classify a path as 'image', 'text' or 'other' by extension."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return "other"


def image_data_url(path: Path) -> str:
    """Return a data URL for an image file."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def inline_text(path: Path) -> str | None:
    """Return decoded UTF-8 text for small text files, else None."""
    if classify(path) != "text":
        return None
    try:
        if not path.exists() or path.stat().st_size > MAX_INLINE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def render_section(paths: list[Path]) -> str:
    """Render a markdown section describing the given attachment paths.

    Text files that can be inlined get a fenced code block; images, other
    files, oversized or missing files are referenced as ``- name (kind)``.
    Never raises; returns '' for an empty list.
    """
    if not paths:
        return ""
    lines = ["## Attached files"]
    for path in paths:
        kind = classify(path)
        text = inline_text(path) if kind == "text" else None
        if text is not None:
            lines.append(f"**{path.name}**")
            lines.append("```")
            lines.append(text)
            lines.append("```")
        else:
            lines.append(f"- {path.name} ({kind})")
    return "\n".join(lines)
