"""The unattended supervisor: an autonomous project with budget left never just sits."""
import copy
import json
import time

import pytest

from swarmforge.projects import autonomy, service
from swarmforge.projects.spec import ProjectError

from .test_project_recovery import state  # noqa: F401  (fixture)


@pytest.fixture
def unattended(state, cfg):  # noqa: F811
    cfg.projects.autonomous = True
    cfg.projects.auto_retry_delay_s = 0
    return state


def stop(ps, pid, message="The same problem has failed three times. Propose a correction before resuming."):
    service.stop(ps, ps.get(pid), message, {"task": "deck"})


def test_any_internal_stop_becomes_a_backed_off_replan_with_the_reason(unattended, cfg):
    store, ps, pid = unattended
    stop(ps, pid)
    assert autonomy.recover_waiting(cfg, store, ps.get(pid))
    p = ps.get(pid)
    assert p["status"] == "running" and not p["last_error"]
    context = autonomy.instructions(store, pid)
    assert context["problem"] == "supervisor" and "failed three times" in context["instruction"]
    assert "evaluate the milestone now" not in context["instruction"]  # first round: just a different approach
    stop(ps, pid)
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))  # back-off: 2 minutes before round 2
    state_ = json.loads(store.kv_get("project_supervisor:" + pid))
    state_["rounds"] = [time.time() - 600]
    store.kv_set("project_supervisor:" + pid, json.dumps(state_))
    assert autonomy.recover_waiting(cfg, store, ps.get(pid))
    assert "evaluate the milestone now" in autonomy.instructions(store, pid)["instruction"]
    assert autonomy.deferral_allowed(store, ps.get(pid))


def test_supervisor_never_overrides_the_owner_money_or_uncertain_writes(unattended, cfg):
    store, ps, pid = unattended
    service.pause(store, pid)
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))
    ps.update(pid, status="running", last_error=None)
    store.record_llm_call("project:" + pid, task_key="project-lead:m0", cost_usd=29.9, cost_source="estimated", status="ok")
    stop(ps, pid)
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))  # under $0.50 left: a money decision
    cfg.projects.supervisor_rounds_per_day = 0
    store._exec("DELETE FROM llm_calls")
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))  # switched off
    cfg.projects.autonomous = False
    cfg.projects.supervisor_rounds_per_day = 8
    assert not autonomy.recover_waiting(cfg, store, ps.get(pid))  # owner did not choose unattended mode


def test_after_repeated_rounds_a_milestone_moves_on_with_failed_checks_listed_not_passed(unattended, cfg, monkeypatch):
    store, ps, pid = unattended
    checks = [{"text": "Swipe deck shows 14 profiles", "status": "passed", "evidence": [{"test": "t"}]},
              {"text": "Card animation is smooth", "status": "failed", "evidence": []}]
    monkeypatch.setattr(service, "verify_checks", lambda *_: copy.deepcopy(checks))
    with pytest.raises(ProjectError, match="failed checks"):
        service.checkpoint(store, ps.get(pid), "Try to finish", config=cfg)
    store.kv_set(autonomy._defer_key(ps.get(pid)), "1")  # set by the supervisor's second round
    service.checkpoint(store, ps.get(pid), "Deck works; animation still failing after repairs", config=cfg)
    p = ps.get(pid)
    assert p["status"] == "running" and p["current_milestone"] == 0  # verification pass first
    service.checkpoint(store, ps.get(pid), "Animation still failing", config=cfg)
    assert ps.get(pid)["current_milestone"] == 1
    m0 = ps.milestones(pid)[0]
    assert m0["status"] == "verification_pending"
    assert [c["status"] for c in json.loads(m0["report"])["checks"]] == ["passed", "failed"]  # never marked passed


def test_worker_stays_alive_while_a_stopped_project_can_still_be_reopened(unattended, cfg):
    store, ps, pid = unattended
    assert not autonomy.supervision_pending(cfg, store)  # running projects keep the worker alive anyway
    stop(ps, pid, "Project lead stopped: TimeoutError: . Review the saved decision before resuming.")
    assert autonomy.supervision_pending(cfg, store)
    store.kv_set("project_supervisor:" + pid, json.dumps({"rounds": [time.time()] * cfg.projects.supervisor_rounds_per_day}))
    assert not autonomy.supervision_pending(cfg, store)  # out of rounds today: nothing to wait for
    store.kv_set("project_supervisor:" + pid, None)
    ps.update(pid, last_error="Paused by you")
    assert not autonomy.supervision_pending(cfg, store)  # an owner pause is not the worker's business


