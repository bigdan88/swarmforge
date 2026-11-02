"""Pure, stdlib-only line-range edit engine for the Pi harness.

Provides :func:`apply_patch` and :func:`edit_lines` so agents can make
targeted edits to a file instead of rewriting it wholesale.  Every hunk is
validated (including optional exact-content anchors) against the *original*
file before anything is written; on any problem the whole edit is rejected
with a single :class:`EditConflict` report and the file is left untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["EditConflict", "apply_patch", "edit_lines", "parse_unified_diff"]


class EditConflict(ValueError):
    """Raised when an edit is rejected; str(e) is a multi-line, helpful report."""


def _read_file(path: str | Path) -> tuple[str, list[str]]:
    p = Path(path)
    if not p.exists():
        raise EditConflict(f"no such file: {p}")
    if p.is_dir():
        raise EditConflict(f"not a file (is a directory): {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EditConflict(f"cannot read {p}: {exc}") from exc
    return text, text.split("\n")


def _split_text(text: str) -> list[str]:
    """Split replacement text into lines, ignoring a single trailing newline."""
    if text == "":
        return []
    text = text.removesuffix("\n")
    return text.split("\n")


def _normalize_hunk(raw: Any, index: int) -> dict[str, Any]:
    if isinstance(raw, dict):
        hunk = dict(raw)
    elif isinstance(raw, (tuple, list)) and len(raw) == 3:
        start, end, text = raw
        hunk = {"start": start, "end": end, "text": text}
    else:
        raise EditConflict(
            f"hunk #{index + 1}: expected a dict or (start, end, text) tuple"
        )
    try:
        start = int(hunk["start"])
        end = int(hunk["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EditConflict(
            f"hunk #{index + 1}: missing or invalid 'start'/'end' ({exc})"
        ) from exc
    text = hunk.get("text", "")
    if not isinstance(text, str):
        raise EditConflict(f"hunk #{index + 1}: 'text' must be a string")
    old = hunk.get("old")
    if old is not None:
        if not isinstance(old, (list, tuple)) or not all(
            isinstance(line, str) for line in old
        ):
            raise EditConflict(f"hunk #{index + 1}: 'old' must be a list of strings")
        old = list(old)
    return {"start": start, "end": end, "text": text, "old": old}


def parse_unified_diff(text: str) -> list[dict[str, Any]]:
    """Parse a unified diff for a single file into hunk dicts.

    Context lines (' ') and removed lines ('-') form the anchor ``old``;
    added lines ('+') form the replacement ``text``.
    """
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    old_lines: list[str] = []
    new_lines: list[str] = []
    old_start = 0
    for line in text.split("\n"):
        if line.startswith("@@"):
            if current is not None:
                hunks.append(_finish_hunk(current, old_lines, new_lines))
            header = line[2:]
            # strip optional trailing section heading
            header = header.split("@@")[0].strip()
            try:
                spec = header.split()[0]
                old_start = int(spec.split(",")[0].lstrip("-"))
            except (IndexError, ValueError) as exc:
                raise EditConflict(f"malformed hunk header: {line!r}") from exc
            current = {"start": old_start}
            old_lines = []
            new_lines = []
        elif current is not None:
            if line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            elif line == "":
                # trailing empty line after the last "\ No newline" style content
                if old_lines or new_lines:
                    hunks.append(_finish_hunk(current, old_lines, new_lines))
                    current = None
            elif line.startswith("\\"):
                # "\ No newline at end of file" marker: ignore
                continue
            elif line.startswith(("---", "+++")):
                continue
            else:
                raise EditConflict(f"unexpected line in diff: {line!r}")
    if current is not None:
        hunks.append(_finish_hunk(current, old_lines, new_lines))
    if not hunks:
        raise EditConflict("no hunks found in diff")
    return hunks


def _finish_hunk(
    header: dict[str, Any], old_lines: list[str], new_lines: list[str]
) -> dict[str, Any]:
    start = header["start"]
    end = start + len(old_lines) - 1
    return {
        "start": start,
        "end": end,
        "old": old_lines,
        "text": "\n".join(new_lines),
    }


def _check_diff_header_file(diff_text: str, path: str | Path) -> None:
    """If the diff's ---/+++ header names a file, it must match `path`."""
    target = Path(path).name
    for line in diff_text.split("\n"):
        if line.startswith(("--- ", "+++ ")):
            name = line[4:].split("\t")[0].strip()
            if name.startswith(("b/", "a/")):
                name = name[2:]
            if (
                name
                and name not in ("/dev/null",)
                and Path(name).name != target
                and name != str(path)
            ):
                raise EditConflict(
                    f"diff header names file {name!r} but target path is {str(path)!r}"
                )


def _apply(path: str | Path, hunks: list[dict[str, Any]]) -> str:
    original_text, lines = _read_file(path)
    if original_text == "":
        lines = []
    elif original_text.endswith("\n"):
        lines = lines[:-1]
    file_lines = len(lines)

    errors: list[str] = []
    prev_end = 0
    for i, hunk in enumerate(hunks):
        start, end, old = hunk["start"], hunk["end"], hunk["old"]
        tag = f"lines {start}-{end}:"
        if start < 1 or end < start or end > file_lines:
            errors.append(f"{tag} range out of bounds (file has {file_lines} line(s))")
            continue
        if start <= prev_end:
            errors.append(f"{tag} hunks overlap or are not in ascending order")
            continue
        prev_end = end
        if old is not None:
            if len(old) != end - start + 1:
                errors.append(
                    f"{tag} anchor has {len(old)} line(s) but range covers "
                    f"{end - start + 1} line(s)"
                )
                continue
            for offset, expected in enumerate(old):
                actual = lines[start - 1 + offset]
                if actual != expected:
                    errors.append(
                        f"{tag} line {start + offset} does not match\n"
                        f"  expected: {expected!r}\n"
                        f"  actual:   {actual!r}"
                    )
    if errors:
        raise EditConflict("\n".join(errors))

    new_lines = list(lines)
    for hunk in sorted(hunks, key=lambda h: h["start"], reverse=True):
        new_lines[hunk["start"] - 1 : hunk["end"]] = _split_text(hunk["text"])

    trailing_newline = original_text.endswith("\n")
    new_text = "\n".join(new_lines)
    if trailing_newline:
        new_text += "\n"
    if not hunks:
        return f"applied 0 hunk(s) to {Path(path).as_posix()} ({file_lines} -> {len(new_lines)} lines)"
    Path(path).write_text(new_text, encoding="utf-8", newline="\n")
    return (
        f"applied {len(hunks)} hunk(s) to {Path(path).as_posix()} "
        f"({file_lines} -> {len(new_lines)} lines)"
    )


def apply_patch(path: str | Path, hunks: Any) -> str:
    """Apply validated hunks (or a unified-diff string) to ``path`` atomically."""
    if isinstance(hunks, str):
        _check_diff_header_file(hunks, path)
        hunks = parse_unified_diff(hunks)
    normalized = [_normalize_hunk(h, i) for i, h in enumerate(hunks)]
    return _apply(path, normalized)


def edit_lines(path: str | Path, ranges: Any) -> str:
    """Replace line ranges in ``path`` (no anchors), atomically."""
    normalized = [_normalize_hunk(r, i) for i, r in enumerate(ranges)]
    return _apply(path, normalized)
