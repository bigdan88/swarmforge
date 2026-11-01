"""Acceptance tests: auto-split large planner tasks into <=3-file sub-tasks.

Independent verification of the deterministic post-pass in src/swarmforge/plan.py
(invoked from validate_plan) and of the atomic-task wording in prompts.py.
Maps to AC1-AC6 of the issue.
"""

from __future__ import annotations

import pytest

from swarmforge import prompts
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


def _raw(i, deps=(), files=None, desc="plain work", role="developer_backend"):
    return {
        "id": i,
        "title": f"task {i}",
        "description": desc,
        "role": role,
        "depends_on": list(deps),
        "acceptance_criteria": [f"{i} is done"],
        "expected_files": files if files is not None else [f"{i}.py"],
    }


def _plan(tasks, ac=True):
    return {
        "summary": "s",
        "tasks": tasks,
        "acceptance_criteria": [{"id": "AC1", "text": "works"}] if ac else [],
    }


def _flat_wave_order(p):
    return [tid for wi, w in enumerate(p.waves()) for tid in w if wi >= 0]


# ---------------------------------------------------------------------- AC1
class TestAC1FileDrivenSplit:
    def test_four_file_task_split_via_validate_plan(self):
        files = ["a.py", "b.py", "c.py", "d.py"]
        p = validate_plan(_plan([_raw("T2", files=files)]), DEV, 8)
        assert [t.id for t in p.tasks] == ["T2-1", "T2-2"]
        assert all(len(t.expected_files) <= MAX_FILES_PER_TASK for t in p.tasks)
        # no file is lost or duplicated by the split
        assert sorted(f for t in p.tasks for f in t.expected_files) == sorted(files)

    def test_five_file_task_split_into_two_parts(self):
        files = [f"f{i}.py" for i in range(5)]
        p = validate_plan(_plan([_raw("T2", files=files)]), DEV, 8)
        assert [t.id for t in p.tasks] == ["T2-1", "T2-2"]
        assert p.task("T2-1").expected_files == files[:3]
        assert p.task("T2-2").expected_files == files[3:]

    def test_seven_file_task_split_into_three_parts(self):
        files = [f"g{i}.py" for i in range(7)]
        p = validate_plan(_plan([_raw("T9", files=files)]), DEV, 8)
        assert 2 <= len(p.tasks) <= 3
        assert [t.id for t in p.tasks] == ["T9-1", "T9-2", "T9-3"]
        for t in p.tasks:
            assert 1 <= len(t.expected_files) <= MAX_FILES_PER_TASK
        assert sorted(f for t in p.tasks for f in t.expected_files) == sorted(files)

    def test_split_subtasks_preserve_role_and_criteria(self):
        files = [f"f{i}.py" for i in range(4)]
        p = validate_plan(
            _plan([_raw("T2", files=files, role="developer_frontend")]), DEV, 8
        )
        for t in p.tasks:
            assert t.role == "developer_frontend"
            assert t.acceptance_criteria == ["T2 is done"]
            assert "Original instructions: plain work" in t.description


# ---------------------------------------------------------------------- AC2
class TestAC2WidgetDrivenSplit:
    def test_three_widget_categories_trigger_split(self):
        desc = "Add date filters, export buttons and a settings panel to the dashboard."
        assert len(mentioned_widgets(desc)) >= 3
        p = validate_plan(
            _plan([_raw("T3", files=["dashboard.py"], desc=desc)]), DEV, 8
        )
        assert [t.id for t in p.tasks] == ["T3-1", "T3-2"]
        # widget-driven split keeps the (small) file list whole in every part
        for t in p.tasks:
            assert t.expected_files == ["dashboard.py"]
        # each sub-task description scopes a subset of the widgets
        scopes = [t.description for t in p.tasks]
        assert all("Your scope:" in s for s in scopes)
        assert any("panel" in s for s in scopes)

    def test_two_widget_categories_do_not_split(self):
        desc = "Add filters and buttons to the toolbar."
        assert len(mentioned_widgets(desc)) == 2
        p = validate_plan(_plan([_raw("T4", files=["ui.py"], desc=desc)]), DEV, 8)
        assert [t.id for t in p.tasks] == ["T4"]

    def test_five_widget_categories_split_into_three_parts(self):
        desc = "Build a sidebar with tabs, a chart, a table, a form and a search box."
        assert len(mentioned_widgets(desc)) >= 5
        subs = split_task(
            Task(
                id="T8", title="t", description=desc, role="r", expected_files=["ui.py"]
            )
        )
        assert [s.id for s in subs] == ["T8-1", "T8-2", "T8-3"]


