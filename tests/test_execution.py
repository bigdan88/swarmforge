"""Task lineage regressions: fake providers, temporary stores, no external writes."""
import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from swarmforge.db import Store
from swarmforge.execution import (Execution, ExecutionStopped, SharedBudget, ensure_allowance,
                                 external_once, observe_tool, recover, snapshot, progress_text)
from swarmforge.llm import BudgetExceeded, LLMClient
from swarmforge.orchestrator import Orchestrator
from swarmforge.state import RunState


def attempt(store, cfg, rid="first", parent=None):
    store.create_run(rid, budget_usd=cfg.limits.budget_usd, max_runtime_s=cfg.limits.max_runtime_s,
                     seed_run_id=parent, pid=os.getpid())
    eid = ensure_allowance(store, rid, cfg, parent)
    execution = Execution(store, rid, eid)
    execution.acquire(parent)
    return execution


def test_retry_replacement_and_restart_keep_original_allowance(cfg, tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    e = attempt(store, cfg)
    before = snapshot(store, "first")
    budget = SharedBudget(e)
    call = budget.reserve(1)
    budget.settle(call, .75)
    e.checkpoint("integrated", {"branch": "saved", "report": "retained"})
    e.finish("task_failure", "tests failed")
    store.close()
    reopened = Store(path)
    cfg.limits.budget_usd *= 10
    cfg.limits.max_runtime_s *= 10
    replacement = attempt(reopened, cfg, "replacement", "first")
    after = snapshot(reopened, "replacement")
    assert after["execution_id"] == "first"
    assert after["deadline"] == before["deadline"]
    assert after["budget_usd"] == before["budget_usd"]
    assert after["spent_usd"] == .75 and after["number"] == 2
    assert reopened._one("SELECT detail FROM execution_checkpoints WHERE execution_id='first'")
    replacement.finish("completed", "done")


def test_concurrent_attempts_are_fenced_across_connections(cfg, tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    root = attempt(store, cfg)
    root.finish("task_failure", "retry")
    others = [Store(path), Store(path)]
    for n in range(2):
        store.create_run(f"retry{n}", budget_usd=5, max_runtime_s=300)
        ensure_allowance(store, f"retry{n}", cfg, "first")
    def claim(n):
        e = Execution(others[n], f"retry{n}", "first")
        try:
            e.acquire("first")
            return e
        except ExecutionStopped as exc:
            return exc.kind
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(isinstance(x, Execution) for x in results) == 1
    assert "concurrent_attempt" in results


def test_model_reservations_cannot_double_spend_and_cancel_is_durable(cfg, tmp_path):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    b = SharedBudget(e)
    a = b.reserve(4)
    assert b.reserve(4) is None
    b.settle(a, 4)
    b.settle(a, 4)  # settlement is idempotent
    assert b.spent == 4
    with pytest.raises(BudgetExceeded):
        b.reserve(2)
    store.request_cancel("first")
    with pytest.raises(ExecutionStopped, match="Cancellation"):
        b.reserve(.1)
    e.finish("cancelled", "user requested")
    store.close()
    store = Store(tmp_path / "db")
    with pytest.raises(ExecutionStopped) as exc:
        attempt(store, cfg, "retry", "first")
    assert exc.value.kind == "cancelled"


def test_heartbeat_and_repeated_responses_do_not_count_as_progress(cfg, tmp_path):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    ctx = SimpleNamespace(workspace=tmp_path, files_written=set())
    read = {"name": "read_file", "args": {"path": "a.py"}, "output": "actual evidence"}
    observe_tool(e, ctx, read)
    progress = snapshot(store, "first")["last_progress_at"]
    for _ in range(cfg.limits.repeat_action_limit):
        observe_tool(e, ctx, read)
        store.event("first", "llm", "another answer")
    assert snapshot(store, "first")["last_progress_at"] == progress
    with pytest.raises(ExecutionStopped) as exc:
        e.poll()
    assert exc.value.kind == "stalled"
    store._exec("DELETE FROM execution_evidence WHERE kind='action'")
    store._exec("UPDATE execution_attempts SET last_progress_at=?", (time.time() - cfg.limits.no_progress_s - 1,))
    with pytest.raises(ExecutionStopped, match="No new code"):
        e.poll()
    assert e.row()["heartbeat"] > progress


def test_recurring_failure_and_recovery_are_bounded(cfg, tmp_path):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    for _ in range(cfg.limits.repeat_action_limit):
        e.observe("failure", {"tests": "same failure"})
    with pytest.raises(ExecutionStopped, match="Recurring failure"):
        e.poll()
    e.finish("stalled", "same failure")
    second = attempt(store, cfg, "retry", "first")
    assert second.row()["recoveries"] == 1
    second.finish("stalled", "still blocked")
    with pytest.raises(ExecutionStopped, match="Bounded recovery"):
        attempt(store, cfg, "third", "retry")


def test_checkpoint_replay_and_uncertain_external_actions(cfg, tmp_path):
    async def scenario():
        path = tmp_path / "db"
        store = Store(path)
        e = attempt(store, cfg)
        calls = []
        async def create_pr():
            calls.append("create")
            return "https://fake.test/pull/1"
        url = await external_once(store, "first", "pr:branch", create_pr)
        e.finish("completed", "published")
        store.close()
        store = Store(path)
        assert await external_once(store, "first", "pr:branch", create_pr) == url
        assert calls == ["create"]
        async def ambiguous_merge():
            calls.append("merge")
            raise ConnectionError("reply lost after server accepted merge")
        with pytest.raises(ConnectionError):
            await external_once(store, "first", "merge:1", ambiguous_merge)
        with pytest.raises(ExecutionStopped, match="reconciliation"):
            await external_once(store, "first", "merge:1", ambiguous_merge)
        assert calls == ["create", "merge"]
    asyncio.run(scenario())


def test_lost_worker_keeps_reservations_and_does_not_replay_publication(cfg, tmp_path, monkeypatch):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    call = SharedBudget(e).reserve(1)
    e.checkpoint("external:pr:branch", {"branch": "saved"}, status="pending")
    monkeypatch.setattr("swarmforge.launcher.pid_alive", lambda _: False)
    assert not recover(store, "first")
    assert snapshot(store, "first")["spent_usd"] == 1
    with pytest.raises(ExecutionStopped) as exc:
        attempt(store, cfg, "retry", "first")
    assert exc.value.kind == "external_uncertain"
    assert store._one("SELECT status FROM execution_calls WHERE id=?", (call,))["status"] == "uncertain"
    assert store.get_run("first")["stop_kind"] == "lost_worker"
    assert not recover(store, "first")
    assert snapshot(store, "first")["spent_usd"] == 1


def test_lost_worker_recovery_uses_saved_code_once(cfg, tmp_path, monkeypatch):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    e.checkpoint("integrated", {"branch": "preserved"})
    deadline = e.row()["deadline"]
    monkeypatch.setattr("swarmforge.launcher.pid_alive", lambda _: False)
    assert recover(store, "first")
    second = attempt(store, cfg, "second", "first")
    assert second.row()["deadline"] == deadline
    assert not recover(store, "second")


@pytest.mark.parametrize("stop", ["cancelled", "allowance_exhausted", "stalled"])
def test_watchdog_stops_underlying_work_and_retains_report(cfg, tmp_path, monkeypatch, stop):
    async def scenario():
        cfg.knowledge.enabled = False
        cfg.self_heal.diagnose = False
        store = Store(tmp_path / "db")
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def pipeline(self, *args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        monkeypatch.setattr(Orchestrator, "_pipeline", pipeline)
        real_watchdog = Orchestrator._watchdog
        async def fast_watchdog(self, pipeline):
            return await real_watchdog(self, pipeline, every_s=.01)
        monkeypatch.setattr(Orchestrator, "_watchdog", fast_watchdog)
        orch = Orchestrator(cfg, store, echo=None)
        running = asyncio.create_task(orch.execute(run_id="first"))
        await started.wait()
        if stop == "cancelled":
            store.request_cancel("first")
        elif stop == "allowance_exhausted":
            store._exec("UPDATE execution_allowances SET deadline=?", (time.time() - 1,))
        else:
            store._exec("UPDATE execution_attempts SET last_progress_at=?", (time.time() - cfg.limits.no_progress_s - 1,))
        await asyncio.wait_for(running, 2)
        assert cancelled.is_set()
        assert store.get_run("first")["stop_kind"] == stop
        assert (cfg.workdir / "runs/first/report.md").is_file()
        assert e_owner(store) is None
    asyncio.run(scenario())


def e_owner(store):
    return store._one("SELECT active_run FROM execution_allowances")["active_run"]


def test_parallel_fake_model_calls_reserve_unknown_prices_sequentially(cfg, tmp_path):
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        active = peak = 0
        async def respond(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "result"}}],
                                           "usage": {"estimated_cost": .1}})
        client = LLMClient(cfg, store, "first", SharedBudget(e), transport=httpx.MockTransport(respond))
        await asyncio.gather(*(client.chat("planner", [{"role": "user", "content": "task"}]) for _ in range(3)))
        assert peak == 1
        assert snapshot(store, "first")["spent_usd"] == pytest.approx(.3)
        assert store.cost_summary("first")["total"] == pytest.approx(.3)
        assert "remaining across all attempts" in progress_text(store, "first")
    asyncio.run(scenario())


