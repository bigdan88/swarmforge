"""Delivery operations return outcomes; the orchestrator owns lifecycle transitions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..workspace import GitError, GitWorkspace


@dataclass(frozen=True)
class DeliveryOutcome:
    reason: str
    warning: str | None = None
    branch: str | None = None


async def deliver_local(workspace: GitWorkspace, source: str) -> DeliveryOutcome:
    src = Path(source)
    if src.is_dir() and (src / ".git").exists():
        try:
            branch = await asyncio.to_thread(workspace.deliver_to_local, src)
            return DeliveryOutcome(f"approved; delivered to branch {branch} in {src}", branch=branch)
        except GitError as exc:
            return DeliveryOutcome(
                f"approved; local run, branch {workspace.integration_branch} "
                f"(delivery into {src} failed: {str(exc)[:200]})", warning=str(exc))
    return DeliveryOutcome(f"approved; local run, branch {workspace.integration_branch}")
