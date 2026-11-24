"""Opt-in speed/versatility options: auto routing, direct repairs, parallel review, the early tester,
model escalation, JUnit flags for non-pytest runners and outlines for non-Python files."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmforge.agent import navtools
from swarmforge.agent.pi_runtime import pi_argv, pi_argv_head
from swarmforge.agent.tools import ToolContext
from swarmforge.orchestrator import Orchestrator
from swarmforge.plan import Task
from swarmforge.testing import with_junit

from .test_e2e import _run


@pytest.fixture
def heavy(request):
    """Full runs take turns across xdist workers (same lock as test_e2e)."""
    with request.getfixturevalue("browser_slot")():
        yield


# ------------------------------------------------------------------ unit level
def test_with_junit_known_runners_and_placeholder():
    p = "/artifacts/junit-x.xml"
    assert with_junit("python -m pytest -q", p) == f"python -m pytest -q --junitxml={p}"
    assert with_junit("pytest --junitxml=mine.xml", p) == "pytest --junitxml=mine.xml"
    assert with_junit("npx vitest run", p) == f"npx vitest run --reporter=default --reporter=junit --outputFile.junit={p}"
    assert with_junit("npx vitest run --reporter=dot", p) == "npx vitest run --reporter=dot"
    node = with_junit("node --test", p)
    assert node.startswith("node --test ") and node.endswith(f"--test-reporter=junit --test-reporter-destination={p}")
    assert with_junit("gotestsum -- ./...", p) == f"gotestsum --junitfile {p} -- ./..."
    assert with_junit("gotestsum", p) == f"gotestsum --junitfile {p}"
    jest = "JEST_JUNIT_OUTPUT_FILE={junit} npx jest --reporters=default --reporters=jest-junit"
    assert with_junit(jest, p) == jest.replace("{junit}", p)
    assert with_junit("make test", p) == "make test"       # unknown runner: untouched


@pytest.mark.parametrize("name, text, expected, absent", [
    ("app.tsx",
     "import x from 'y'\nexport interface Props { a: string }\nexport default function App(props: Props) {\n"
     "  if (props.a) {\n    return null\n  }\n}\nexport const useThing = (a: number) => {\n  return a\n}\n"
     "class Store {\n  async load(id: string): Promise<void> {\n  }\n}\n",
     ["L2: export interface Props", "L3: export default function App", "L8: export const useThing",
      "L11: class Store", "L12:   async load(id: string)"], ["L4", "L5"]),
    ("main.go", "package main\n\ntype Server struct {\n}\n\nfunc (s *Server) Run() error {\n\treturn nil\n}\n",
     ["L3: type Server struct", "L6: func (s *Server) Run() error"], ["L7"]),
    ("lib.rs", "pub struct Deck;\nimpl Deck {\n    pub fn new() -> Self { Deck }\n}\n",
     ["L1: pub struct Deck", "L2: impl Deck", "L3:     pub fn new()"], []),
])
def test_outline_for_other_languages(tmp_path, name, text, expected, absent):
    (tmp_path / name).write_text(text, encoding="utf-8")
    out = asyncio.run(navtools.outline(ToolContext(workspace=tmp_path), name))
    for e in expected:
        assert e in out, out
    for a in absent:
        assert a + ":" not in out, out


def test_outline_unknown_type_still_shows_the_head(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert "no outline for this file type" in asyncio.run(navtools.outline(ToolContext(workspace=tmp_path), "notes.txt"))


def test_pi_argv_head_carries_no_prompt_text():
    head = pi_argv_head("pi", "m", "high", ["--x"])
    assert "--append-system-prompt" not in head and head[-1] == "--x"
    full = pi_argv("pi", "m", "high", "SYS", "PROMPT", ["--x"])
    assert full[-5:] == ["--append-system-prompt", "SYS", "--x", "--", "PROMPT"]


class _Store:
    def __init__(self):
        self.upserts = []

    def tasks(self, run_id):
        return [{"task_key": "T1"}, {"task_key": "D1"}]

    def upsert_task(self, run_id, key, **kw):
        self.upserts.append((key, kw))


def _orch(**limits):
    lim = SimpleNamespace(direct_repairs=True, direct_repair_max_problems=3)
    lim.__dict__.update(limits)
    o = SimpleNamespace(cfg=SimpleNamespace(limits=lim, developer_roles=["dev_a", "dev_b"], roles={}),
                        store=_Store(), run_id="r", mode="swarm", single_role="dev_b", logs=[])
    o.log = lambda kind, msg, **kw: o.logs.append(msg)
    return o


def test_direct_fix_plan_decisions():
    plan = Orchestrator._direct_fix_plan
    tests = {"kind": "tests", "summary": "test suite not passing", "detail": "2 failed"}
    review = {"kind": "review", "summary": "[major] bug", "detail": "x"}
    r1 = Task(id="R1F1", title="t", description="d", role="dev_a")
    assert plan(_orch(direct_repairs=False), 1, [tests], []) is None            # option off
    assert plan(_orch(), 1, [{"kind": "task", "summary": "T2 did not complete", "detail": ""}], []) is None
    assert plan(_orch(direct_repair_max_problems=1), 1, [tests, tests], []) is None   # too many: planner decides
    uncovered = plan(_orch(), 1, [review], [])                                  # finding without a repair task
    assert [t.role for t in uncovered.tasks] == ["dev_a"] and "[major] bug" in uncovered.tasks[0].acceptance_criteria
    assert plan(_orch(), 1, [review], [r1]).tasks == []                         # already covered: nothing to add
    p = plan(_orch(), 1, [review, tests], [r1])
    (t,) = p.tasks
    assert t.id == "D1x2" and t.depends_on == ["R1F1"] and "2 failed" in t.description   # D1 was taken
    o = _orch()
    o.mode = "reviewed"
    assert plan(o, 2, [tests], []).tasks[0].role == "dev_b"                     # the run's single developer


def test_escalation_only_on_retry_or_repair():
    def make(model="strong"):
        o = _orch()
        o.cfg.roles = {"dev_a": SimpleNamespace(model="cheap", escalation_model=model)}
        o.llm = SimpleNamespace(model_overrides={})
        return o
    t = Task(id="T1", title="t", description="d", role="dev_a")
    o = make()
    Orchestrator._maybe_escalate(o, t, 0, 1)
    assert o.llm.model_overrides == {}                                          # first attempt stays cheap
    Orchestrator._maybe_escalate(o, t, 0, 2)
    assert o.llm.model_overrides == {"T1": "strong"} and o.store.upserts == [("T1", {"model": "strong"})]
    o = make()
    Orchestrator._maybe_escalate(o, t, 1, 1)
    assert o.llm.model_overrides == {"T1": "strong"}                            # repair cycle
    o = make(model=None)
    Orchestrator._maybe_escalate(o, t, 1, 2)
    assert o.llm.model_overrides == {}                                          # nothing configured


# ------------------------------------------------------------------ full offline runs


@pytest.mark.slow
async def test_parallel_review_with_direct_repairs_completes(cfg, sample_repo, heavy):
    cfg.limits.parallel_review = True
    cfg.limits.direct_repairs = True
    store, run_id, fake = await _run(cfg, sample_repo)
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    msgs = [e["message"] for e in store.events(run_id)]
    assert any("while the full test suite runs" in m for m in msgs)
    assert any("limits.direct_repairs" in m for m in msgs)
    tr = store.test_runs(run_id)
    assert tr[-1]["exit_code"] == 0 and tr[-1]["failed"] == 0


@pytest.mark.slow
async def test_tester_alongside_developers_completes(cfg, sample_repo, heavy):
    cfg.limits.tester_with_developers = True
    store, run_id, fake = await _run(cfg, sample_repo)
    run = store.get_run(run_id)
    assert run["state"] == "COMPLETED", run["state_reason"]
    assert any(t["task_key"] == "tester-c0-early" for t in store.tasks(run_id))


async def _run_auto(cfg, sample_repo, monkeypatch, answer):
    """A full run in mode=auto where the routing call gets `answer` (a message dict)."""
    from . import fake_team
    original = fake_team.planner

    def planner(body):
        if "how much coordination" in body["messages"][0]["content"]:
            return answer
        return original(body)
    monkeypatch.setattr(fake_team, "planner", planner)
    return await _run(cfg, sample_repo, mode="auto")


@pytest.mark.slow
async def test_auto_mode_follows_the_routing_answer(cfg, sample_repo, heavy, monkeypatch):
    store, run_id, fake = await _run_auto(cfg, sample_repo, monkeypatch, {
        "content": 'Sure. {"mode": "swarm", "reason": "API and UI are independent"}'})
    run = store.get_run(run_id)
    assert run["mode"] == "swarm" and run["state"] == "COMPLETED", run["state_reason"]
    assert any("auto workflow -> swarm: API and UI are independent" in e["message"] for e in store.events(run_id))
    assert len([t for t in store.tasks(run_id) if t["kind"] == "implement"]) > 1     # the planner split it


@pytest.mark.slow
async def test_auto_mode_uses_one_developer_without_a_usable_answer(cfg, sample_repo, heavy, monkeypatch):
    store, run_id, fake = await _run_auto(cfg, sample_repo, monkeypatch, {"content": "it depends"})
    run = store.get_run(run_id)
    assert run["mode"] == "reviewed", run["mode"]
    assert [t["task_key"] for t in store.tasks(run_id) if t["kind"] == "implement"] == ["T1"]
    assert any("auto workflow -> reviewed" in e["message"] for e in store.events(run_id))
