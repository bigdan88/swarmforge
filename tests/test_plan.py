"""Tests for the deterministic auto-split post-pass in swarmforge.plan."""
import pytest

from swarmforge.plan import (
    ID_RE,
    MAX_FILES_PER_TASK,
    Plan,
    PlanValidationError,
    Task,
    auto_split_plan,
    mentioned_widgets,
    needs_split,
    split_task,
    validate_plan,
)

DEV = ["developer_backend", "developer_frontend"]


def _plan(tasks, ac=True):
    return {"summary": "s", "tasks": tasks,
            "acceptance_criteria": [{"id": "AC1", "text": "works"}] if ac else []}


def _t(i, deps=(), role="developer_backend", files=None, desc="d"):
    return {"id": i, "title": i, "description": desc, "role": role, "depends_on": list(deps),
            "acceptance_criteria": ["x"], "expected_files": files or [f"{i}.py"]}


def test_five_file_task_is_split():
    files = [f"f{i}.py" for i in range(5)]
    p = validate_plan(_plan([_t("T2", files=files, role="developer_frontend")]), DEV, 8)
    assert [t.id for t in p.tasks] == ["T2-1", "T2-2"]
    assert p.task("T2-1").expected_files == files[:3]
    assert p.task("T2-2").expected_files == files[3:]
    assert p.task("T2-2").depends_on == ["T2-1"]


def test_small_task_left_alone():
    raw = _t("T1", files=["a.py", "b.py"])
    p = validate_plan(_plan([raw]), DEV, 8)
    assert len(p.tasks) == 1
    t = p.tasks[0]
    assert t.id == "T1"
    assert t.title == "T1"
    assert t.description == "d"
    assert t.expected_files == ["a.py", "b.py"]
    assert t.depends_on == []
    assert t.acceptance_criteria == ["x"]


def test_split_dependency_edges_and_waves():
    files = [f"f{i}.py" for i in range(5)]
    p = validate_plan(_plan([
        _t("T1"),
        _t("T2", deps=["T1"], files=files),
        _t("T3", deps=["T2"]),
    ]), DEV, 8)
    t21, t22 = p.task("T2-1"), p.task("T2-2")
    assert t21.depends_on == ["T1"]
    assert t22.depends_on == ["T2-1"]
    assert p.task("T3").depends_on == ["T2-2"]
    assert p.waves() == [["T1"], ["T2-1"], ["T2-2"], ["T3"]]


def test_widget_trigger_split():
    desc = "Add filters, archive buttons and a disk panel to the dashboard."
    assert mentioned_widgets(desc) == ["button", "filter", "panel"]
    p = validate_plan(_plan([_t("T2", files=["ui.py"], desc=desc)]), DEV, 8)
    assert [t.id for t in p.tasks] == ["T2-1", "T2-2"]
    for t in p.tasks:
        assert t.expected_files == ["ui.py"]
    assert "filters" in p.task("T2-1").description
    assert "button" in p.task("T2-2").description


def test_mentioned_widgets_word_boundaries():
    assert mentioned_widgets("tables and tabs") == ["tab", "table"]
    assert mentioned_widgets("no widgets here") == []
    assert mentioned_widgets("Filter FILTERS filtering") == ["filter"]


def test_subtask_ids_and_descriptions():
    files = [f"f{i}.py" for i in range(4)]
    subs = split_task(Task(id="T9", title="t", description="d", role="developer_backend",
                           expected_files=files))
    assert len(subs) == 2
    for s in subs:
        assert ID_RE.match(s.id)
        assert "Original instructions:" in s.description
        assert s.role == "developer_backend"
        assert s.acceptance_criteria == []


def test_three_parts_for_very_large_tasks():
    files = [f"f{i}.py" for i in range(7)]
    subs = split_task(Task(id="T1", title="t", description="d", role="developer_backend",
                           expected_files=files))
    assert [s.id for s in subs] == ["T1-1", "T1-2", "T1-3"]
    assert [len(s.expected_files) for s in subs] == [3, 2, 2]
    assert subs[2].depends_on == ["T1-2"]


def test_corrective_plan_with_existing_ids():
    files = [f"f{i}.py" for i in range(5)]
    p = validate_plan(_plan([_t("F1", deps=["T2"], files=["fix.py"])], ac=False),
                      DEV, 8, existing_ids={"T2"}, require_criteria=False)
    # no split needed here; now with a split task in the same plan
    p = validate_plan(_plan([_t("T2", files=files), _t("F1", deps=["T2"], files=["fix.py"])], ac=False),
                      DEV, 8, existing_ids={"T1"}, require_criteria=False)
    assert p.task("F1").depends_on == ["T2-2"]
    assert p.waves() == [["T2-1"], ["T2-2"], ["F1"]]


def test_auto_split_plan_noop():
    tasks = [Task(id="T1", title="t", description="d", role="developer_backend",
                  expected_files=["a.py"]),
             Task(id="T2", title="t", description="d", role="developer_backend",
                  expected_files=["b.py"], depends_on=["T1"])]
    plan = auto_split_plan(Plan(summary="s", tasks=tasks))
    assert plan.tasks == tasks


def test_validation_errors_still_raised():
    with pytest.raises(PlanValidationError, match="must be one of"):
        validate_plan(_plan([_t("T1", role="hacker")]), DEV, 8)


def test_needs_split_thresholds():
    small = Task(id="T1", title="t", description="plain", role="r",
                 expected_files=["a.py", "b.py", "c.py"])
    assert not needs_split(small)
    big = Task(id="T2", title="t", description="plain", role="r",
               expected_files=["a.py", "b.py", "c.py", "d.py"])
    assert needs_split(big)
    widgets = Task(id="T3", title="t", description="a button, a tab and a menu", role="r",
                   expected_files=["a.py"])
    assert needs_split(widgets)
    assert MAX_FILES_PER_TASK == 3
