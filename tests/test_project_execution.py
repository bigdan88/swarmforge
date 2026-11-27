"""Approved workflow choices and measurements, using only fake adapters."""
import json
from contextlib import closing

import pytest

from swarmforge import runqueue
from swarmforge.db import Store
from swarmforge.orchestrator import Orchestrator
from swarmforge.plan import Criterion, Plan
from swarmforge.projects import service
from swarmforge.projects.metrics import project_metrics
from swarmforge.projects.spec import ProjectError, execution_policy, fixed, render, replace_living
from swarmforge.projects.store import Projects
from swarmforge.testing import TestEvidence, evaluate_criteria

from .test_projects import Repo, ask, definition


@pytest.mark.parametrize("policy", [{"developers": True, "validation": "self"}, {"developers": 3, "validation": "self"},
                                  {"developers": 1, "validation": "none"}, {"developers": 1}, None])
def test_invalid_execution_policy_is_rejected(policy):
    with pytest.raises(ProjectError, match="Execution"):
        render({**definition(), "execution": policy})


def test_execution_is_fixed_and_legacy_specs_are_unchanged():
    d = definition()
    assert execution_policy(d) == {"developers": 2, "validation": "independent"}
    old = render(d)
    assert "### Execution" not in old
    new = render({**d, "execution": {"developers": 1, "validation": "self"}})
    assert "Developer self tests" in fixed(new)
    with pytest.raises(ProjectError, match="any byte"):
        replace_living(old, new)


@pytest.mark.parametrize("developers,validation,action", [(1, "self", "plan"), (1, "independent", "plan"),
                                                        (2, "self", "plan"), (2, "independent", "plan"),
                                                        (2, "self", "request_review")])
async def test_dispatch_respects_approved_policy_and_preserves_publication(cfg, tmp_path, monkeypatch, developers, validation, action):
    import swarmforge.orchestrator as module
    with closing(Store(cfg.db_path)) as store:
        ps = Projects(store)
        pid = ps.create()["id"]
        ps.draft(pid, {**definition(), "execution": {"developers": developers, "validation": validation}})
        service.approve(store, pid)
        await service.advance(cfg, store, pid, repository=Repo(), ask=ask)
        q = next(q for q in ps.items(pid) if q["status"] == "queued")
        assert q["mode"] == ("reviewed" if developers == 1 else "swarm")
        store._exec("UPDATE queue_items SET project_action=? WHERE id=?", (action, q["id"]))
        observed = []

        class FakeOrchestrator:
            def __init__(self, config, store, **kwargs):
                observed.append((config, kwargs))

            async def execute(self, **kwargs):
                store.create_run(kwargs["run_id"])
                store.update_run(kwargs["run_id"], state="COMPLETED")
                return kwargs["run_id"]

        monkeypatch.setattr(module, "Orchestrator", FakeOrchestrator)
        monkeypatch.setattr(runqueue, "resolve_base", lambda *a: ("main", ""))
        result = await runqueue.run_item(cfg, store, store.queue_item(q["id"]), echo=None)
        config, kwargs = observed[0]
        expected = 1 if action == "request_review" else developers
        assert len(config.developer_roles) == expected
        assert config.limits.max_concurrent_agents <= expected
        assert config.review.independent_agents == (validation == "independent" or action == "request_review")
        assert kwargs["mode"] == ("reviewed" if expected == 1 else "swarm")
        assert config.github.create_pr and result["status"] == "done"
        assert config.review.require_verified_criteria == cfg.review.require_verified_criteria
        # A completed run still needs its merge before subsequent project work.
        assert not result["merged_sha"]
        assert cfg.review.independent_agents  # launch overrides do not mutate user configuration


