"""Tests for the deterministic repair-cycle scope lock in state.py."""

from __future__ import annotations

import pytest

from swarmforge.state import (
    finding_in_scope,
    normalize_scope_path,
    out_of_scope_findings,
    repair_tasks_from_findings,
)

THEME_SCOPE = {"src/swarmforge/theme.py", "src/swarmforge/dashboard.py"}


def finding(
    severity="major",
    file="src/app.py",
    line=10,
    title="crash",
    description="budget_usd is NULL crashes explain_run",
    recommendation="use `or 0`",
):
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "title": title,
        "description": description,
        "recommendation": recommendation,
    }


# --- normalize_scope_path ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        (" a.py ", "a.py"),
        ("./a.py", "a.py"),
        ("/a.py", "a.py"),
        ("./a\\b.py", "a/b.py"),
        (".\\.\\src/x.py", "src/x.py"),
        ("src\\swarmforge\\theme.py", "src/swarmforge/theme.py"),
    ],
)
def test_normalize_scope_path(raw, expected):
    assert normalize_scope_path(raw) == expected


# --- finding_in_scope -------------------------------------------------------


def test_finding_in_scope_fails_open():
    assert finding_in_scope(finding(), None)
    assert finding_in_scope(finding(), set())


def test_finding_in_scope_exact_match():
    assert finding_in_scope(finding(file="src/swarmforge/theme.py"), THEME_SCOPE)
    assert not finding_in_scope(finding(file="src/swarmforge/repos.py"), THEME_SCOPE)
    assert not finding_in_scope(finding(file="Dockerfile"), THEME_SCOPE)


def test_finding_in_scope_directory_prefix():
    assert finding_in_scope(finding(file="docs/guide.md"), {"docs/"})
    assert not finding_in_scope(finding(file="docsx/guide.md"), {"docs/"})


def test_finding_in_scope_no_file_stays_in_scope():
    assert finding_in_scope({"severity": "major", "file": None}, THEME_SCOPE)
    assert finding_in_scope({"severity": "major", "file": "  "}, THEME_SCOPE)


def test_finding_in_scope_normalizes_both_sides():
    assert finding_in_scope(
        finding(file="./src/swarmforge\\theme.py"), {"src/swarmforge/theme.py"}
    )


# --- out_of_scope_findings --------------------------------------------------


def test_out_of_scope_findings_issue_example():
    findings = [
        finding(file="src/swarmforge/repos.py", title="GIT env in repos.py"),
        finding(file="Dockerfile", title="ruff in Dockerfile"),
        finding(file="src/swarmforge/dashboard.py", title="theme switch bug"),
    ]
    out = out_of_scope_findings(findings, THEME_SCOPE)
    assert [f["title"] for f in out] == ["GIT env in repos.py", "ruff in Dockerfile"]


def test_out_of_scope_findings_ignores_minor_severity():
    findings = [finding(severity="minor", file="src/swarmforge/repos.py")]
    assert out_of_scope_findings(findings, THEME_SCOPE) == []


def test_out_of_scope_findings_empty_scope():
    findings = [finding(file="src/swarmforge/repos.py")]
    assert out_of_scope_findings(findings, None) == []
    assert out_of_scope_findings(findings, set()) == []


def test_out_of_scope_findings_preserves_order_and_identity():
    a = finding(file="Dockerfile", title="ruff")
    b = finding(file="src/swarmforge/repos.py", title="git")
    out = out_of_scope_findings([b, a], THEME_SCOPE)
    assert out == [b, a]


# --- repair_tasks_from_findings with scope_files ----------------------------


def test_repair_tasks_scope_drops_and_renumbers():
    findings = [
        finding(file="src/swarmforge/repos.py", title="git env", description="d1"),
        finding(file="src/swarmforge/theme.py", title="theme bug", description="d2"),
        finding(file="Dockerfile", title="ruff", description="d3"),
        finding(file="src/swarmforge/dashboard.py", title="dash bug", description="d4"),
    ]
    tasks = repair_tasks_from_findings(
        findings, ["dev"], cycle=1, scope_files=THEME_SCOPE
    )
    assert [t.id for t in tasks] == ["R1F1", "R1F2"]
    assert [t.title for t in tasks] == ["theme bug", "dash bug"]


def test_repair_tasks_without_scope_unchanged():
    findings = [
        finding(file="src/swarmforge/repos.py", title="git env", description="d1"),
        finding(file="src/swarmforge/theme.py", title="theme bug", description="d2"),
    ]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1)
    assert [t.id for t in tasks] == ["R1F1", "R1F2"]


def test_repair_tasks_empty_scope_fails_open():
    findings = [finding(file="src/swarmforge/repos.py")]
    assert (
        len(repair_tasks_from_findings(findings, ["dev"], cycle=1, scope_files=set()))
        == 1
    )
    assert (
        len(repair_tasks_from_findings(findings, ["dev"], cycle=1, scope_files=None))
        == 1
    )


def test_repair_tasks_only_out_of_scope_with_empty_roles_returns_empty():
    findings = [finding(file="Dockerfile", title="ruff")]
    assert (
        repair_tasks_from_findings(findings, [], cycle=1, scope_files=THEME_SCOPE) == []
    )


def test_repair_tasks_in_scope_blocking_with_empty_roles_raises():
    findings = [finding(file="src/swarmforge/theme.py")]
    with pytest.raises(ValueError):
        repair_tasks_from_findings(findings, [], cycle=1, scope_files=THEME_SCOPE)
