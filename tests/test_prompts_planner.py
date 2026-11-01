"""Tests that the planner prompts request atomic tasks."""

from swarmforge import prompts


def test_planner_system_mentions_atomic():
    assert "ATOMIC" in prompts.PLANNER_SYSTEM


def test_planner_system_at_most_3_files():
    assert "at most 3 files" in prompts.PLANNER_SYSTEM


def test_planner_system_mentions_split():
    assert "split" in prompts.PLANNER_SYSTEM.lower()


def test_fix_planner_system_atomic():
    assert "atomic" in prompts.FIX_PLANNER_SYSTEM.lower()


def test_planner_system_keeps_read_only_tools():
    for name in ("outline", "find_symbol", "git_changes", "git_history"):
        assert name in prompts.PLANNER_SYSTEM


# --- guard: every tool name referenced by the prompts must be registered ---

import re

from swarmforge.agent import tools as agent_tools

PROMPT_REFERENCED_TOOLS = (
    "start_process",
    "read_process_output",
    "stop_process",
    "browse",
    "check_code",
    "run_tests",
    "install_dependencies",
    "outline",
    "find_symbol",
    "git_changes",
    "git_history",
    "http_request",
    "format_code",
    "list_packages",
)

# Words that can appear where a tool name would but are not tools.
_NON_TOOL_WORDS = {"same"}


def test_prompt_referenced_tools_are_registered():
    for name in PROMPT_REFERENCED_TOOLS:
        assert name in agent_tools.TOOL_SCHEMAS, f"{name} missing from TOOL_SCHEMAS"
        assert name in agent_tools.ASYNC_IMPLS, f"{name} missing from ASYNC_IMPLS"


def _bullet_tool_names(text: str) -> set[str]:
    """Extract tool identifiers from '- <name>: ...' / '- <a> / <b>: ...' bullets."""
    names: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^- ([a-z_][a-z0-9_]*(?: / [a-z_][a-z0-9_]*)*):", line)
        if not match:
            continue
        for name in match.group(1).split(" / "):
            if name not in _NON_TOOL_WORDS:
                names.add(name)
    return names


def test_developer_and_tester_prompts_have_no_unregistered_tool_names():
    for prompt_name, prompt in (
        ("DEVELOPER_SYSTEM", prompts.DEVELOPER_SYSTEM),
        ("TESTER_SYSTEM", prompts.TESTER_SYSTEM),
    ):
        for name in sorted(_bullet_tool_names(prompt)):
            assert name in agent_tools.TOOL_SCHEMAS, (
                f"{prompt_name} references unregistered tool {name!r}"
            )
