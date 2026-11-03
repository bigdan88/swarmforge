"""Allow-listed operational configuration: safe to display and persist with runs."""
from __future__ import annotations

import copy
from dataclasses import asdict

from .config import Config


def resolve_variant(cfg: Config, store) -> tuple[Config, str | None]:
    """Resolve the same variant for launch, inspection and previews without changing the base."""
    cfg = copy.deepcopy(cfg)
    if cfg.active_variant and not cfg.variant_applied:
        from .variants import apply_overlay
        variant = store.variant(cfg.active_variant)
        if variant:
            return apply_overlay(cfg, variant["overlay"], variant["id"]), None
        note = f"active_variant {cfg.active_variant!r} not found; using the base config"
        cfg.active_variant = None
        return cfg, note
    return cfg, None


def effective_config(cfg: Config) -> dict:
    # Do not serialize providers, headers, environment, extra_body, agent_env or command strings.
    return {
        "active_variant": cfg.active_variant,
        "variant_applied": cfg.variant_applied,
        "roles": {name: {"provider": role.provider, "model": role.model,
                          "runtime": role.runtime, "fallback_models": list(role.fallback_models),
                          "temperature": role.temperature, "max_steps": role.max_steps,
                          "timeout_s": role.timeout_s,
                          "reasoning_effort": role.reasoning_effort or cfg.reasoning_effort}
                  for name, role in cfg.roles.items()},
        "developer_roles": list(cfg.developer_roles),
        "limits": asdict(cfg.limits),
        "projects": asdict(cfg.projects),
        "sandbox": {"mode": cfg.sandbox.mode, "image": cfg.sandbox.image,
                    "network": cfg.sandbox.network},
        "pi": {"network": cfg.pi.network, "relay_host": cfg.pi.relay_host},
        "review": asdict(cfg.review),
        "github": {"require_push_approval": cfg.github.require_push_approval,
                   "create_pr": cfg.github.create_pr, "branch_prefix": cfg.github.branch_prefix},
        "auto_merge": asdict(cfg.auto_merge),
        "knowledge": {"enabled": cfg.knowledge.enabled,
                      "max_notes_in_prompt": cfg.knowledge.max_notes_in_prompt},
    }


def policy_summary(cfg: Config) -> str:
    return (f"scheduling={cfg.limits.same_repo}; sandbox={cfg.sandbox.mode}/{cfg.sandbox.network}; "
            f"Pi network={cfg.pi.network}; push approval={cfg.github.require_push_approval}; "
            f"blocking={','.join(cfg.review.blocking_severities)}; auto-merge={cfg.auto_merge.enabled}")
