"""Reply-scoped downloadable files; no arbitrary filesystem reads or executable tools."""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from pathlib import Path

from .attachments import IMAGE_MIME, RASTER_SIGNATURES, TEXT_EXTENSIONS, raster_image_matches, sanitize_name

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 8
BINARY_TYPES = {".pdf": "application/pdf", ".zip": "application/zip", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
SCHEMA = """CREATE TABLE IF NOT EXISTS chat_files (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, reply_id TEXT NOT NULL, message_id INTEGER,
    name TEXT NOT NULL, kind TEXT NOT NULL, media_type TEXT NOT NULL, path TEXT NOT NULL,
    size INTEGER NOT NULL, created_at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS chat_files_message ON chat_files(conversation_id,message_id);
"""


def root(config):
    return (Path(config.workdir) / "chat_outputs").resolve()


def ensure_tables(store):
    store.ensure_schema(SCHEMA)


def metadata(row):
    return {k: row[k] for k in ("id", "name", "kind", "size", "media_type")}


class ReplyFiles:
    def __init__(self, config, store, conversation, reply_id=None):
        self.config, self.store, self.conversation = config, store, conversation
        self.reply_id = reply_id or uuid.uuid4().hex
        self.ids = []
        ensure_tables(store)

    def create(self, name: str, content: str, encoding: str = "utf-8") -> str:
        if len(self.ids) >= MAX_FILES:
            raise ValueError("A reply can attach at most eight files")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Give the file a short filename")
        if not isinstance(content, str) or len(content) > MAX_FILE_BYTES * 4 // 3 + 8:
            raise ValueError("Files are limited to 10 MB")
        safe = sanitize_name(name)
        ext = Path(safe).suffix.lower()
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("Provide valid base64 for a binary file") from None
        else:
            raise ValueError("Choose utf-8 text or base64 binary content")
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError("Files must be between 1 byte and 10 MB")
        kind, media = "file", "application/octet-stream"
        if ext in RASTER_SIGNATURES:
            if not raster_image_matches(ext, data):
                raise ValueError("The image data does not match its file type")
            kind, media = "image", IMAGE_MIME[ext]
        elif ext in TEXT_EXTENSIONS or ext in {".svg", ".tsx", ".jsx"}:
            data.decode("utf-8")
            media = "text/plain; charset=utf-8"  # HTML/SVG/code download as inert attachments.
        elif ext in BINARY_TYPES:
            if ext == ".pdf" and not data.startswith(b"%PDF-"):
                raise ValueError("The PDF data is invalid")
            if ext != ".pdf" and not data.startswith(b"PK\x03\x04"):
                raise ValueError("The archive/document data is invalid")
            media = BINARY_TYPES[ext]
        else:
            raise ValueError("Create a text/code file, PDF, ZIP, Office document or PNG/JPEG/GIF/WebP image")
        file_id = uuid.uuid4().hex
        folder = root(self.config) / file_id
        folder.mkdir(parents=True, exist_ok=False)
        path = folder / safe
        path.write_bytes(data)
        self.store._exec("INSERT INTO chat_files(id,conversation_id,reply_id,name,kind,media_type,path,size,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (file_id, self.conversation, self.reply_id, safe, kind, media, str(path), len(data), time.time()))
        self.ids.append(file_id)
        return json.dumps({"id": file_id, "name": safe, "kind": kind, "size": len(data),
                           "url": f"/api/v1/conversations/{self.conversation}/files/{file_id}", "attached": True})

    def attach(self, message_id):
        self.store._exec("UPDATE chat_files SET message_id=? WHERE reply_id=? AND conversation_id=? AND message_id IS NULL",
                         (message_id, self.reply_id, self.conversation))

    def from_fences(self, text):
        for match in re.finditer(r"^```file:([^\r\n]+)\r?\n(.*?)^```\s*$", text, re.M | re.S):
            if len(self.ids) >= MAX_FILES:
                break
            name, content = match.groups()
            if Path(name.strip()).suffix.lower() not in TEXT_EXTENSIONS | {".svg", ".tsx", ".jsx"}:
                continue
            if self.store._one("SELECT id FROM chat_files WHERE reply_id=? AND name=?", (self.reply_id, sanitize_name(name.strip()))):
                continue
            try:
                self.create(name.strip(), content)
            except ValueError:
                continue  # The fenced source remains readable/copyable even when too large to attach.

    def register(self, registry):
        registry.register("create_file", "Create a downloadable file attached to this reply. Text/code uses utf-8; real binary files and raster images use base64. Does not run code or read files. Do not invent binary data.",
                          {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}, "encoding": {"type": "string", "enum": ["utf-8", "base64"]}}, "required": ["name", "content"]}, self.create)
