"""Tests for swarmforge.attachments."""

from __future__ import annotations

import base64

from swarmforge.attachments import (
    IMAGE_EXTENSIONS,
    MAX_FILES,
    MAX_INLINE_BYTES,
    TEXT_EXTENSIONS,
    classify,
    data_url,
    guess_mime,
    sanitize_name,
    save_attachments,
)


class TestSanitizeName:
    def test_path_traversal(self):
        assert sanitize_name("../../etc/passwd") == "passwd"
        assert sanitize_name("C:\\Users\\bob\\secret.txt") == "secret.txt"

    def test_spaces_kept(self):
        assert sanitize_name("my report.md") == "my report.md"

    def test_unsafe_chars_replaced(self):
        assert sanitize_name("a*b?c.txt") == "a_b_c.txt"

    def test_unicode_replaced(self):
        assert sanitize_name("résumé.txt") == "r_sum_.txt"

    def test_long_name_capped_at_80(self):
        name = "a" * 100 + ".txt"
        result = sanitize_name(name)
        assert len(result) == 80
        assert result.endswith(".txt")

    def test_long_name_without_extension(self):
        result = sanitize_name("b" * 100)
        assert len(result) == 80

    def test_empty(self):
        assert sanitize_name("") == "file"
        assert sanitize_name("///") == "file"

    def test_never_empty(self):
        for bad in ["...", "..", "???", "   "]:
            assert sanitize_name(bad)


class TestClassify:
    def test_text(self):
        for ext in [".txt", ".py", ".json", ".md"]:
            assert classify(f"file{ext}") == "text"

    def test_image(self):
        for ext in [".png", ".jpg", ".webp"]:
            assert classify(f"file{ext}") == "image"

    def test_other(self):
        assert classify("archive.tar.gz") == "other"
        assert classify("binary.bin") == "other"

    def test_uppercase(self):
        assert classify("PHOTO.PNG") == "image"
        assert classify("SCRIPT.PY") == "text"


class TestMimeAndDataUrl:
    def test_guess_mime(self):
        assert guess_mime("a.png") == "image/png"
        assert guess_mime("a.jpg") == "image/jpeg"
        assert guess_mime("a.jpeg") == "image/jpeg"
        assert guess_mime("a.bin") == "application/octet-stream"

    def test_data_url_prefix(self):
        url = data_url("pic.png", b"hello")
        assert url.startswith("data:image/png;base64,")
        payload = url.split(",", 1)[1]
        assert base64.b64decode(payload) == b"hello"

    def test_constants(self):
        assert ".txt" in TEXT_EXTENSIONS
        assert ".png" in IMAGE_EXTENSIONS
        assert MAX_INLINE_BYTES == 8192
        assert MAX_FILES == 10


class TestSaveAttachments:
    def test_writes_files(self, tmp_path):
        target = tmp_path / "att"
        paths = save_attachments(target, [{"name": "a.txt", "data": b"hi"}])
        assert len(paths) == 1
        assert paths[0].read_bytes() == b"hi"
        assert paths[0].parent == target.resolve()

    def test_creates_missing_dirs(self, tmp_path):
        target = tmp_path / "x" / "y" / "z"
        save_attachments(target, [{"name": "a.txt", "data": b"hi"}])
        assert target.is_dir()

    def test_absolute_paths(self, tmp_path):
        paths = save_attachments(tmp_path, [{"name": "a.txt", "data": b"hi"}])
        assert all(p.is_absolute() for p in paths)

    def test_collision_suffixing(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"existing")
        paths = save_attachments(
            tmp_path,
            [
                {"name": "a.txt", "data": b"one"},
                {"name": "a.txt", "data": b"two"},
            ],
        )
        names = sorted(p.name for p in paths)
        assert names == ["a_1.txt", "a_2.txt"]
        assert (tmp_path / "a.txt").read_bytes() == b"existing"
        assert (tmp_path / "a_1.txt").read_bytes() == b"one"
        assert (tmp_path / "a_2.txt").read_bytes() == b"two"

    def test_collision_no_extension(self, tmp_path):
        (tmp_path / "file").write_bytes(b"x")
        paths = save_attachments(tmp_path, [{"name": "file", "data": b"y"}])
        assert paths[0].name == "file_1"

    def test_max_files_cap(self, tmp_path):
        attachments = [
            {"name": f"f{i}.txt", "data": b"x"} for i in range(MAX_FILES + 5)
        ]
        paths = save_attachments(tmp_path, attachments)
        assert len(paths) == MAX_FILES

    def test_sanitizes_bad_names(self, tmp_path):
        paths = save_attachments(
            tmp_path,
            [
                {"name": "../../evil.txt", "data": b"1"},
                {"name": "", "data": b"2"},
            ],
        )
        assert paths[0].name == "evil.txt"
        assert paths[1].name == "file"

    def test_empty_list(self, tmp_path):
        assert save_attachments(tmp_path, []) == []
