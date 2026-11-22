"""Bounded top-level vault browsing with opaque IDs and safe retained-file reads."""
import hashlib
import os
import re

from .artifacts import ArtifactReader, ArtifactUnavailable


class NotesReader:
    def __init__(self, root):
        self.files = ArtifactReader(root) if root else None

    def _entries(self):
        if self.files is None or not self.files.root.is_dir():
            return [], False
        entries, limited = [], False
        with os.scandir(self.files.root) as scan:
            for i, item in enumerate(scan):
                if i >= 1024:
                    limited = True
                    break
                if item.name.endswith(".md") and not item.name.startswith("."):
                    entries.append((hashlib.sha256(item.name.encode()).hexdigest()[:24], item.name))
        return sorted(entries, key=lambda e: e[1].casefold()), limited

    def _note(self, opaque, name):
        content, size, truncated = self.files._read_parts((name,), 64000)
        text = content.decode("utf-8", errors="replace")
        title = re.search(r"(?m)^title:\s*(.+)$", text) or re.search(r"(?m)^#\s+(.+)$", text)
        return {"id": opaque, "title": title[1].strip("\"'")[:300] if title else name[:-3], "text": text,
                "size_bytes": size, "truncated": truncated}

    def list(self, search="", offset=0):
        entries, limited = self._entries()
        found = []
        for opaque, name in entries:
            try:
                note = self._note(opaque, name)
                if search.casefold() in (note["title"] + "\n" + note["text"]).casefold():
                    found.append({"id": opaque, "title": note["title"], "excerpt": note["text"][:200]})
            except ArtifactUnavailable:
                continue
        return {"items": found[offset:offset+25], "total": len(found), "limit": 25, "offset": offset,
                "configured": self.files is not None, "limited": limited}

    def detail(self, note_id):
        if not re.fullmatch(r"[a-f0-9]{24}", note_id):
            return None
        entries, _ = self._entries()
        for opaque, name in entries:
            if opaque == note_id:
                try:
                    return self._note(opaque, name)
                except ArtifactUnavailable:
                    return None
        return None
