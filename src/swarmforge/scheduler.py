"""Dependency-aware asynchronous task scheduler.

Pure orchestration logic: given tasks with dependencies and an async `execute(task) -> bool`
callback, dispatch every task whose dependencies have succeeded, up to `max_concurrency` at
once. Tasks whose dependencies failed are marked BLOCKED (transitively) and never run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from .plan import Task
from .state import RunAborted, TaskState


@dataclass
class ScheduleResult:
    states: dict[str, TaskState]
    order_started: list[str] = field(default_factory=list)
    max_parallel: int = 0

    @property
    def succeeded(self) -> list[str]:
        return [k for k, v in self.states.items() if v == TaskState.SUCCEEDED]

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.states.items() if v == TaskState.FAILED]

    @property
    def blocked(self) -> list[str]:
        return [k for k, v in self.states.items() if v == TaskState.BLOCKED]


class Scheduler:
    def __init__(self, tasks: Iterable[Task], execute: Callable[[Task], Awaitable[bool]], *,
                 max_concurrency: int = 2, satisfied: Iterable[str] = (),
                 should_cancel: Callable[[], bool] | None = None,
                 on_state: Callable[[str, TaskState, str], None] | None = None):
        self.tasks = {t.id: t for t in tasks}
        self.execute = execute
        self.max_concurrency = max(1, max_concurrency)
        self.satisfied = set(satisfied)  # deps already done in earlier cycles
        self.should_cancel = should_cancel
        self.on_state = on_state or (lambda *_: None)
        self.states: dict[str, TaskState] = {k: TaskState.PENDING for k in self.tasks}

    def _set(self, tid: str, st: TaskState, why: str = "") -> None:
        self.states[tid] = st
        self.on_state(tid, st, why)

    def _deps_ok(self, t: Task) -> bool:
        return all(d in self.satisfied or self.states.get(d) == TaskState.SUCCEEDED for d in t.depends_on)

    def _deps_failed(self, t: Task) -> str | None:
        for d in t.depends_on:
            if d in self.satisfied:
                continue
            if self.states.get(d) in (TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED):
                return d
        return None

    async def run(self) -> ScheduleResult:
        result = ScheduleResult(states=self.states)
        running: dict[asyncio.Task, str] = {}
        try:
            while True:
                # propagate blocking
                changed = True
                while changed:
                    changed = False
                    for tid, t in self.tasks.items():
                        if self.states[tid] == TaskState.PENDING:
                            bad = self._deps_failed(t)
                            if bad:
                                self._set(tid, TaskState.BLOCKED, f"dependency {bad} did not succeed")
                                changed = True
                cancel = bool(self.should_cancel and self.should_cancel())
                if not cancel:
                    for tid, t in self.tasks.items():
                        if len(running) >= self.max_concurrency:
                            break
                        if self.states[tid] == TaskState.PENDING and self._deps_ok(t):
                            self._set(tid, TaskState.RUNNING)
                            result.order_started.append(tid)
                            running[asyncio.create_task(self._guarded(t))] = tid
                    result.max_parallel = max(result.max_parallel, len(running))
                if not running:
                    for tid, st in self.states.items():
                        if st == TaskState.PENDING:
                            self._set(tid, TaskState.CANCELLED if cancel else TaskState.BLOCKED,
                                      "run cancelled" if cancel else "unsatisfiable dependencies")
                    return result
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    tid = running.pop(fut)
                    ok, why = fut.result()
                    self._set(tid, TaskState.SUCCEEDED if ok else TaskState.FAILED, why)
        finally:
            for fut in running:
                fut.cancel()

    async def _guarded(self, t: Task) -> tuple[bool, str]:
        try:
            return bool(await self.execute(t)), ""
        except (asyncio.CancelledError, RunAborted):
            raise
        except Exception as e:  # a crashing task must not take down the scheduler
            return False, f"{type(e).__name__}: {e}"
