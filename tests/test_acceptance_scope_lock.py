"""Acceptance tests for scope-locking repair-cycle tasks to the original plan's file set.

Complements tests/test_state_scope.py, the scope tests appended to
tests/test_acceptance_repair_tasks.py, and tests/test_dashboard_scope.py by
covering the gaps:

- AC2: in-scope findings produce repair tasks *byte-identically* to the
  pre-feature behavior (ids, roles, dedup, description/AC text) even when
  out-of-scope findings are interleaved and filtered.
- AC3: skipped findings remain visible in the Review tab's findings list.
- AC5: a plan whose tasks declare no expected_files (empty scope) disables
  filtering; findings with a missing/empty file field are always kept;
  path-style variants (backslashes, './', leading '/') count as in scope.
"""

from dataclasses import asdict

import pytest

from swarmforge.state import out_of_scope_findings, repair_tasks_from_findings

from .test_acceptance_repair_tasks import _orch_with_run, _small_plan, make_finding

pytest.importorskip("streamlit")

THEME_SCOPE = {"src/swarmforge/theme.py", "src/swarmforge/cli.py"}


# --------------------------------------------------------------------- AC5
def test_plan_without_expected_files_disables_filtering(cfg):
    """AC5: plan exists but declares no expected_files -> nothing is filtered."""
    orch, store = _orch_with_run(cfg)
    orch.plan = _small_plan(orch, set())
    assert orch._repair_scope_files() == set()

    findings = [make_finding(i, file=f"src/app{i}.py") for i in (1, 2, 3)]
    review = {"verdict": "request_changes", "summary": "blocking", "findings": findings}
    tasks = orch._repair_tasks_from_review(review, 1)
    assert [t.id for t in tasks] == ["R1F1", "R1F2", "R1F3"]
    assert not [e for e in store.events(orch.run_id) if e["kind"] == "scope"]


def test_findings_without_file_are_always_kept(cfg):
    """AC5: findings with a missing/empty file field are never scope-skipped."""
    orch, store = _orch_with_run(cfg)
    orch.plan = _small_plan(orch, THEME_SCOPE)

    f_none = dict(make_finding(1, file="x"), title="[major] no file")
    f_none["file"] = None
    f_blank = dict(make_finding(2, file="x"), title="[major] blank file")
    f_blank["file"] = "   "
    review = {"verdict": "request_changes", "summary": "blocking",
              "findings": [f_none, f_blank]}
    tasks = orch._repair_tasks_from_review(review, 1)
    assert [t.id for t in tasks] == ["R1F1", "R1F2"]
    assert not [e for e in store.events(orch.run_id) if e["kind"] == "scope"]

    # state-level helper agrees
    assert out_of_scope_findings([f_none, f_blank], THEME_SCOPE) == []


def test_path_style_variants_are_in_scope(cfg):
    """AC1/AC5 robustness: './', leading '/' and backslash spellings match."""
    orch, store = _orch_with_run(cfg)
    orch.plan = _small_plan(orch, {"src/swarmforge/theme.py"})
    findings = [
        make_finding(1, file="src/swarmforge/theme.py"),
        make_finding(2, file="./src/swarmforge/theme.py"),
        make_finding(3, file="src\\swarmforge\\theme.py"),
        make_finding(4, file="/src/swarmforge/theme.py"),
    ]
    review = {"verdict": "request_changes", "summary": "blocking", "findings": findings}
    tasks = orch._repair_tasks_from_review(review, 2)
    assert [t.id for t in tasks] == ["R2F1", "R2F2", "R2F3", "R2F4"]
    assert not [e for e in store.events(orch.run_id) if e["kind"] == "scope"]


# --------------------------------------------------------------------- AC2
def test_in_scope_tasks_byte_identical_to_unscoped():
    """AC2: with scope applied, surviving tasks equal the pre-feature output.

    Out-of-scope and non-blocking findings are removed, duplicates deduped on
    (severity, file, title); the remaining tasks must be byte-identical to
    what `repair_tasks_from_findings` produced before scoping existed (same
    ids R<cycle>F<n>, round-robin roles, descriptions and AC text).
    """
    roles = ["backend", "frontend", "reviewer_fixer"]
    in1 = make_finding(1, file="src/swarmforge/theme.py")
    out1 = make_finding(2, file="src/swarmforge/repos.py")
    in2 = make_finding(3, severity="critical", file="src/swarmforge/cli.py")
    dup_of_in1 = dict(in1)
    minor_out = make_finding(9, severity="minor", file="src/swarmforge/navtools.py")

    mixed = [in1, out1, in2, dup_of_in1, minor_out]
    scoped = repair_tasks_from_findings(mixed, roles, cycle=5, scope_files=THEME_SCOPE)
    unscoped = repair_tasks_from_findings([in1, in2], roles, cycle=5)
    assert [asdict(t) for t in scoped] == [asdict(t) for t in unscoped]
    assert [t.id for t in scoped] == ["R5F1", "R5F2"]
    assert [t.role for t in scoped] == roles[:2]


# --------------------------------------------------------------------- AC3
def test_skipped_findings_still_recorded_and_visible(cfg):
    """AC3: scope-skipping does not remove findings from the findings table.

    The reviewer findings are recorded by `_review` before repair-task
    generation; the out-of-scope skip only affects task creation, so the
    findings remain in `store.findings` for the Review tab.
    """
    from swarmforge.db import Store

    store = Store(cfg.db_path)
    run_id = "r-scope-visible"
    store.create_run(run_id, budget_usd=5, max_runtime_s=300, max_repair_cycles=2)
    for f in (make_finding(1, file="src/swarmforge/theme.py"),
              make_finding(2, file="src/swarmforge/repos.py")):
        store.record_finding(run_id, cycle=1, source="reviewer", severity=f["severity"],
                             file=f["file"], line=f["line"], title=f["title"],
                             description=f["description"], recommendation=f["recommendation"])
    found = store.findings(run_id)
    assert {f["file"] for f in found} == {"src/swarmforge/theme.py",
                                          "src/swarmforge/repos.py"}


def test_out_of_scope_findings_still_visible_in_review_tab(tmp_path, monkeypatch):
    """AC3 (dashboard): with a scope event present, the findings table still
    lists the out-of-scope finding (nothing silently lost)."""
    from streamlit.testing.v1 import AppTest

    from .test_dashboard_scope import APP, _open, _seed, _write_cfg

    cfg = _write_cfg(tmp_path, monkeypatch)
    from swarmforge.db import Store

    store = Store(cfg.db_path)
    run_id = "20990101-020000-cccccc"
    _seed(store, run_id)
    store.record_finding(
        run_id, cycle=1, source="reviewer", severity="major",
        file="src/swarmforge/repos.py", line=88, title="[major] GIT env leaks",
        description="GIT_DIR from host leaks into repos.py subprocesses",
        recommendation="scrub GIT_ env")
    store.event(run_id, "scope",
                "skipped out-of-scope finding: [major] GIT env leaks "
                "(file src/swarmforge/repos.py)", level="warn")

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    _open(at, run_id)
    assert not at.exception, [e.value for e in at.exception]

    # the caption is present...
    assert any(c.value == "skipped out-of-scope findings: 1" for c in at.caption)
    # ...and the findings dataframe still lists the skipped finding
    findings_dfs = [d.value for d in at.dataframe if "file" in d.value.columns]
    assert findings_dfs, "Review tab rendered no findings table"
    df = findings_dfs[0]
    assert "src/swarmforge/repos.py" in set(df["file"])
    assert "[major] GIT env leaks" in set(df["title"])