async def test_self_test_mapping_requires_executed_evidence(cfg, tmp_path):
    with closing(Store(cfg.db_path)) as store:
        cfg.review.independent_agents = False
        orch = Orchestrator(cfg, store, mode="reviewed")
        orch.run_id = "self"
        store.create_run("self")
        store.upsert_task("self", "T1", role=cfg.developer_roles[0])
        store.kv_set("self_tests:self:T1", json.dumps({"AC1": ["test_app.test_result"]}))
        report = await orch._tester_phase(0)
        ev = TestEvidence("cycle0", "pytest", 0, False, 1, passed=1, junit_found=True,
                          cases={"test_app.test_result": "passed"})
        results = evaluate_criteria(report["criteria_tests"], [("AC1", "Result works"), ("AC2", "Unmapped")], ev)
        assert [r["status"] for r in results] == ["satisfied", "unverified"]
        ev.cases = {"test_app.test_result": "failed"}
        assert evaluate_criteria(report["criteria_tests"], [("AC1", "Result works")], ev)[0]["status"] == "failed"
        assert await orch._review(0, ev) is None
        orch.preview = {"url": "http://localhost:1234"}
        assert await orch._visual_review(0) == []
        assert store._all("SELECT * FROM llm_calls") == []


def test_self_validation_cannot_waive_product_evidence(cfg, tmp_path):
    with closing(Store(cfg.db_path)) as store:
        cfg.review.independent_agents = False
        cfg.review.require_verified_criteria = False
        orch = Orchestrator(cfg, store)
        orch.run_id = "self"
        store.create_run("self")
        orch.plan = Plan("test", [], [Criterion("AC1", "Real application", "product")])
        orch._criteria_results = [{"criterion_id": "AC1", "text": "Real application", "status": "unverified",
                                   "required_level": "product", "evidence": "Only unit tests ran"}]
        orch._repair_scope = lambda: set()
        ev = TestEvidence("cycle0", "pytest", 0, False, 1, passed=1, junit_found=True)
        assert any(p["kind"] == "acceptance" for p in orch._gate(0, ev, None, None, []))


def test_project_metrics_keep_prior_attempts_and_distinguish_merges(tmp_path):
    with closing(Store(tmp_path / "metrics.db")) as store:
        ps = Projects(store)
        pid = ps.create()["id"]
        ps.draft(pid, definition())
        assert project_metrics(store, pid)["cost_per_merged_task_usd"] is None
        assert project_metrics(store, pid)["first_pass_delivery_rate"] is None
        config = json.dumps({"review": {"independent_agents": False}, "developer_roles": ["developer_backend"]})
        store.create_run("failed", mode="reviewed", config_json=config)
        store.update_run("failed", state="FAILED", execution_id="lineage", finished_at=100)
        store.create_run("success", mode="reviewed", config_json=config)
        store.update_run("success", state="COMPLETED", execution_id="lineage", finished_at=200)
        q = store.queue_add("Build", "body", "owner/inventory", project_id=pid, project_key="build", milestone=0)
        store.queue_update(q, run_id="success", status="done")  # old attempt no longer has a queue pointer
        store.record_llm_call("failed", role="developer_backend", model="m", cost_usd=2, total_tokens=20, cost_source="provider_reported")
        store.record_llm_call("success", role="developer_backend", model="m", cost_usd=3, total_tokens=30, cost_source="provider_reported")
        store.record_llm_call("project:" + pid, role="lead", model="lead", cost_usd=1, total_tokens=10, cost_source="provider_reported")
        store.record_llm_call("success", role="developer_backend", model="m", cost_usd=None)
        store.record_acceptance("success", cycle=0, criterion_id="AC1", status="satisfied")
        m = project_metrics(store, pid)
        assert m["runs"] == 2 and m["cost_usd"] == 6 and m["tokens"] == 60
        assert m["merged_tasks"] == 0 and m["cost_per_merged_task_usd"] is None
        assert m["attempted_tasks"] == 1 and m["delivery_rate"] == 0
        assert m["unpriced_calls"] == 1 and m["run_states"] == {"FAILED": 1, "COMPLETED": 1}
        store._exec("UPDATE queue_items SET merged_sha='merged' WHERE id=?", (q,))
        m = project_metrics(store, pid)
        assert m["cost_per_merged_task_usd"] == 6 and m["first_pass_delivery_rate"] == 0
        assert m["delivery_rate"] == 1
        assert m["workflows"][0]["workflow"] == "reviewed / self"
        assert m["acceptance_observations"] == {"satisfied": 1}


