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


def test_apply_patch_diff_new_file_hunk(tmp_path: Path) -> None:
    """A unified diff whose '@@ -0,0' header creates a NEW file is rejected with
    a helpful 'not found' message when the target already exists (AC2/AC5)."""
    p = write(tmp_path, "a\nb\n")
    diff = f"--- a/{p.name}\n+++ b/{p.name}\n@@ -0,0 +1,2 @@\n+x\n+y\n"
    with pytest.raises(EditConflict, match="not found"):
        apply_patch(p, diff)
    assert p.read_text(encoding="utf-8") == "a\nb\n"


def test_edit_conflict_is_value_error() -> None:
    assert issubclass(EditConflict, ValueError)
    with pytest.raises(ValueError):
        raise EditConflict("boom")


# ------------------------------------------------------- agent tool layer
from swarmforge.agent import pi_runtime as PR
from swarmforge.agent.tools import ASYNC_IMPLS, TOOL_SCHEMAS, ToolContext, ToolError
from swarmforge.config import SandboxConfig
from swarmforge.sandbox import LocalSandbox


def _ctx(tmp_path: Path) -> ToolContext:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "target.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    return ToolContext(
        workspace=ws,
        sandbox=LocalSandbox(SandboxConfig(mode="local")),
        artifacts_dir=tmp_path / "artifacts",
    )


def test_edit_lines_registered() -> None:
    assert "edit_lines" in ASYNC_IMPLS and "edit_lines" in TOOL_SCHEMAS
    assert "apply_patch" in ASYNC_IMPLS and "apply_patch" in TOOL_SCHEMAS
    assert "edit_lines" in PR.SF_TOOLS and "apply_patch" in PR.SF_TOOLS


async def test_tool_edit_lines_applies_and_records(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    msg: str = await ASYNC_IMPLS["edit_lines"](
        ctx, path="target.py", ranges=[{"start": 2, "end": 3, "text": "B\nC"}]
    )
    assert "target.py" in msg and "2 hunk" not in msg
    assert (ctx.workspace / "target.py").read_text(encoding="utf-8") == "a\nB\nC\nd\n"
    assert ctx.files_written == {"target.py"}


async def test_tool_anchor_mismatch_is_tool_error(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match="does not match"):
        await ASYNC_IMPLS["apply_patch"](
            ctx,
            path="target.py",
            hunks=[{"start": 1, "end": 1, "old": ["WRONG"], "text": "x"}],
        )
    assert (ctx.workspace / "target.py").read_text(encoding="utf-8") == "a\nb\nc\nd\n"
    assert ctx.files_written == set()


async def test_tool_rejects_path_escape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match="escapes the workspace"):
        await ASYNC_IMPLS["edit_lines"](
            ctx, path="../evil.py", ranges=[{"start": 1, "end": 1, "text": "x"}]
        )


async def test_tool_rejects_bad_arg_shape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match="ranges must be a list"):
        await ASYNC_IMPLS["edit_lines"](ctx, path="target.py", ranges="nope")
    with pytest.raises(ToolError, match="hunks must be"):
        await ASYNC_IMPLS["apply_patch"](ctx, path="target.py", hunks=42)


async def test_tool_apply_patch_unified_diff(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    diff = "--- a/target.py\n+++ b/target.py\n@@ -1,4 +1,4 @@\n a\n-b\n+B\n c\n d\n"
    msg: str = await ASYNC_IMPLS["apply_patch"](ctx, path="target.py", hunks=diff)
    assert "target.py" in msg
    assert (ctx.workspace / "target.py").read_text(encoding="utf-8") == "a\nB\nc\nd\n"
    assert ctx.files_written == {"target.py"}