# ---------------------------------------------------------------------- AC3
class TestAC3DependencyChain:
    def test_chain_inheritance_and_rewiring(self):
        files = [f"f{i}.py" for i in range(5)]
        p = validate_plan(
            _plan(
                [
                    _raw("T1"),
                    _raw("T2", deps=["T1"], files=files),
                    _raw("T3", deps=["T2"]),
                    _raw("T4", deps=["T1", "T2"]),
                ]
            ),
            DEV,
            8,
        )
        # first sub-task inherits the original depends_on; later ones chain
        assert p.task("T2-1").depends_on == ["T1"]
        assert p.task("T2-2").depends_on == ["T2-1"]
        # other tasks that depended on the original id are rewired to the last sub-task
        assert p.task("T3").depends_on == ["T2-2"]
        assert p.task("T4").depends_on == ["T1", "T2-2"]
        # all ids pass ID_RE and the plan stays acyclic/valid
        for t in p.tasks:
            assert ID_RE.match(t.id), t.id
        waves = p.waves()
        assert waves == [["T1"], ["T2-1"], ["T2-2"], ["T3", "T4"]]
        flat = _flat_wave_order(p)
        assert (
            len(flat) == len(set(flat)) == len(p.tasks)
        )  # every task is scheduled once
        pos = {tid: i for i, tid in enumerate(flat)}
        for t in p.tasks:
            for d in t.depends_on:
                assert pos[d] < pos[t.id], f"{t.id} not after its dependency {d}"

    def test_multi_dependency_inherited_by_first_subtask(self):
        files = [f"f{i}.py" for i in range(5)]
        p = validate_plan(
            _plan([_raw("A"), _raw("B"), _raw("T", deps=["A", "B"], files=files)]),
            DEV,
            8,
        )
        assert p.task("T-1").depends_on == ["A", "B"]
        assert p.task("T-2").depends_on == ["T-1"]

    def test_two_chained_split_tasks_rewire_transitively(self):
        f5 = [f"a{i}.py" for i in range(5)]
        f4 = ["b.py", "c.py", "d.py", "e.py"]
        p = validate_plan(
            _plan([_raw("T1", files=f5), _raw("T2", deps=["T1"], files=f4)]), DEV, 8
        )
        assert p.task("T1-2").depends_on == ["T1-1"]
        # T2-1 inherited T2's depends_on ["T1"], which the post-pass rewires to T1-2
        assert p.task("T2-1").depends_on == ["T1-2"]
        assert p.task("T2-2").depends_on == ["T2-1"]
        flat = _flat_wave_order(p)
        assert flat.index("T2-1") > flat.index("T1-2")

    def test_split_task_directly_returns_valid_chain(self):
        files = [f"f{i}.py" for i in range(4)]
        subs = split_task(
            Task(
                id="T7",
                title="t",
                description="d",
                role="r",
                depends_on=["T0"],
                acceptance_criteria=["ac"],
                expected_files=files,
            )
        )
        assert [s.id for s in subs] == ["T7-1", "T7-2"]
        assert subs[0].depends_on == ["T0"]
        assert subs[1].depends_on == ["T7-1"]
        assert all(s.acceptance_criteria == ["ac"] for s in subs)
        assert all(ID_RE.match(s.id) for s in subs)

    def test_widget_split_chain_is_sequential(self):
        desc = "Add filters, buttons and a panel."
        subs = split_task(
            Task(
                id="W1", title="t", description=desc, role="r", expected_files=["ui.py"]
            )
        )
        assert [s.depends_on for s in subs] == [[], ["W1-1"]]


# ---------------------------------------------------------------------- AC4
class TestAC4SmallTaskUntouched:
    def test_three_file_two_widget_task_identical(self):
        desc = "Wire the filter and the buttons in ui.py."
        raw = _raw("T1", files=["a.py", "b.py", "c.py"], desc=desc)
        p = validate_plan(_plan([raw]), DEV, 8)
        assert len(p.tasks) == 1
        t = p.tasks[0]
        assert t.id == "T1"
        assert t.title == raw["title"]
        assert t.description == desc  # not rewritten with split boilerplate
        assert t.depends_on == []
        assert t.acceptance_criteria == raw["acceptance_criteria"]
        assert t.expected_files == raw["expected_files"]

    def test_auto_split_plan_returns_same_plan_when_nothing_to_split(self):
        tasks = [
            Task(
                id="T1", title="t", description="d", role="r", expected_files=["a.py"]
            ),
            Task(
                id="T2",
                title="t",
                description="d",
                role="r",
                depends_on=["T1"],
                expected_files=["b.py", "c.py"],
            ),
        ]
        plan = Plan(summary="s", tasks=tasks)
        assert auto_split_plan(plan) is plan
        assert plan.tasks == tasks

    def test_needs_split_exact_thresholds(self):
        at_limit = Task(
            id="T1",
            title="t",
            description="a button and a tab",
            role="r",
            expected_files=["a.py", "b.py", "c.py"],
        )
        assert not needs_split(at_limit)
        over_files = Task(
            id="T2",
            title="t",
            description="plain",
            role="r",
            expected_files=["a.py", "b.py", "c.py", "d.py"],
        )
        assert needs_split(over_files)
        over_widgets = Task(
            id="T3",
            title="t",
            description="a menu, a modal and a dialog",
            role="r",
            expected_files=["a.py"],
        )
        assert needs_split(over_widgets)