@pytest.mark.slow
async def test_one_developer_self_tests_delivers_a_local_branch(cfg, sample_repo):
    from .conftest import FakeProvider, tool_call
    from .fake_team import _n_assistant
    cfg.review.independent_agents = False
    cfg.review.require_junit = cfg.review.require_verified_criteria = True
    cfg.knowledge.enabled = False
    cfg.limits.parallel_review = True
    cfg.limits.tester_with_developers = True
    script = [
        ("write_file", {"path": "greeting.py", "content": "def greet():\n    return 'hello'\n"}),
        ("write_file", {"path": "tests/test_greeting.py", "content": "from greeting import greet\n\ndef test_greeting():\n    assert greet() == 'hello'\n"}),
        ("finish", {"summary": "Added a greeting with a self test", "criteria_tests": {"AC1": ["tests/test_greeting.py::test_greeting"]}}),
    ]

    def developer(body):
        name, args = script[min(_n_assistant(body), len(script) - 1)]
        return {"content": "", "tool_calls": [tool_call(name, args)]}

    fake = FakeProvider({"deepseek-fake": developer})  # any other agent/model would fail
    issue = sample_repo.parent / "greeting.md"
    issue.write_text("# Greeting\nAdd greet() returning hello, with a regression test.")
    with closing(Store(cfg.db_path)) as store:
        orch = Orchestrator(cfg, store, mode="reviewed", echo=None, llm_transport=fake.transport)
        rid = await orch.execute(issue_file=str(issue), repo=str(sample_repo))
        run = store.get_run(rid)
        assert run["state"] == "COMPLETED", run["state_reason"]
        assert "delivered to branch" in run["state_reason"]
        assert {t["role"] for t in store.tasks(rid)} == {cfg.developer_roles[0]}
        assert store.acceptance(rid)[-1]["status"] == "satisfied"
        assert store.test_runs(rid)[-1]["junit_path"]


@pytest.mark.parametrize("structured", [False, True])
async def test_pi_handoff_retains_self_test_mapping(cfg, tmp_path, monkeypatch, structured):
    from swarmforge.agent import pi_runtime as pi
    from swarmforge.agent.tools import ToolContext
    from swarmforge.sandbox import LocalSandbox
    final = {"summary": "Implemented and tested", "criteria_tests": {"AC1": ["tests/test_app.py::test_case"]}}

    async def fake_process(argv, cwd, env, out_file, err_file, timeout_s):
        text = "```json\n" + json.dumps(final) + "\n```" if structured else "Implemented and tested"
        out_file.write_text(json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                                                           "content": [{"type": "text", "text": text}]}}))
        return False

    monkeypatch.setattr(pi, "_run_local", fake_process)
    cfg.review.independent_agents = False
    with closing(Store(cfg.db_path)) as store:
        orch = Orchestrator(cfg, store)
        relay = pi.ModelRelay(None)  # no provider calls; the process supplies a recorded final event
        result = await pi.run_pi_agent(relay=relay, ctx=ToolContext(workspace=tmp_path, sandbox=LocalSandbox(cfg.sandbox)),
                                       role=cfg.developer_roles[0], task_key="T1", system="test", prompt="test",
                                       terminal=orch._finish_terminal(), max_steps=2, timeout_s=5, max_tokens=100, thinking=None)
        assert result.status == "completed", result.error
        assert result.value["criteria_tests"] == (final["criteria_tests"] if structured else {})
