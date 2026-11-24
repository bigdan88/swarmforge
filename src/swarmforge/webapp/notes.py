"""Bounded vault browsing with opaque IDs, nested notes, metadata facets and safe retained-file reads.

Notes are found by a bounded walk that never follows symbolic links, junctions or hidden folders.
IDs are a hash of the note's relative path, so the browser never sees vault paths. Writes resolve an
ID through the same walk and require the hash of the content the user reviewed.
"""
import hashlib
import os
import re
import stat

from ..knowledge import normalize_newlines, split_front_matter
from .artifacts import ArtifactReader, ArtifactUnavailable

MAX_ENTRIES = 2048
MAX_DEPTH = 6
MAX_NOTE_BYTES = 64000
EDITABLE_TYPES = ("user-note", "decision", "pitfall", "pattern", "repo-fact", "review-lesson", "model-behavior")


def note_id(parts: tuple[str, ...]) -> str:
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[:24]


class NotesReader:
    def __init__(self, root):
        self.files = ArtifactReader(root) if root else None

    @property
    def root(self):
        return self.files.root if self.files else None

    def _entries(self):
        if self.files is None or not self.files.root.is_dir():
            return [], False
        entries, limited, seen = [], False, 0
        stack = [((), self.files.root)]
        while stack:
            prefix, folder = stack.pop()
            try:
                scan = list(os.scandir(folder))
            except OSError:
                continue
            for item in scan:
                seen += 1
                if seen > MAX_ENTRIES:
                    return sorted(entries, key=lambda e: "/".join(e[1]).casefold()), True
                if item.name.startswith("."):
                    continue
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                # Never follow links, junctions or other reparse points out of the vault.
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                parts = (*prefix, item.name)
                if stat.S_ISDIR(info.st_mode):
                    if len(parts) < MAX_DEPTH:
                        stack.append((parts, item.path))
                elif item.name.endswith(".md"):
                    entries.append((note_id(parts), parts))
        return sorted(entries, key=lambda e: "/".join(e[1]).casefold()), limited

    def _note(self, opaque, parts):
        content, size, truncated = self.files._read_parts(parts, MAX_NOTE_BYTES)
        # Display parsing only: the SHA below still covers the original bytes, and files are never rewritten.
        text = normalize_newlines(content.decode("utf-8", errors="replace"))
        meta, body = split_front_matter(text)
        heading = re.search(r"(?m)^#\s+(.+)$", body)
        title = meta.get("title") or (heading[1] if heading else parts[-1][:-3])
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        return {"id": opaque, "title": str(title).strip("\"'")[:300], "text": text, "body": body,
                "type": str(meta.get("type") or "user-note")[:40], "repo": str(meta.get("repo") or "")[:300] or None,
                "tags": [str(t)[:60] for t in tags if str(t).strip()][:30], "folder": "/".join(parts[:-1]) or None,
                "source": str(meta.get("source") or "")[:120] or None, "created": str(meta.get("created") or "")[:40] or None,
                "size_bytes": size, "truncated": truncated,
                "sha": None if truncated else hashlib.sha256(content).hexdigest(),
                "editable": not truncated and str(meta.get("type") or "user-note") in EDITABLE_TYPES}

    def list(self, search="", offset=0, type="", repo=""):
        entries, limited = self._entries()
        found, types, repos = [], set(), set()
        for opaque, parts in entries:
            try:
                note = self._note(opaque, parts)
            except ArtifactUnavailable:
                continue
            types.add(note["type"])
            if note["repo"]:
                repos.add(note["repo"])
            if (type and note["type"] != type) or (repo and note["repo"] != repo):
                continue
            if search.casefold() in (note["title"] + "\n" + note["text"]).casefold():
                found.append({"id": opaque, "title": note["title"], "excerpt": note["body"][:200], "type": note["type"],
                              "repo": note["repo"], "folder": note["folder"]})
        return {"items": found[offset:offset+25], "total": len(found), "limit": 25, "offset": offset,
                "configured": self.files is not None, "limited": limited,
                "types": sorted(types)[:50], "repos": sorted(repos)[:200]}

    def locate(self, note_id_value):
        if not isinstance(note_id_value, str) or not re.fullmatch(r"[a-f0-9]{24}", note_id_value):
            return None
        entries, _ = self._entries()
        return next((parts for opaque, parts in entries if opaque == note_id_value), None)

    def detail(self, note_id_value):
        parts = self.locate(note_id_value)
        if parts is None:
            return None
        try:
            note = self._note(note_id_value, parts)
        except ArtifactUnavailable:
            return None
        note.pop("body")
        return note