def test_approval_wait_preserves_progress_without_refreshing_deadline(cfg, tmp_path):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    deadline = e.row()["deadline"]
    aid = store.request_approval("first", "Publish approved result?")
    e.poll()
    earlier = time.time() - cfg.limits.no_progress_s - 10
    store._exec("UPDATE execution_attempts SET last_progress_at=?,progress_wait_started=?", (earlier, earlier))
    store.decide_approval(aid, True)
    e.poll()
    assert snapshot(store, "first")["last_progress_at"] == earlier
    assert e.row()["deadline"] == deadline


def test_queue_restart_does_not_rerun_completed_ordinary_task(cfg, tmp_path, monkeypatch):
    from swarmforge import runqueue
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        e.finish("completed", "already published")
        store.update_run("first", state="COMPLETED", pr_url="https://fake.test/pull/1")
        qid = store.queue_add("T", "B", "owner/repo")
        store.queue_update(qid, status="running", run_id="first")
        store.kv_set(runqueue.PAUSED_KEY, "1")
        async def forbidden(*args, **kwargs):
            pytest.fail("completed task was executed again")
        monkeypatch.setattr(runqueue, "run_item", forbidden)
        await runqueue.work(cfg, store, echo=None)
        assert store.queue_item(qid)["status"] == "done"
        assert len(store.list_runs()) == 1
    asyncio.run(scenario())


