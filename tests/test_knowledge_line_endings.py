"""Knowledge notes parse identically with LF, CRLF, mixed and CR line endings (Windows vaults)."""
import hashlib

import pytest

from swarmforge.knowledge import Vault, parse_note, split_front_matter
from swarmforge.webapp.notes import NotesReader

FRONT = "---\ntitle: Empty repos return lists\ntype: pitfall\nrepo: owner/app\ntags: [api, lists]\n---\n\nReturn [] not None.\n\n---\n\nAfter a rule.\n"
PLAIN = "# Plain heading\n\nNo front matter here.\n"
RULE_FIRST = "---\nThis note starts with a horizontal rule.\n---\nAnd continues.\n"

ENDINGS = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}


def encode(text, ending, bom=False):
    return (b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", ending).encode("utf-8")


def mixed(text):
    lines = text.split("\n")
    return "".join(line + ("\r\n" if i % 2 else "\n") for i, line in enumerate(lines[:-1])) + lines[-1]


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("bom", [False, True])
def test_reader_metadata_is_identical_for_every_line_ending(tmp_path, ending, bom):
    root = tmp_path / ending
    root.mkdir()
    (root / "front.md").write_bytes(encode(FRONT, ENDINGS[ending], bom))
    (root / "plain.md").write_bytes(encode(PLAIN, ENDINGS[ending], bom))
    reader = NotesReader(root)
    notes = {n["title"]: n for n in reader.list()["items"]}
    assert set(notes) == {"Empty repos return lists", "Plain heading"}
    front = reader.detail(notes["Empty repos return lists"]["id"])
    assert (front["type"], front["repo"], front["tags"]) == ("pitfall", "owner/app", ["api", "lists"])
    assert "\r" not in front["text"] and "﻿" not in front["text"]
    assert reader.detail(notes["Plain heading"]["id"])["type"] == "user-note"
    # Conflict detection still hashes the original bytes, and reading never rewrites the file.
    raw = (root / "front.md").read_bytes()
    assert front["sha"] == hashlib.sha256(raw).hexdigest()
    assert (root / "front.md").read_bytes() == raw


def test_mixed_line_endings_parse_like_lf(tmp_path):
    (tmp_path / "mixed.md").write_bytes(mixed(FRONT).encode())
    (tmp_path / "lf.md").write_bytes(FRONT.encode())
    reader = NotesReader(tmp_path)
    details = [reader.detail(n["id"]) for n in reader.list()["items"]]
    assert [(d["title"], d["type"], d["tags"]) for d in details] == [("Empty repos return lists", "pitfall", ["api", "lists"])] * 2


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_a_leading_horizontal_rule_is_not_front_matter(tmp_path, ending):
    (tmp_path / "rule.md").write_bytes(encode(RULE_FIRST, ENDINGS[ending]))
    meta, body = split_front_matter(RULE_FIRST.replace("\n", ENDINGS[ending]))
    assert meta == {} and body.startswith("---\nThis note starts")
    note = parse_note(tmp_path / "rule.md")
    assert "This note starts with a horizontal rule." in note.body and "And continues." in note.body


def test_saving_an_edited_crlf_note_keeps_rules_and_leading_text(tmp_path):
    vault = Vault(tmp_path)
    path = tmp_path / "front.md"
    path.write_bytes(encode(FRONT, "\r\n", bom=True))
    note = parse_note(path)
    assert note.title == "Empty repos return lists" and note.tags == ["api", "lists"]
    note.title = "Edited"
    vault.save(note)
    again = parse_note(path)
    assert again.title == "Edited" and again.type == "pitfall"
    assert "Return [] not None." in again.body and "\n---\n" in again.body and "After a rule." in again.body
