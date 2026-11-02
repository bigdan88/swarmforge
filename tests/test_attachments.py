import base64
from pathlib import Path

from swarmforge.attachments import (
    IMAGE_EXTENSIONS,
    MAX_FILES,
    MAX_INLINE_BYTES,
    TEXT_EXTENSIONS,
    classify,
    image_data_url,
    inline_text,
    render_section,
    sanitize_name,
    save_attachments,
)


class FakeFile:
    def __init__(self, name: str, data: bytes = b"data"):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


def test_sanitize_name_strips_path_traversal():
    assert sanitize_name("..\\..\\evil.png") == "evil.png"
    assert sanitize_name("../../evil.png") == "evil.png"
    assert sanitize_name("/etc/passwd") == "passwd"


def test_sanitize_name_charset_and_spaces():
    assert sanitize_name("my notes (1).txt") == "my_notes_1.txt"
    assert sanitize_name("hello world.md") == "hello_world.md"


def test_sanitize_name_stem_cap_keeps_extension():
    name = "a" * 200 + ".txt"
    result = sanitize_name(name)
    assert result.endswith(".txt")
    assert len(result) == 80 + len(".txt")


def test_sanitize_name_never_empty():
    assert sanitize_name("///") == "file"
    assert sanitize_name("???") == "file"


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


def test_classify():
    assert classify(Path("a.png")) == "image"
    assert classify(Path("a.JPG")) == "image"
    assert classify(Path("a.py")) == "text"
    assert classify(Path("a.bin")) == "other"
    assert classify(Path("noext")) == "other"


def test_image_data_url_prefix(tmp_path: Path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG")
    url = image_data_url(p)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\x89PNG"


def test_inline_text_size_cap(tmp_path: Path):
    small = tmp_path / "small.txt"
    small.write_text("hello")
    big = tmp_path / "big.txt"
    big.write_text("x" * (MAX_INLINE_BYTES + 1))
    assert inline_text(small) == "hello"
    assert inline_text(big) is None
    assert inline_text(tmp_path / "missing.txt") is None
    assert inline_text(tmp_path / "img.png") is None


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


def test_constants():
    assert ".txt" in TEXT_EXTENSIONS and ".py" in TEXT_EXTENSIONS
    assert ".png" in IMAGE_EXTENSIONS and ".svg" in IMAGE_EXTENSIONS
    assert MAX_INLINE_BYTES == 8192
    assert MAX_FILES == 10
