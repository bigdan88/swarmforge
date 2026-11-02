"""Acceptance tests: Pi harness structured line-range edit / diff tool.

Maps to the issue's acceptance criteria:
  AC1 src/swarmforge/pi_edit.py API surface + pure stdlib (no network, no swarmforge imports)
  AC2 exact anchor matching; any mismatch rejects the WHOLE edit, file byte-identical;
      exactly one write on success (atomic)
  AC3 registered as Pi tools (SF_TOOLS / ASYNC_IMPLS / TOOL_SCHEMAS / WRITE_TOOLS) and
      callable from the Pi sandbox via `sf edit` / `sf patch`
  AC4 tests/test_pi_edit.py (match / mismatch / multi-hunk / unified diff / ctx wrappers)
      -- verified by that file passing; mapped in the test report
  AC5 README 'Pi (pi.dev) as the developer harness' section and
      prompts.PI_DEVELOPER_SYSTEM document the targeted-edit workflow
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from swarmforge import pi_edit, prompts
from swarmforge.agent import pi_runtime as PR
from swarmforge.agent.tools import (
    ASYNC_IMPLS,
    TOOL_SCHEMAS,
    WRITE_TOOLS,
    ToolContext,
    ToolError,
)
from swarmforge.config import SandboxConfig
from swarmforge.pi_edit import EditConflict
from swarmforge.resources import sf as SF
from swarmforge.sandbox import LocalSandbox

REPO_ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------------ AC1


def test_ac1_module_exposes_apply_patch_and_edit_lines() -> None:
    assert callable(pi_edit.apply_patch) and callable(pi_edit.edit_lines)
    assert list(inspect.signature(pi_edit.apply_patch).parameters)[:2] == [
        "path",
        "hunks",
    ]
    assert list(inspect.signature(pi_edit.edit_lines).parameters)[:2] == [
        "path",
        "ranges",
    ]


def test_ac1_pure_stdlib_no_network_no_swarmforge_imports() -> None:
    """Every import in pi_edit.py must be stdlib: no network, no swarmforge."""
    tree = ast.parse(inspect.getsource(pi_edit))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "pathlib", "typing"}, sorted(imported)
    for forbidden in ("urllib", "socket", "http", "requests", "swarmforge"):
        assert forbidden not in imported


# ------------------------------------------------------------------------ AC2


def _file(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "target.py"
    p.write_bytes(content.encode("utf-8"))
    return p


def test_ac2_anchor_mismatch_rejects_whole_edit_file_byte_identical(
    tmp_path: Path,
) -> None:
    """First hunk is valid, second mismatches: NEITHER may be written."""
    original = b"def f():\n    return x\n\ndef g():\n    return y\n"
    p = _file(tmp_path, original.decode())
    with pytest.raises(EditConflict) as exc:
        pi_edit.apply_patch(
            p,
            [
                {"start": 1, "end": 1, "old": ["def f():"], "text": "def f2():"},
                {"start": 5, "end": 5, "old": ["    return Z"], "text": "    return 0"},
            ],
        )
    msg = str(exc.value)
    assert "line 5" in msg, msg  # helpful: points at the offending line
    assert "'    return y'" in msg and "'    return Z'" in msg  # actual vs expected
    assert p.read_bytes() == original


def test_ac2_anchor_length_mismatch_rejected(tmp_path: Path) -> None:
    original = "a\nb\nc\n"
    p = _file(tmp_path, original)
    with pytest.raises(EditConflict, match="anchor has"):
        pi_edit.apply_patch(p, [{"start": 1, "end": 3, "old": ["a"], "text": "x"}])
    assert p.read_bytes() == original.encode()


def test_ac2_overlapping_hunks_rejected_file_unchanged(tmp_path: Path) -> None:
    original = "a\nb\nc\n"
    p = _file(tmp_path, original)
    with pytest.raises(EditConflict):
        pi_edit.apply_patch(
            p,
            [
                {"start": 1, "end": 2, "text": "X"},
                {"start": 2, "end": 3, "text": "Y"},
            ],
        )
    assert p.read_bytes() == original.encode()


def test_ac2_out_of_range_hunks_rejected_file_unchanged(tmp_path: Path) -> None:
    original = "a\nb\n"
    p = _file(tmp_path, original)
    for bad in (
        {"start": 0, "end": 1, "text": "x"},
        {"start": 1, "end": 9, "text": "x"},
    ):
        with pytest.raises(EditConflict, match="out of bounds"):
            pi_edit.apply_patch(p, [bad])
    assert p.read_bytes() == original.encode()


def test_ac2_success_writes_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """A valid multi-hunk patch results in a single atomic write."""
    calls: list[str] = []
    original_write = Path.write_text

    def counting_write(self: Path, data, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(self))
        return original_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", counting_write)
    p = _file(tmp_path, "1\n2\n3\n4\n5\n")
    msg = pi_edit.apply_patch(
        p,
        [
            {"start": 2, "end": 2, "old": ["2"], "text": "two"},
            {"start": 4, "end": 5, "old": ["4", "5"], "text": "four-five"},
        ],
    )
    assert len(calls) == 1, calls
    assert p.read_text(encoding="utf-8") == "1\ntwo\n3\nfour-five\n"
    assert "applied 2 hunk(s)" in msg


def test_ac2_failed_edit_never_writes(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    original_write = Path.write_text

    def counting_write(self: Path, data, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(self))
        return original_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", counting_write)
    p = _file(tmp_path, "a\nb\n")
    with pytest.raises(EditConflict):
        pi_edit.apply_patch(p, [{"start": 1, "end": 1, "old": ["WRONG"], "text": "A"}])
    assert calls == [], calls


# ------------------------------------------------------------------------ AC3

_TOOLS = ("edit_lines", "apply_patch")


@pytest.mark.parametrize("name", _TOOLS)
def test_ac3_registered_in_all_tool_tables(name: str) -> None:
    assert name in PR.SF_TOOLS, "must be offered to Pi agents through sf"
    assert name in ASYNC_IMPLS
    assert name in TOOL_SCHEMAS
    assert name in WRITE_TOOLS
    schema = TOOL_SCHEMAS[name]
    fn = schema["function"]
    assert fn["name"] == name and schema["type"] == "function"
    assert "path" in fn["parameters"]["required"]


def test_ac3_sf_edit_command_end_to_end(tmp_path: Path) -> None:
    """`sf edit PATH --lines A-B --text T` parses and runs the edit_lines tool."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    tool, args = SF.parse(["edit", "app.py", "--lines", "2-3", "--text", "B\nC"])
    assert tool == "edit_lines"
    ctx = ToolContext(
        workspace=ws,
        sandbox=LocalSandbox(SandboxConfig(mode="local")),
        artifacts_dir=tmp_path / "artifacts",
    )
    msg = _run(ctx, tool, args)
    assert (ws / "app.py").read_text(encoding="utf-8") == "a\nB\nC\nd\n"
    assert str(ws) not in msg and "app.py" in msg  # path shown relative to workspace
    assert ctx.files_written == {"app.py"}


