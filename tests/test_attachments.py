"""Tests for swarmforge.attachments (both upload-object and dict APIs)."""

from __future__ import annotations

import base64
from pathlib import Path

from swarmforge.attachments import (
    IMAGE_EXTENSIONS,
    MAX_FILES,
    MAX_INLINE_BYTES,
    TEXT_EXTENSIONS,
    classify,
    data_url,
    guess_mime,
    image_data_url,
    inline_text,
    render_section,
    sanitize_name,
    save_attachments,
)


class FakeFile:
    """Duck-types streamlit's UploadedFile (name + getvalue)."""

    def __init__(self, name: str, data: bytes = b"data"):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


# ----------------------------------------------------------------- sanitize_name


def test_sanitize_name_strips_path_traversal():
    assert sanitize_name("..\\..\\evil.png") == "evil.png"
    assert sanitize_name("../../evil.png") == "evil.png"
    assert sanitize_name("/etc/passwd") == "passwd"
    assert sanitize_name("C:\\Users\\bob\\secret.txt") == "secret.txt"


def test_sanitize_name_charset_and_spaces():
    # unsafe characters (including spaces) become underscores
    assert sanitize_name("my notes (1).txt") == "my_notes_1.txt"
    assert sanitize_name("hello world.md") == "hello_world.md"
    assert sanitize_name("my report.md") == "my_report.md"
    assert sanitize_name("a*b?c.txt") == "a_b_c.txt"


def test_sanitize_name_unicode_replaced():
    assert sanitize_name("résumé.txt") == "r_sum.txt"
    assert sanitize_name("héllo wörld.txt") == "h_llo_w_rld.txt"


def test_sanitize_name_stem_cap_keeps_extension():
    name = "a" * 200 + ".txt"
    result = sanitize_name(name)
    assert result.endswith(".txt")
    assert result == "a" * 80 + ".txt"
    assert len(sanitize_name("b" * 200)) == 80


def test_sanitize_name_never_empty():
    assert sanitize_name("") == "file"
    assert sanitize_name("///") == "file"
    assert sanitize_name("???") == "file"
    assert sanitize_name("...") == "file"
    assert sanitize_name("   ") == "file"


# -------------------------------------------------------------------- classify


def test_classify_paths():
    assert classify(Path("a.png")) == "image"
    assert classify(Path("a.JPG")) == "image"
    assert classify(Path("a.py")) == "text"
    assert classify(Path("a.bin")) == "other"
    assert classify(Path("noext")) == "other"


def test_classify_names():
    for ext in (".txt", ".py", ".json", ".md"):
        assert classify(f"file{ext}") == "text"
    for ext in (".png", ".jpg", ".webp"):
        assert classify(f"file{ext}") == "image"
    assert classify("archive.tar.gz") == "other"
    assert classify("binary.bin") == "other"
    assert classify("PHOTO.PNG") == "image"
    assert classify("SCRIPT.PY") == "text"


# ------------------------------------------------------------- mime / data urls


def test_guess_mime():
    assert guess_mime("a.png") == "image/png"
    assert guess_mime("a.jpg") == "image/jpeg"
    assert guess_mime("a.jpeg") == "image/jpeg"
    assert guess_mime("a.bin") == "application/octet-stream"


def test_data_url_prefix():
    url = data_url("pic.png", b"hello")
    assert url.startswith("data:image/png;base64,")
    payload = url.split(",", 1)[1]
    assert base64.b64decode(payload) == b"hello"


