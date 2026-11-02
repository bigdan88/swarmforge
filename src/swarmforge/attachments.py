"""Attachment storage and classification helpers.

Deterministic, offline utilities for handling user-attached files:
name sanitization, extension-based classification, MIME guessing and
collision-safe saving to disk.
"""

from __future__ import annotations

import base64
import os
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
        ".html",
        ".css",
        ".xml",
        ".sql",
        ".sh",
        ".bat",
        ".ps1",
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
    }
)

IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

MAX_INLINE_BYTES = 8192
MAX_FILES = 10

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


def sanitize_name(name: str) -> str:
    """Return a safe filename from ``name``.

    Strips directory components (both ``/`` and ``\\`` separators), replaces
    unsafe characters with ``_`` and caps the length at 80 characters while
    preserving the extension. Never returns an empty string.
    """
    base = os.path.basename(name.replace("\\", "/"))
    cleaned = _SAFE_CHARS.sub("_", base).strip()
    if not cleaned:
        return "file"
    if len(cleaned) <= 80:
        return cleaned
    stem, dot, ext = cleaned.rpartition(".")
    if not dot or len(ext) > 10:
        stem, ext = cleaned, ""
    budget = 80 - (len(ext) + 1 if ext else 0)
    stem = stem[:budget]
    result = f"{stem}.{ext}" if ext else stem
    return result or "file"


def classify(name: str) -> str:
    """Classify a filename as ``"image"``, ``"text"`` or ``"other"``."""
    ext = pathlib.PurePosixPath(name).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "other"


def guess_mime(name: str) -> str:
    """Guess a MIME type from the file extension."""
    ext = pathlib.PurePosixPath(name).suffix.lower()
    return IMAGE_MIME.get(ext, "application/octet-stream")


def data_url(name: str, data: bytes) -> str:
    """Encode ``data`` as a data URL using the guessed MIME type."""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{guess_mime(name)};base64,{encoded}"


def save_attachments(
    target_dir: pathlib.Path, attachments: list[dict]
) -> list[pathlib.Path]:
    """Write attachments to ``target_dir`` and return absolute paths.

    Each attachment is a ``{"name": str, "data": bytes}`` dict. At most
    ``MAX_FILES`` entries are written (the rest are skipped). Name collisions
    are resolved by appending ``_1``, ``_2``… before the extension. Bad names
    are sanitized rather than raising.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    for attachment in attachments[:MAX_FILES]:
        name = sanitize_name(str(attachment.get("name", "")))
        data = attachment.get("data", b"")
        if not isinstance(data, bytes):
            data = str(data).encode("utf-8")
        candidate = target_dir / name
        counter = 0
        while candidate.exists():
            counter += 1
            stem, dot, ext = name.rpartition(".")
            if not dot or len(ext) > 10:
                stem, ext = name, ""
            candidate = target_dir / (
                f"{stem}_{counter}.{ext}" if ext else f"{stem}_{counter}"
            )
        candidate.write_bytes(data)
        written.append(candidate.resolve())
    return written