def test_ac3_sf_patch_command_end_to_end_hunks_and_diff(tmp_path: Path) -> None:
    """`sf patch` accepts hunks JSON and a unified diff and runs apply_patch."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    ctx = ToolContext(
        workspace=ws,
        sandbox=LocalSandbox(SandboxConfig(mode="local")),
        artifacts_dir=tmp_path / "artifacts",
    )
    hunks = [{"start": 2, "end": 2, "old": ["b"], "text": "B"}]
    tool, args = SF.parse(["patch", "app.py", json.dumps(hunks)])
    assert tool == "apply_patch" and args["hunks"] == hunks
    _run(ctx, tool, args)
    assert (ws / "app.py").read_text(encoding="utf-8") == "a\nB\nc\nd\n"

    diff = "--- a/app.py\n+++ b/app.py\n@@ -3,2 +3,2 @@\n-c\n+C\n d\n"
    tool, args = SF.parse(["patch", "app.py", diff])
    assert tool == "apply_patch" and args["hunks"] == diff
    _run(ctx, tool, args)
    assert (ws / "app.py").read_text(encoding="utf-8") == "a\nB\nC\nd\n"


def test_ac3_sf_patch_mismatch_reports_error_not_crash(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("keep\n", encoding="utf-8")
    ctx = ToolContext(
        workspace=ws,
        sandbox=LocalSandbox(SandboxConfig(mode="local")),
        artifacts_dir=tmp_path / "artifacts",
    )
    bad = [{"start": 1, "end": 1, "old": ["gone"], "text": "x"}]
    tool, args = SF.parse(["patch", "app.py", json.dumps(bad)])
    with pytest.raises(ToolError, match="does not match"):
        _run(ctx, tool, args)
    assert (ws / "app.py").read_text(encoding="utf-8") == "keep\n"
    assert ctx.files_written == set()


def _run(ctx: ToolContext, tool: str, args: dict) -> str:
    import asyncio

    return asyncio.run(ASYNC_IMPLS[tool](ctx, **args))


# ------------------------------------------------------------------------ AC5


def _pi_readme_section() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    heading = "## Pi (pi.dev) as the developer harness"
    assert heading in readme, "README must keep the Pi harness section"
    rest = readme.split(heading, 1)[1]
    return rest.split("\n## ", 1)[0]


def test_ac5_readme_documents_targeted_edit_workflow() -> None:
    section = _pi_readme_section()
    for needle in (
        "sf edit",
        "sf patch",
        "pi_edit.py",
        "apply_patch",
        "edit_lines",
        "anchor",
        "atomic",
    ):
        assert needle in section, f"README Pi section missing {needle!r}"


def test_ac5_pi_developer_prompt_documents_targeted_edit_workflow() -> None:
    s = prompts.PI_DEVELOPER_SYSTEM
    assert "sf edit" in s and "sf patch" in s
    assert "anchor" in s.lower() and "atomic" in s.lower()
    # the whole point of the issue: steer agents away from full-file rewrites
    assert "rewriting whole files" in s or "full-file rewrites" in s