def test_image_data_url_prefix(tmp_path: Path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG")
    url = image_data_url(p)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG"


# ------------------------------------------------------------------ inline text


def test_inline_text_size_cap(tmp_path: Path):
    small = tmp_path / "small.txt"
    small.write_text("hello")
    big = tmp_path / "big.txt"
    big.write_text("x" * (MAX_INLINE_BYTES + 1))
    assert inline_text(small) == "hello"
    assert inline_text(big) is None
    assert inline_text(tmp_path / "missing.txt") is None
    assert inline_text(tmp_path / "img.png") is None


# --------------------------------------------------------------- render section


def test_render_section_inlines_text_and_handles_missing(tmp_path: Path):
    txt = tmp_path / "notes.txt"
    txt.write_text("some notes")
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG")
    missing = tmp_path / "gone.md"
    md = render_section([txt, img, missing])
    assert md.startswith("## Attached files")
    assert "some notes" in md
    assert "- pic.png (image)" in md
    assert "- gone.md (text)" in md


def test_render_section_empty():
    assert render_section([]) == ""


# ------------------------------------------------- save_attachments (file objects)


def test_save_attachments_collision_safe(tmp_path: Path):
    files = [
        FakeFile("notes.txt", b"one"),
        FakeFile("notes.txt", b"two"),
        FakeFile("notes.txt", b"three"),
    ]
    saved = save_attachments(files, tmp_path / "att")
    names = [p.name for p in saved]
    assert names == ["notes.txt", "notes_1.txt", "notes_2.txt"]
    assert all(p.is_absolute() for p in saved)
    assert (tmp_path / "att" / "notes.txt").read_bytes() == b"one"
    assert (tmp_path / "att" / "notes_2.txt").read_bytes() == b"three"


def test_save_attachments_max_files(tmp_path: Path):
    files = [FakeFile(f"f{i}.txt") for i in range(MAX_FILES + 5)]
    saved = save_attachments(files, tmp_path / "att")
    assert len(saved) == MAX_FILES


def test_save_attachments_creates_dest(tmp_path: Path):
    dest = tmp_path / "a" / "b" / "c"
    saved = save_attachments([FakeFile("x.txt", b"hi")], dest)
    assert saved[0].read_bytes() == b"hi"


def test_save_attachments_sanitizes_upload_names(tmp_path: Path):
    out = save_attachments([FakeFile("../../tmp/dir/evil name!.png", b"\x89PNG")],
                           tmp_path / "att")
    assert out[0].name == "evil_name.png"
    assert out[0].is_file()


# ---------------------------------------------------- save_attachments (dicts)


def test_save_attachments_dicts_writes_files(tmp_path: Path):
    target = tmp_path / "att"
    paths = save_attachments(target, [{"name": "a.txt", "data": b"hi"}])
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"hi"
    assert paths[0].parent == target.resolve()


def test_save_attachments_dicts_creates_missing_dirs(tmp_path: Path):
    target = tmp_path / "x" / "y" / "z"
    save_attachments(target, [{"name": "a.txt", "data": b"hi"}])
    assert target.is_dir()


def test_save_attachments_dicts_collision_suffixing(tmp_path: Path):
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


def test_save_attachments_dicts_collision_no_extension(tmp_path: Path):
    (tmp_path / "file").write_bytes(b"x")
    paths = save_attachments(tmp_path, [{"name": "file", "data": b"y"}])
    assert paths[0].name == "file_1"


def test_save_attachments_dicts_max_files_cap(tmp_path: Path):
    attachments = [{"name": f"f{i}.txt", "data": b"x"} for i in range(MAX_FILES + 5)]
    assert len(save_attachments(tmp_path, attachments)) == MAX_FILES


def test_save_attachments_dicts_sanitizes_bad_names(tmp_path: Path):
    paths = save_attachments(
        tmp_path,
        [
            {"name": "../../evil.txt", "data": b"1"},
            {"name": "", "data": b"2"},
        ],
    )
    assert paths[0].name == "evil.txt"
    assert paths[1].name == "file"


def test_save_attachments_dicts_empty_list(tmp_path: Path):
    assert save_attachments(tmp_path, []) == []


# ------------------------------------------------------------------- constants


def test_constants():
    assert ".txt" in TEXT_EXTENSIONS and ".py" in TEXT_EXTENSIONS
    assert ".png" in IMAGE_EXTENSIONS and ".svg" in IMAGE_EXTENSIONS
    assert isinstance(TEXT_EXTENSIONS, frozenset)
    assert isinstance(IMAGE_EXTENSIONS, frozenset)
    assert not (TEXT_EXTENSIONS & IMAGE_EXTENSIONS)
    assert MAX_INLINE_BYTES == 8192
    assert MAX_FILES == 10