# ---------------------------------------------------------------------- AC5
class TestAC5PromptsRequestAtomicTasks:
    def test_planner_system_requests_atomic_tasks(self):
        text = prompts.PLANNER_SYSTEM
        assert "ATOMIC" in text
        assert "at most 3 files" in text
        assert "widgets" in text.lower()
        assert "sub-tasks" in text and "depends_on" in text

    def test_fix_planner_system_requests_atomic_tasks(self):
        text = prompts.FIX_PLANNER_SYSTEM
        assert "atomic" in text.lower()
        assert "at most 3 files" in text

    def test_prompts_keep_content_existing_tests_assert_on(self):
        # tests/test_navtools.py::test_prompts_document_the_new_tools requires the
        # read-only nav tool names to stay documented in PLANNER_SYSTEM.
        for name in ("outline", "find_symbol", "git_changes", "git_history"):
            assert name in prompts.PLANNER_SYSTEM, f"PLANNER_SYSTEM missing {name}"
        # planner must still instruct the submit_plan workflow
        assert "submit_plan" in prompts.PLANNER_SYSTEM
        assert "submit_plan" in prompts.FIX_PLANNER_SYSTEM


# ---------------------------------------------------------------------- AC6
class TestAC6ValidationBehaviourPreserved:
    def test_invalid_plans_still_rejected_before_split(self):
        with pytest.raises(PlanValidationError, match="must be one of"):
            validate_plan(_plan([_raw("T1", role="hacker")]), DEV, 8)
        with pytest.raises(PlanValidationError, match="cycle"):
            validate_plan(
                _plan([_raw("T1", deps=["T2"]), _raw("T2", deps=["T1"])]), DEV, 8
            )
        with pytest.raises(PlanValidationError, match="unknown task"):
            validate_plan(_plan([_raw("T1", deps=["T9"])]), DEV, 8)
        with pytest.raises(PlanValidationError, match="duplicated"):
            validate_plan(_plan([_raw("T1"), _raw("T1")]), DEV, 8)
        with pytest.raises(PlanValidationError, match="relative path"):
            validate_plan(_plan([_raw("T1", files=["../etc/passwd"])]), DEV, 8)
        with pytest.raises(PlanValidationError, match="too many"):
            validate_plan(_plan([_raw(f"T{i}") for i in range(5)]), DEV, 3)

    def test_valid_plain_plan_waves_unchanged(self):
        p = validate_plan(
            _plan(
                [
                    _raw("T1"),
                    _raw("T2", role="developer_frontend"),
                    _raw("T3", deps=["T1", "T2"]),
                ]
            ),
            DEV,
            8,
        )
        assert p.waves() == [["T1", "T2"], ["T3"]]

    def test_fix_plan_may_depend_on_existing_and_rewires_when_split(self):
        files = [f"f{i}.py" for i in range(5)]
        p = validate_plan(
            _plan(
                [
                    _raw("T2", files=files),
                    _raw("F1", deps=["T2", "T1"], files=["fix.py"]),
                ],
                ac=False,
            ),
            DEV,
            8,
            existing_ids={"T1"},
            require_criteria=False,
        )
        # split T2 -> T2-1/T2-2; F1's reference to T2 rewired, external existing id kept,
        # original dependency order preserved
        assert p.task("F1").depends_on == ["T2-2", "T1"]
        assert [t.id for t in p.tasks] == ["T2-1", "T2-2", "F1"]


# ------------------------------------------------- known edge-case defects
class TestKnownEdgeCaseDefects:
    """Reproductions for two edge cases where the post-pass violates the letter
    of AC3 ('sub-task ids pass ID_RE, and Plan.waves() stays acyclic/valid').
    Marked xfail so the suite stays green while the defects are reported."""

    @pytest.mark.xfail(
        reason="defect: a 32-char task id (max ID_RE length) produces "
        "33/34-char sub-task ids that fail ID_RE",
        strict=False,
    )
    def test_long_id_subtasks_still_match_id_re(self):
        long_id = "A" + "b" * 31  # 32 chars: valid under ID_RE
        assert ID_RE.match(long_id)
        files = [f"f{i}.py" for i in range(5)]
        p = validate_plan(_plan([_raw(long_id, files=files)]), DEV, 8)
        assert all(ID_RE.match(t.id) for t in p.tasks)

    @pytest.mark.xfail(
        reason="defect: splitting T2 when a task named T2-1 already exists "
        "yields duplicate ids; waves() silently drops one task",
        strict=False,
    )
    def test_split_does_not_duplicate_existing_subtask_ids(self):
        files = [f"f{i}.py" for i in range(5)]
        p = validate_plan(_plan([_raw("T2", files=files), _raw("T2-1")]), DEV, 8)
        ids = [t.id for t in p.tasks]
        assert len(ids) == len(set(ids)), f"duplicate task ids: {ids}"
        flat = _flat_wave_order(p)
        assert len(flat) == len(p.tasks), "waves() must schedule every task"
