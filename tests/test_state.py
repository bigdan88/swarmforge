"""Tests for swarmforge.state.repair_tasks_from_findings."""
import pytest

from swarmforge.plan import ID_RE
from swarmforge.state import repair_tasks_from_findings


def finding(severity="major", file="src/app.py", line=10, title="crash",
            description="budget_usd is NULL crashes explain_run",
            recommendation="use `or 0`"):
    return {"severity": severity, "file": file, "line": line, "title": title,
            "description": description, "recommendation": recommendation}


def test_three_major_findings_create_three_tasks():
    findings = [
        finding(title=f"finding {i}", description=f"exact text {i}", line=i)
        for i in range(1, 4)
    ]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1)
    assert len(tasks) == 3
    assert [t.id for t in tasks] == ["R1F1", "R1F2", "R1F3"]
    for i, t in enumerate(tasks, start=1):
        assert t.depends_on == []
        assert t.title == f"finding {i}"
        assert f"exact text {i}" in t.description
        assert f"src/app.py:{i}" in t.description
        assert "MANDATORY repair for reviewer finding" in t.description
        assert "cannot be skipped" in t.description
        assert len(t.acceptance_criteria) == 1
        assert f'"exact text {i}"' in t.acceptance_criteria[0]
        assert ID_RE.match(t.id)
        assert t.expected_files == ["src/app.py"]


def test_severity_filtering():
    findings = [
        finding(severity="minor", title="m"),
        finding(severity="info", title="i"),
        finding(severity="critical", title="c"),
        finding(severity="major", title="M"),
    ]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=2)
    assert [t.title for t in tasks] == ["c", "M"]
    # custom severities drop criticals
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=2, severities={"major"})
    assert [t.title for t in tasks] == ["M"]


def test_duplicate_findings_deduplicated():
    findings = [finding(), finding(), finding(title="other")]
    tasks = repair_tasks_from_findings(findings, ["dev"], cycle=1)
    assert len(tasks) == 2


def test_roles_round_robin():
    findings = [finding(title=f"f{i}") for i in range(3)]
    tasks = repair_tasks_from_findings(
        findings, ["developer_backend", "developer_frontend"], cycle=1)
    assert [t.role for t in tasks] == [
        "developer_backend", "developer_frontend", "developer_backend"]


def test_empty_findings_and_empty_roles():
    assert repair_tasks_from_findings([], ["dev"], cycle=1) == []
    assert repair_tasks_from_findings([finding(severity="minor")], [], cycle=1) == []
    with pytest.raises(ValueError):
        repair_tasks_from_findings([finding()], [], cycle=1)


def test_missing_file_and_line():
    f = finding()
    f["file"] = None
    f["line"] = None
    tasks = repair_tasks_from_findings([f], ["dev"], cycle=1)
    assert len(tasks) == 1
    assert "File: unknown" in tasks[0].description
    assert tasks[0].expected_files == []
    f2 = finding(file="a.py", line=None)
    tasks = repair_tasks_from_findings([f2], ["dev"], cycle=1)
    assert "File: a.py" in tasks[0].description