def test_project_accounts_for_all_recovered_and_replacement_attempts(cfg, tmp_path):
    from swarmforge.projects.store import Projects
    from .test_projects import definition
    store = Store(tmp_path / "db")
    ps = Projects(store)
    p = ps.create()
    ps.draft(p["id"], definition())
    e = attempt(store, cfg)
    store.record_llm_call("first", cost_usd=.75, cost_source="provider_reported", status="ok")
    e.finish("lost_worker", "retained")
    second = attempt(store, cfg, "second", "first")
    store.record_llm_call("second", cost_usd=.5, cost_source="provider_reported", status="ok")
    second.finish("completed", "done")
    store.queue_add("task", "body", "owner/inventory", project_id=p["id"], milestone=0,
                    run_id="second", status="done")
    assert ps.spent(p["id"]) == 1.25
    assert ps.spent(p["id"], 0) == 1.25


def test_local_command_cancellation_kills_and_awaits_process_tree(cfg, tmp_path, monkeypatch):
    from swarmforge import sandbox
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        class Process:
            pid = 123
            returncode = None
            async def communicate(self):
                started.set()
                await asyncio.Future()
            async def wait(self):
                stopped.set()
                return -9
        process = Process()
        async def launch(*args, **kwargs):
            return process
        killed = []
        monkeypatch.setattr(asyncio, "create_subprocess_shell", launch)
        monkeypatch.setattr(sandbox, "_kill_tree", lambda proc: killed.append(proc.pid))
        command = asyncio.create_task(sandbox.LocalSandbox(cfg.sandbox).run(tmp_path, "fake-command"))
        await started.wait()
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert killed == [123] and stopped.is_set()
    asyncio.run(scenario())


def test_interrupted_model_request_retains_spend_once(cfg, tmp_path):
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        started = asyncio.Event()
        async def respond(request):
            started.set()
            await asyncio.Future()
        client = LLMClient(cfg, store, "first", SharedBudget(e), transport=httpx.MockTransport(respond))
        call = asyncio.create_task(client.chat("planner", [{"role": "user", "content": "task"}]))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert e.row()["spent_usd"] == cfg.limits.budget_usd
        assert store.cost_summary("first")["total"] == cfg.limits.budget_usd
        assert store._one("SELECT COUNT(*) AS n FROM execution_calls WHERE status='uncertain'")["n"] == 1
    asyncio.run(scenario())


