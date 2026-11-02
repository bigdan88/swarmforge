"""Attachment storage, classification and rendering helpers.

Deterministic, offline utilities for handling user-attached files: name
sanitization, extension-based classification, MIME guessing, collision-safe
saving to disk, and markdown/data-URL rendering.

Two callers share this module:
- the dashboard Chat page / launcher uploader, which passes streamlit-style
  upload objects (``.name`` + ``.getvalue()``) and ``Path`` objects around;
- the chatbot, which passes ``{"name": str, "data": bytes}`` dicts.
``save_attachments`` and ``classify`` accept both shapes.
"""

from __future__ import annotations

import base64
import mimetypes
import pathlib
import re

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
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
        ".tsv",
        ".log",
        ".sql",
        ".sh",
        ".bat",
        ".ps1",
        ".html",
        ".css",
        ".xml",
        ".go",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".rb",
    }
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".svg",
        ".tiff",
    }
)

IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".tiff": "image/tiff",
}

MAX_INLINE_BYTES = 8192
MAX_FILES = 10

_MAX_STEM_CHARS = 80
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_name(name: str) -> str:
    """Return a safe filename for *name*.

    Takes the basename (stripping POSIX and Windows separators), keeps only
    ``[A-Za-z0-9._-]`` characters (spaces and unicode become ``_``), collapses
    runs of underscores, strips trailing separator characters from the stem,
    and caps the stem at 80 characters while preserving the extension. Never
    returns an empty string.
    """
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_CHARS.sub("_", base)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")

    suffix = pathlib.PurePosixPath(cleaned).suffix
    stem = cleaned[: len(cleaned) - len(suffix)] if suffix else cleaned
    stem = stem[:_MAX_STEM_CHARS].rstrip("._-") or "file"
    result = stem + suffix
    return result or "file"


def classify(name) -> str:
    """Classify a filename (``str`` or ``Path``) as 'image', 'text' or 'other'."""
    ext = pathlib.PurePosixPath(str(name)).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "other"


def guess_mime(name) -> str:
    """Guess a MIME type from the file name's extension."""
    ext = pathlib.PurePosixPath(str(name)).suffix.lower()
    return IMAGE_MIME.get(ext, "application/octet-stream")


def data_url(name, data: bytes) -> str:
    """Encode ``data`` as a data URL using the guessed MIME type."""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{guess_mime(name)};base64,{encoded}"


def image_data_url(path) -> str:
    """Return a data URL for an image file on disk."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(pathlib.Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def inline_text(path) -> str | None:
    """Return decoded UTF-8 text for small text files, else None."""
    if classify(path) != "text":
        return None
    try:
        p = pathlib.Path(path)
        if not p.exists() or p.stat().st_size > MAX_INLINE_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def render_section(paths) -> str:
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
            lines.append(f"**{pathlib.Path(path).name}**")
            lines.append("```")
            lines.append(text)
            lines.append("```")
        else:
            lines.append(f"- {pathlib.Path(path).name} ({kind})")
    return "\n".join(lines)


def _attachment_items(files) -> list[tuple[str, bytes]]:
    """Normalize an attachment iterable into (name, data) pairs.

    Accepts streamlit-style upload objects (``.name`` + ``.getvalue()``) and
    ``{"name": str, "data": bytes}`` dicts. At most MAX_FILES entries are kept.
    """
    items: list[tuple[str, bytes]] = []
    for f in list(files)[:MAX_FILES]:
        if isinstance(f, dict):
            name = str(f.get("name") or "")
            data = f.get("data", b"")
        else:
            name = str(getattr(f, "name", "") or "")
            data = f.getvalue()
        if not isinstance(data, bytes):
            data = str(data).encode("utf-8")
        items.append((name, data))
    return items


def save_attachments(files_or_target_dir, target_dir_or_files=None) -> list[pathlib.Path]:
    """Save uploaded files with collision-safe names; return absolute paths.

    Two call shapes are supported (the argument order differs, so the list
    argument is detected by type):

    - ``save_attachments(files, dest_dir)`` — dashboard/streamlit style, where
      *files* are upload objects exposing ``.name`` and ``.getvalue()``;
    - ``save_attachments(target_dir, attachments)`` — chatbot style, where
      *attachments* are ``{"name": str, "data": bytes}`` dicts.

    At most MAX_FILES files are saved; name collisions get ``_1``, ``_2`` …
    suffixes before the extension; bad names are sanitized; individual write
    failures are skipped.
    """
    if isinstance(files_or_target_dir, (list, tuple)):
        files, dest = files_or_target_dir, target_dir_or_files
    else:
        dest, files = files_or_target_dir, target_dir_or_files
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[pathlib.Path] = []
    for name, data in _attachment_items(files):
        try:
            target = dest / sanitize_name(name)
            suffix = target.suffix
            stem = target.stem
            n = 1
            while target.exists():
                target = dest / f"{stem}_{n}{suffix}"
                n += 1
            target.write_bytes(data)
            saved.append(target.resolve())
        except OSError:
            continue
    return saved