def test_lead_gets_more_time_after_each_timeout(unattended, cfg, monkeypatch):
    """A slow lead (long project history) used to time out the same way three times and stop."""
    import asyncio

    from .test_project_recovery import finish
    from .test_projects import Repo, proposal

    store, ps, pid = unattended
    limits = []

    async def lead_needing_900s(awaitable, timeout):
        limits.append(timeout)
        if timeout < 900:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    async def ask(*args):
        return proposal()

    monkeypatch.setattr(asyncio, "wait_for", lead_needing_900s)
    for _ in range(3):
        if ps.get(pid)["status"] != "running":
            assert autonomy.recover_waiting(cfg, store, ps.get(pid))
        autonomy._context(store, ps.get(pid), "test", "", 0)
        finish(service.advance(cfg, store, pid, repository=Repo(), ask=ask))
        if any(q["status"] == "queued" for q in ps.items(pid)):
            break
    assert [t for t in limits if t != 30] == [300, 600, 1200]  # doubled after each timeout, capped at 20 min
    assert any(q["status"] == "queued" for q in ps.items(pid))


def test_after_two_lead_timeouts_the_lead_starts_with_another_model(unattended, cfg):
    import asyncio

    from swarmforge.projects import lead

    store, ps, pid = unattended
    cfg.roles["planner"].fallback_models = ["backup/model"]
    seen = {}

    async def fake_chat(self, role, messages, *, task_key=None, **kw):
        seen["override"] = self.model_overrides.get(task_key)
        seen["busy_wait"] = self.busy_wait_s
        seen["sent"] = messages[1]["content"]
        return type("R", (), {"content": '{"action": "plan"}'})()

    import swarmforge.llm as llm_mod
    original = llm_mod.LLMClient.chat
    llm_mod.LLMClient.chat = fake_chat
    try:
        asyncio.run(lead.call(cfg, store, ps.get(pid), "system", {"x": 1}))
        assert seen["override"] is None and seen["busy_wait"] is None  # normal call: unchanged
        asyncio.run(lead.call(cfg, store, ps.get(pid), "system", {"x": 1, "_lead_timeouts": 2, "_lead_time_limit_s": 1200}))
        assert seen["override"] == "backup/model"
        assert seen["busy_wait"] == 400  # a busy model gives way to its fallback inside the limit
        assert "_lead_timeouts" not in seen["sent"]  # bookkeeping never reaches the model
    finally:
        llm_mod.LLMClient.chat = original


def test_lead_context_is_trimmed_to_fit_and_the_spec_owner_part_is_kept(unattended):
    from swarmforge.projects import lead
    from swarmforge.projects.spec import fixed

    store, ps, pid = unattended
    spec = ps.get(pid)["spec"]
    context = {"spec": spec, "latest_diff": "d" * 900_000,
               "recent_outcomes": [{"report": "r" * 50_000} for _ in range(60)], "milestone": 2}
    out = lead.fit_context(context, 120_000)
    assert len(json.dumps(out)) <= 120_000
    assert fixed(out["spec"]) == fixed(spec)  # the owner's part is never cut
    assert "characters trimmed" in out["latest_diff"] and out["milestone"] == 2
    assert out["recent_outcomes"][0].startswith("[20 older entries omitted]")
    small = {"spec": spec, "milestone": 1}
    assert lead.fit_context(small) == small  # a normal project is sent unchanged


def test_too_large_request_is_not_mistaken_for_a_busy_model(cfg, unattended):
    """Qwen answered 'input length' errors with HTTP 500; SwarmForge waited 15 min per model as if busy."""
    import asyncio

    import httpx

    from swarmforge.projects import lead

    store, ps, pid = unattended
    sizes = []

    def handler(request):
        size = len(request.content)
        sizes.append(size)
        if size > 60_000:
            return httpx.Response(500, json={"error": {"message": '{"error":{"code":"invalid_parameter_error",'
                                                       '"message":"Range of input length should be [1, 983616]"}}'}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action": "plan"}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    huge = {"spec": ps.get(pid)["spec"], "latest_diff": "x" * 200_000}
    result = asyncio.run(asyncio.wait_for(lead.call(cfg, store, ps.get(pid), "system", huge, transport=httpx.MockTransport(handler)), 30))
    assert result == {"action": "plan"}
    assert sizes[0] > 60_000 and sizes[-1] <= 60_000  # retried at once with less, no busy waiting
