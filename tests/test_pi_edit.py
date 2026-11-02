"""Tests for the pure line-range edit engine in swarmforge.pi_edit."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from swarmforge.pi_edit import EditConflict, apply_patch, edit_lines, parse_unified_diff


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "target.py"
    p.write_text(content, encoding="utf-8")
    return p


def test_pure_stdlib_module() -> None:
    import swarmforge.pi_edit as mod

    src = inspect.getsource(mod)
    imports = [
        line.strip()
        for line in src.split("\n")
        if line.strip().startswith(("import ", "from "))
    ]
    assert imports == [
        "from __future__ import annotations",
        "from pathlib import Path",
        "from typing import Any",
    ], imports


def test_match_single_hunk(tmp_path: Path) -> None:
    p = write(tmp_path, "a\nb\nc\nd\n")
    msg = apply_patch(
        p,
        [{"start": 2, "end": 3, "old": ["b", "c"], "text": "B\nC"}],
    )
    assert p.read_text(encoding="utf-8") == "a\nB\nC\nd\n"
    assert msg == f"applied 1 hunk(s) to {p.as_posix()} (4 -> 4 lines)"


def test_match_preserves_no_trailing_newline(tmp_path: Path) -> None:
    p = write(tmp_path, "one\ntwo")
    apply_patch(p, [{"start": 1, "end": 1, "old": ["one"], "text": "ONE"}])
    assert p.read_text(encoding="utf-8") == "ONE\ntwo"


def test_mismatch_rejects_and_leaves_file_identical(tmp_path: Path) -> None:
    original = "def f():\n    return x\n"
    p = write(tmp_path, original)
    with pytest.raises(EditConflict) as exc:
        apply_patch(
            p,
            [
                {
                    "start": 1,
                    "end": 2,
                    "old": ["def f():", "    return y"],
                    "text": "nope",
                }
            ],
        )
    msg = str(exc.value)
    assert "line 2" in msg
    assert "'    return x'" in msg  # actual
    assert "'    return y'" in msg  # expected
    assert p.read_text(encoding="utf-8") == original  # byte-identical


def test_multi_hunk_applied_with_line_shift(tmp_path: Path) -> None:
    p = write(tmp_path, "1\n2\n3\n4\n5\n6\n7\n")
    msg = apply_patch(
        p,
        [
            {"start": 2, "end": 2, "old": ["2"], "text": "two\ntwo-b"},
            {"start": 5, "end": 6, "old": ["5", "6"], "text": "five"},
        ],
    )
    assert p.read_text(encoding="utf-8") == "1\ntwo\ntwo-b\n3\n4\nfive\n7\n"
    assert msg == f"applied 2 hunk(s) to {p.as_posix()} (7 -> 7 lines)"


def test_multi_hunk_second_mismatch_rejects_all(tmp_path: Path) -> None:
    original = "a\nb\nc\nd\n"
    p = write(tmp_path, original)
    with pytest.raises(EditConflict):
        apply_patch(
            p,
            [
                {"start": 1, "end": 1, "old": ["a"], "text": "A"},
                {"start": 3, "end": 3, "old": ["WRONG"], "text": "C"},
            ],
        )
    assert p.read_text(encoding="utf-8") == original


def test_overlap_rejected(tmp_path: Path) -> None:
    p = write(tmp_path, "a\nb\nc\n")
    with pytest.raises(EditConflict) as exc:
        apply_patch(
            p,
            [
                {"start": 1, "end": 2, "text": "x"},
                {"start": 2, "end": 3, "text": "y"},
            ],
        )
    assert "ascending" in str(exc.value)
    assert p.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_out_of_range_rejected(tmp_path: Path) -> None:
    p = write(tmp_path, "a\nb\n")
    with pytest.raises(EditConflict) as exc:
        apply_patch(p, [{"start": 5, "end": 9, "text": "x"}])
    assert "2 line" in str(exc.value)
    with pytest.raises(EditConflict):
        apply_patch(p, [{"start": 0, "end": 1, "text": "x"}])


def test_deletion_and_insertion_via_replacement(tmp_path: Path) -> None:
    p = write(tmp_path, "keep\ndelete\nkeep2\n")
    apply_patch(p, [{"start": 2, "end": 2, "old": ["delete"], "text": ""}])
    assert p.read_text(encoding="utf-8") == "keep\nkeep2\n"

    p2 = write(tmp_path, "x\ny\n")
    # insertion: replace line 1 with itself plus a new line
    apply_patch(p2, [{"start": 1, "end": 1, "old": ["x"], "text": "x\nnew"}])
    assert p2.read_text(encoding="utf-8") == "x\nnew\ny\n"


def test_edit_lines_tuples_and_dicts(tmp_path: Path) -> None:
    p = write(tmp_path, "a\nb\nc\n")
    msg = edit_lines(p, [(1, 1, "A"), {"start": 3, "end": 3, "text": "C"}])
    assert p.read_text(encoding="utf-8") == "A\nb\nC\n"
    assert "applied 2 hunk(s)" in msg


def test_apply_patch_unified_diff_match(tmp_path: Path) -> None:
    p = write(tmp_path, "one\ntwo\nthree\nfour\n")
    diff = """\
