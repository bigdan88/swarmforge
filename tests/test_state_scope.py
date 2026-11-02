"""Tests for scope filtering of repair findings in state.py."""

import pytest

from swarmforge.state import (
    normalize_scope_path,
    out_of_scope_findings,
    repair_tasks_from_findings,
)

SCOPE = {"src/swarmforge/dashboard.py", "src/swarmforge/cli.py"}


def finding(file, title="t", sev="major", desc="d"):
    return {"severity": sev, "file": file, "title": title, "description": desc}


class TestNormalizeScopePath:
    def test_variants(self):
        assert normalize_scope_path("./a/b.py") == "a/b.py"
        assert normalize_scope_path("a\\b.py") == "a/b.py"
        assert normalize_scope_path("/a/b.py") == "a/b.py"
        assert normalize_scope_path(None) == ""
        assert normalize_scope_path("   ") == ""
        assert normalize_scope_path("  a.py ") == "a.py"


class TestOutOfScopeFindings:
    def test_drops_out_of_scope(self):
        f1 = finding("src/swarmforge/repos.py", "F10")
        f2 = finding("src/swarmforge/resources/Dockerfile.sandbox", "F11")
        fin = finding("src/swarmforge/dashboard.py", "F1")
        out = out_of_scope_findings([f1, f2, fin], SCOPE)
        assert out == [f1, f2]

    def test_non_blocking_never_out_of_scope(self):
        f = finding("other/x.py", sev="minor")
        assert out_of_scope_findings([f], SCOPE) == []

    def test_empty_or_none_scope(self):
        f = finding("other/x.py")
        assert out_of_scope_findings([f], None) == []
        assert out_of_scope_findings([f], set()) == []
        assert out_of_scope_findings([f], ("", "  ")) == []

    def test_missing_file_kept(self):
        f = finding(None)
        assert out_of_scope_findings([f], SCOPE) == []

    def test_directory_prefix(self):
        f = finding("docs/x.md")
        assert out_of_scope_findings([f], {"docs/"}) == []

    def test_dedup_respected(self):
        f1 = finding("other/x.py", "dup")
        f2 = finding("other/x.py", "dup")
        out = out_of_scope_findings([f1, f2], SCOPE)
        assert out == [f1]

    def test_severities_kwarg(self):
        f = finding("other/x.py", sev="minor")
        assert out_of_scope_findings([f], SCOPE, severities={"minor"}) == [f]


class TestRepairTasksScope:
    def test_scope_filters_tasks(self):
        f1 = finding("src/swarmforge/dashboard.py", "in")
        f2 = finding("src/swarmforge/repos.py", "out")
        tasks = repair_tasks_from_findings([f1, f2], ["dev"], 2, scope_files=SCOPE)
        assert [t.id for t in tasks] == ["R2F1"]
        assert tasks[0].title == "in"
        assert tasks[0].role == "dev"
        assert tasks[0].expected_files == ["src/swarmforge/dashboard.py"]

    def test_no_valueerror_when_all_out_of_scope(self):
        f = finding("src/swarmforge/repos.py")
        assert repair_tasks_from_findings([f], [], 1, scope_files=SCOPE) == []

    def test_valueerror_for_in_scope_blocking(self):
        f = finding("src/swarmforge/dashboard.py")
        with pytest.raises(ValueError):
            repair_tasks_from_findings([f], [], 1, scope_files=SCOPE)

    def test_regression_without_scope(self):
        f1 = finding("a.py", "one")
        f2 = finding("b.py", "two")
        tasks = repair_tasks_from_findings([f1, f2], ["dev", "rev"], 3)
        assert [t.id for t in tasks] == ["R3F1", "R3F2"]
        assert [t.role for t in tasks] == ["dev", "rev"]

    def test_regression_empty_roles_no_blocking(self):
        assert repair_tasks_from_findings([finding("a.py", sev="minor")], [], 1) == []
