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
