"""Bounded access to retained reports/visual PNGs under an explicit runs root.

Database paths, report links and filenames supplied by clients never choose a
filesystem root. The engine writes workdir/runs/RUN/report.md and artifacts/visual.
"""
from __future__ import annotations

import os
import re
import stat
import struct
from pathlib import Path

MAX_REPORT_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_SHOTS = 24
MAX_DIRECTORY_ENTRIES = 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
SHOT_NAME = re.compile(r"c([0-9]{1,8})-([a-z0-9-]{1,40})-(light|dark)\.png")


class ArtifactUnavailable(ValueError):
    """Intentionally contains no path or underlying OS exception."""


class ArtifactMissing(ArtifactUnavailable):
    pass


class ArtifactReader:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _checked(self, run_id: str, *parts: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ArtifactUnavailable("Artifact unavailable")
        return self._checked_parts((run_id, *parts))

    def _checked_parts(self, parts: tuple[str, ...]) -> Path:
        path = self.root
        try:
            for part in parts:
                if not part or part in (".", "..") or any(c in part for c in ("/", "\\", ":", "\0")):
                    raise ArtifactUnavailable("Artifact unavailable")
                path = path / part
                info = path.lstat()
                # Includes Windows junctions and other reparse points, even if
                # is_symlink() does not identify them as symbolic links.
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ArtifactUnavailable("Artifact unavailable")
            if not path.resolve(strict=True).is_relative_to(self.root):
                raise ArtifactUnavailable("Artifact unavailable")
            return path
        except FileNotFoundError:
            raise ArtifactMissing("Artifact not retained") from None
        except (OSError, RuntimeError):
            raise ArtifactUnavailable("Artifact unavailable") from None

    def _read(self, run_id: str, parts: tuple[str, ...], limit: int):
        if not RUN_ID.fullmatch(run_id):
            raise ArtifactUnavailable("Artifact unavailable")
        return self._read_parts((run_id, *parts), limit)

    def _read_parts(self, parts: tuple[str, ...], limit: int):
        path = self._checked_parts(parts)
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ArtifactUnavailable("Artifact unavailable")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ArtifactUnavailable("Artifact unavailable")
                self._checked_parts(parts)
                content = stream.read(limit + 1)
                self._checked_parts(parts)
            return content[:limit], opened.st_size, len(content) > limit
        except FileNotFoundError:
            raise ArtifactMissing("Artifact not retained") from None
        except OSError:
            raise ArtifactUnavailable("Artifact unavailable") from None

    @staticmethod
    def _png(header: bytes, size: int):
        if size > MAX_IMAGE_BYTES or len(header) < 33 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[8:16] != b"\x00\x00\x00\rIHDR":
            raise ArtifactUnavailable("Screenshot unavailable")
        width, height = struct.unpack(">II", header[16:24])
        if not width or not height or max(width, height) > 50_000 or width * height > MAX_PIXELS:
            raise ArtifactUnavailable("Screenshot unavailable")

    def report(self, run_id: str):
        text, size, truncated = self._read(run_id, ("report.md",), MAX_REPORT_BYTES)
        return {"text": text.decode("utf-8", errors="replace"), "size_bytes": size, "truncated": truncated}

    def screenshot(self, run_id: str, name: str):
        if not SHOT_NAME.fullmatch(name):
            raise ArtifactUnavailable("Screenshot unavailable")
        content, size, truncated = self._read(run_id, ("artifacts", "visual", name), MAX_IMAGE_BYTES)
        self._png(content, size)
        if truncated:
            raise ArtifactUnavailable("Screenshot unavailable")
        return content

    def listing(self, run_id: str | None):
        result = {"report_status": "missing", "report_bytes": None, "report_truncated": False,
                  "screenshots": [], "screenshots_truncated": False, "screenshots_unavailable": False}
        if run_id is None:
            return result
        try:
            path = self._checked(run_id, "report.md")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ArtifactUnavailable("Report unavailable")
            result.update(report_status="available", report_bytes=info.st_size, report_truncated=info.st_size > MAX_REPORT_BYTES)
        except ArtifactMissing:
            pass
        except (ArtifactUnavailable, OSError):
            result["report_status"] = "unavailable"
        try:
            folder = self._checked(run_id, "artifacts", "visual")
            names = []
            with os.scandir(folder) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_DIRECTORY_ENTRIES:
                        result["screenshots_truncated"] = True
                        break
                    match = SHOT_NAME.fullmatch(entry.name)
                    if match:
                        names.append((int(match[1]), entry.name))
            names.sort(reverse=True)
            result["screenshots_truncated"] |= len(names) > MAX_SHOTS
            for _, name in names[:MAX_SHOTS]:
                try:
                    header, size, _ = self._read(run_id, ("artifacts", "visual", name), 33)
                    self._png(header, size)
                    result["screenshots"].append({"name": name, "size_bytes": size})
                except ArtifactUnavailable:
                    result["screenshots_unavailable"] = True
        except ArtifactMissing:
            pass
        except (ArtifactUnavailable, OSError):
            result["screenshots_unavailable"] = True
        return result
