"""Shared execution contract for metered built-in and Pi agent harnesses."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .runtime import AgentResult, AgentSpec


class AgentHarness(Protocol):
    async def run(self, spec: AgentSpec) -> AgentResult: ...


@dataclass
class FunctionHarness:
    """Adapt an existing harness without moving policy or metering into it."""
    execute: Callable[[AgentSpec], Awaitable[AgentResult]]

    async def run(self, spec: AgentSpec) -> AgentResult:
        if spec.should_cancel and spec.should_cancel():
            return AgentResult(status="cancelled", error="cancel requested before agent launch")
        return await self.execute(spec)