def test_priced_request_is_rejected_before_provider_when_remainder_cannot_cover_it(cfg, tmp_path):
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        cfg.roles["planner"].price_in_per_mtok = 1000
        cfg.roles["planner"].price_out_per_mtok = 1000
        async def forbidden(request):
            pytest.fail("request exceeded reserved allowance")
        client = LLMClient(cfg, store, "first", SharedBudget(e), transport=httpx.MockTransport(forbidden))
        with pytest.raises(BudgetExceeded, match="cannot cover"):
            await client.chat("planner", [{"role": "user", "content": "bounded task"}])
        assert not store.llm_calls("first")
    asyncio.run(scenario())


def test_starting_code_from_scratch_still_uses_prior_task_allowance(cfg, tmp_path, monkeypatch):
    from swarmforge import runqueue
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    deadline = e.row()["deadline"]
    e.finish("task_failure", "tests failed")
    qid = store.queue_add("T", "B", "owner/repo", run_id="first", status="failed")
    next_id, _ = runqueue.requeue(store, store.queue_item(qid), seed_run_id=None)
    item = store.queue_item(next_id)
    assert item["seed_run_id"] is None and item["retry_of"] == "first"
    async def pipeline(self, *args):
        assert self.seed_run_id is None
        self._abort(RunState.FAILED, "fixture stopped before work")
    monkeypatch.setattr(Orchestrator, "_pipeline", pipeline)
    cfg.knowledge.enabled = False
    cfg.self_heal.diagnose = False
    asyncio.run(Orchestrator(cfg, store, retry_of=item["retry_of"], echo=None).execute(run_id="second"))
    assert snapshot(store, "second")["deadline"] == deadline
    assert snapshot(store, "second")["execution_id"] == "first"


def test_noisy_command_output_cannot_hide_repeated_actions(cfg, tmp_path):
    store = Store(tmp_path / "db")
    e = attempt(store, cfg)
    ctx = SimpleNamespace(workspace=tmp_path, files_written=set(), commands_run=[])
    for n in range(cfg.limits.repeat_action_limit):
        observe_tool(e, ctx, {"name": "run_command", "args": {"command": "same-command"},
                              "output": f"heartbeat {n}, still running after {n} seconds"})
    with pytest.raises(ExecutionStopped, match="Recurring action"):
        e.poll()


@pytest.mark.parametrize("kind", ["cancelled", "allowance_exhausted", "external_uncertain"])
def test_project_pauses_instead_of_replacing_explicitly_stopped_work(cfg, tmp_path, kind):
    from swarmforge.projects import service
    from swarmforge.projects.store import Projects
    from .test_projects import definition, Repo, ask
    async def scenario():
        store = Store(tmp_path / "db")
        ps = Projects(store)
        p = ps.create()
        ps.draft(p["id"], definition())
        service.approve(store, p["id"])
        await service.advance(cfg, store, p["id"], repository=Repo(), ask=ask)
        q = next(q for q in ps.items(p["id"]) if q["status"] == "queued")
        e = attempt(store, cfg)
        e.finish(kind, "requires review")
        store.update_run("first", state="FAILED", state_reason="requires review")
        store.queue_update(q["id"], status="failed", run_id="first")
        async def forbidden(*args, **kwargs):
            pytest.fail("stopped task was sent to the lead without review")
        await service.advance(cfg, store, p["id"], repository=Repo(), ask=forbidden)
        assert ps.get(p["id"])["status"] == "waiting_for_you"
        assert len(ps.items(p["id"])) == 3
    asyncio.run(scenario())


