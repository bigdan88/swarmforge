"""Attachment pipeline: sanitize, save, classify and render attached files.

Attachments are appended to the issue body as a markdown section so every
agent (planner, developers, reviewers, reports) sees them automatically.
"""
from __future__ import annotations

import re
from pathlib import Path

TEXT_EXTENSIONS = {".md", ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                   ".csv", ".tsv", ".py", ".js", ".ts", ".html", ".css", ".xml", ".sql", ".sh", ".rst"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
MAX_INLINE_BYTES = 8192  # per-file inline cap
MAX_FILES = 10

_BAD_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")


def sanitize_name(name: str) -> str:
    """Return a safe filename: basename only, restricted charset, capped length."""
    base = re.split(r"[/\\]", str(name))[-1]
    base = _BAD_CHARS.sub("", base)
    base = re.sub(r"\s+", " ", base).strip()
    base = base[:80].strip()
    return base or "file"


def save_attachments(dest_dir: Path, files: list[tuple[str, bytes]]) -> list[Path]:
    """Write (name, data) pairs into dest_dir, resolving name collisions.

    Returns the list of absolute paths of the saved files.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for name, data in files:
        safe = sanitize_name(name)
        candidate = dest_dir / safe
        stem, suffix = candidate.stem, candidate.suffix
        n = 0
        while candidate.exists():
            n += 1
            candidate = dest_dir / f"{stem}_{n}{suffix}"
        candidate.write_bytes(data)
        saved.append(candidate.resolve())
    return saved


def classify(path: Path) -> str:
    """Classify a file by extension: 'text', 'image' or 'other'."""
    ext = Path(path).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "other"


def render_section(paths: list[str] | list[Path]) -> str:
    """Render an '## Attached files' markdown section for the given paths.

    Small text files are inlined in fenced blocks; images, binaries, oversized
    and missing files are referenced by their saved path. Never raises.
    """
    if not paths:
        return ""
    lines = ["## Attached files", ""]
    for entry in paths:
        try:
            if not isinstance(entry, (str, Path)):
                continue
            path = Path(entry)
            name = path.name
            kind = classify(path)
            if not path.exists():
                lines.append(f"- {name} ({kind}, stored at {path}; open it to view)")
                continue
            size = path.stat().st_size
            if kind == "text" and size <= MAX_INLINE_BYTES:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001 - rendering must never raise
                    lines.append(f"- {name} (text, {size} bytes, stored at {path}; open it to view)")
                    continue
                lines.append(f"- {name} (text, {size} bytes)")
                lines.append("")
                lines.append(f"### {name}")
                lines.append("```")
                lines.append(content)
                lines.append("```")
            else:
                lines.append(f"- {name} ({kind}, {size} bytes, stored at {path}; open it to view)")
        except Exception:  # noqa: BLE001, S112 - rendering must never raise
            continue
    return "\n".join(lines).rstrip() + "\n"