--- a/target.py
+++ b/target.py
@@ -1,4 +1,4 @@
 one
-two
+TWO
 three
 four
"""
    msg = apply_patch(p, diff)
    assert p.read_text(encoding="utf-8") == "one\nTWO\nthree\nfour\n"
    assert "applied 1 hunk(s)" in msg


def test_apply_patch_unified_diff_mismatch(tmp_path: Path) -> None:
    original = "one\ntwo\nthree\n"
    p = write(tmp_path, original)
    diff = """\
--- a/target.py
+++ b/target.py
@@ -1,3 +1,3 @@
 one
-NOT-TWO
+TWO
 three
"""
    with pytest.raises(EditConflict) as exc:
        apply_patch(p, diff)
    assert "line 2" in str(exc.value)
    assert p.read_text(encoding="utf-8") == original


def test_apply_patch_diff_names_other_file(tmp_path: Path) -> None:
    p = write(tmp_path, "one\n")
    diff = "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-one\n+ONE\n"
    with pytest.raises(EditConflict) as exc:
        apply_patch(p, diff)
    assert "other.py" in str(exc.value)


def test_parse_unified_diff() -> None:
    diff = "@@ -2,3 +2,4 @@\n ctx\n-old\n+new\n+extra\n ctx2\n"
    hunks = parse_unified_diff(diff)
    assert len(hunks) == 1
    assert hunks[0]["start"] == 2
    assert hunks[0]["end"] == 4
    assert hunks[0]["old"] == ["ctx", "old", "ctx2"]
    assert hunks[0]["text"] == "ctx\nnew\nextra\nctx2"


def test_empty_hunks_noop(tmp_path: Path) -> None:
    p = write(tmp_path, "a\nb\n")
    msg = apply_patch(p, [])
    assert p.read_text(encoding="utf-8") == "a\nb\n"
    assert msg == f"applied 0 hunk(s) to {p.as_posix()} (2 -> 2 lines)"
    assert edit_lines(p, []) == f"applied 0 hunk(s) to {p.as_posix()} (2 -> 2 lines)"


def test_missing_file_and_directory(tmp_path: Path) -> None:
    with pytest.raises(EditConflict, match="no such file"):
        apply_patch(tmp_path / "nope.txt", [{"start": 1, "end": 1, "text": "x"}])
    with pytest.raises(EditConflict, match="directory"):
        apply_patch(tmp_path, [{"start": 1, "end": 1, "text": "x"}])


def test_edit_conflict_is_value_error() -> None:
    assert issubclass(EditConflict, ValueError)
    with pytest.raises(ValueError):
        raise EditConflict("boom")