def test_pi_shutdown_cancels_inflight_provider_request(cfg, tmp_path):
    from swarmforge.agent.pi_runtime import ModelRelay
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        started = asyncio.Event()
        async def provider(request):
            started.set()
            await asyncio.Future()
        client = LLMClient(cfg, store, "first", SharedBudget(e), transport=httpx.MockTransport(provider))
        relay = ModelRelay(client)
        token = relay.register("planner", "T1", max_steps=5)
        body = json.dumps({"messages": [{"role": "user", "content": "task"}]}).encode()
        class Reader:
            async def readuntil(self, sep):
                return f"POST /v1/chat/completions HTTP/1.1\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            async def readexactly(self, n):
                return body
        class Writer:
            closed = False
            def close(self):
                self.closed = True
        writer = Writer()
        handler = asyncio.create_task(relay._handle(Reader(), writer))
        await started.wait()
        await relay.stop()
        assert handler.cancelled() and writer.closed
        assert store._one("SELECT status FROM execution_calls")["status"] == "uncertain"
        assert snapshot(store, "first")["spent_usd"] == cfg.limits.budget_usd
    asyncio.run(scenario())


def test_replanned_split_tasks_keep_failed_task_lineage_even_with_new_keys(cfg, tmp_path):
    from swarmforge.projects import service
    from swarmforge.projects.store import Projects
    from .test_projects import definition, Repo, ask, proposal
    async def scenario():
        store = Store(tmp_path / "db")
        ps = Projects(store)
        p = ps.create()
        ps.draft(p["id"], definition())
        service.approve(store, p["id"])
        repo = Repo()
        await service.advance(cfg, store, p["id"], repository=repo, ask=ask)
        first_q = next(q for q in ps.items(p["id"]) if q["status"] == "queued")
        first = attempt(store, cfg)
        deadline = first.row()["deadline"]
        first.finish("task_failure", "same issue needs smaller tasks")
        store.update_run("first", state="FAILED")
        store.queue_update(first_q["id"], status="failed", run_id="first")
        await service.advance(cfg, store, p["id"], repository=repo, ask=ask)
        retry_q = next(q for q in ps.items(p["id"]) if q["status"] == "queued")
        second = attempt(store, cfg, "second", "first")
        second.finish("task_failure", "split the remaining work")
        store.update_run("second", state="FAILED")
        store.queue_update(retry_q["id"], status="failed", run_id="second")
        async def split(*args, **kwargs):
            result = proposal()
            for n, task in enumerate(result["tasks"]):
                task["key"] = f"replacement-{n}"
            return result
        await service.advance(cfg, store, p["id"], repository=repo, ask=split)
        pending = [q for q in ps.items(p["id"]) if q["status"] in ("queued", "tentative")]
        assert len(pending) == 3
        assert all(q["execution_id"] == "first" for q in pending)
        assert snapshot(store, "second")["deadline"] == deadline
    asyncio.run(scenario())


def test_unsent_model_request_releases_its_reservation(cfg, tmp_path, monkeypatch):
    """A connection that never opens cannot have been billed: the reservation is released at $0
    instead of consuming the shared allowance, and the next request can still be reserved."""
    from swarmforge.llm import NothingBilled
    monkeypatch.setattr("swarmforge.llm._backoff", lambda n: 0)
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        def refuse(request):
            raise httpx.ConnectError("name or service not known", request=request)
        client = LLMClient(cfg, store, "first", SharedBudget(e), transport=httpx.MockTransport(refuse))
        with pytest.raises(NothingBilled):
            await client.chat("planner", [{"role": "user", "content": "task"}])
        assert e.row()["spent_usd"] == 0
        assert store._one("SELECT COUNT(*) AS n FROM execution_calls WHERE status='uncertain'")["n"] == 0
        assert store._one("SELECT COUNT(*) AS n FROM execution_calls WHERE status='reserved'")["n"] == 0
        assert SharedBudget(e).reserve() is not None
    asyncio.run(scenario())


def test_sent_request_with_lost_response_still_retains_spend(cfg, tmp_path):
    """Once the request may have reached the provider (read timeout), the charge stays uncertain."""
    async def scenario():
        store = Store(tmp_path / "db")
        e = attempt(store, cfg)
        def lost(request):
            raise httpx.ReadTimeout("no response", request=request)
        client = LLMClient(cfg, store, "first", SharedBudget(e), transport=httpx.MockTransport(lost))
        with pytest.raises(Exception):
            await client.chat("planner", [{"role": "user", "content": "task"}])
        assert e.row()["spent_usd"] == cfg.limits.budget_usd
        assert store._one("SELECT COUNT(*) AS n FROM execution_calls WHERE status='uncertain'")["n"] == 1
    asyncio.run(scenario())
