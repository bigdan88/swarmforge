"""`swarmforge check`: validate configuration, local tooling and provider connectivity."""
from __future__ import annotations

import shutil
import subprocess

import httpx

from .config import Config
from .github import get_token
from .llm import Budget, LLMClient, LLMError

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def _p(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "?"


def _line(status: str, msg: str) -> None:
    print(f"[{status}] {msg}")


async def run_checks(cfg: Config, live: bool = False, roles: list[str] | None = None) -> int:
    failures = 0
    _line(OK, f"config loaded from {cfg.source_path}")
    _line(OK, f"workdir {cfg.workdir} (db {cfg.db_path})")
    for name, r in cfg.roles.items():
        _line(OK, f"role {name:20} -> {r.provider}:{r.model}")
    for pname, p in cfg.providers.items():
        if p.api_key():
            _line(OK, f"provider {pname}: {p.api_key_env} is set")
        else:
            _line(FAIL, f"provider {pname}: environment variable {p.api_key_env} is NOT set (.env or shell)")
            failures += 1

    if shutil.which("git"):
        _line(OK, "git: " + subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip())
    else:
        _line(FAIL, "git not found on PATH")
        failures += 1

    if cfg.sandbox.mode == "docker":
        d = cfg.sandbox.docker_binary
        if not shutil.which(d):
            _line(FAIL, f"docker binary '{d}' not found (install Docker Desktop, or set sandbox.mode: local)")
            failures += 1
        else:
            info = subprocess.run([d, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
            if info.returncode != 0:
                _line(FAIL, f"docker daemon not reachable: {info.stderr.strip()[:200]}")
                failures += 1
            else:
                _line(OK, f"docker server {info.stdout.strip()}")
                img = subprocess.run([d, "image", "inspect", cfg.sandbox.image], capture_output=True)
                if img.returncode == 0:
                    _line(OK, f"sandbox image {cfg.sandbox.image} present")
                else:
                    _line(FAIL, f"sandbox image {cfg.sandbox.image} missing: run `swarmforge sandbox build`")
                    failures += 1
    else:
        _line(WARN, "sandbox.mode is 'local': agent commands run UNSANDBOXED on this machine")

    tok = get_token(cfg.github)
    if tok:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{cfg.github.api_url}/user", headers={"Authorization": f"Bearer {tok}",
                                                                      "User-Agent": "swarmforge"})
            if r.status_code == 200:
                _line(OK, f"GitHub token valid (user {r.json().get('login')})")
            else:
                _line(WARN, f"GitHub token rejected (HTTP {r.status_code}); draft PRs will not be possible")
        except httpx.HTTPError as e:
            _line(WARN, f"could not verify GitHub token: {e}")
    else:
        _line(WARN, f"no GitHub token: public issues can be read, but draft PRs cannot be created")

    for pname, p in cfg.providers.items():
        if not p.pricing_url:
            continue
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(p.pricing_url)
                r.raise_for_status()
                ids = {m["id"]: m for m in r.json().get("data", [])}
            for name, role in cfg.roles.items():
                if role.provider != pname:
                    continue
                if role.model in ids:
                    pr = (ids[role.model].get("metadata") or {}).get("pricing") or {}
                    _line(OK, f"{name}: {role.model} listed; ${_p(pr.get('input_tokens'))}/M in, "
                              f"${_p(pr.get('output_tokens'))}/M out")
                else:
                    _line(FAIL, f"{name}: model {role.model} is NOT in {pname}'s model list")
                    failures += 1
                for fb in role.fallback_models:
                    if fb in ids:
                        _line(OK, f"{name}: fallback {fb} listed")
                    else:
                        _line(WARN, f"{name}: fallback model {fb} is NOT in {pname}'s model list (it will be skipped)")
        except Exception as e:
            _line(WARN, f"could not fetch model list from {p.pricing_url}: {e}")

    if live:
        llm = LLMClient(cfg, budget=Budget(1.0))
        tool = {"type": "function", "function": {
            "name": "report_status", "description": "Report a status word.",
            "parameters": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}}}
        for name in (roles or list(cfg.roles)):
            try:
                res = await llm.chat(name, [
                    {"role": "system", "content": "You are a connectivity test. Always use the provided tool."},
                    {"role": "user", "content": "Call report_status with status 'ready'."}], tools=[tool])
                u = res.usage
                cost = f"${u.cost_usd:.6f} ({u.cost_source})" if u.cost_usd is not None else "cost unavailable"
                if res.tool_calls:
                    _line(OK, f"live {name}: tool call {res.tool_calls[0]['function']['name']}"
                              f"({res.tool_calls[0]['function'].get('arguments')}) tokens={u.total_tokens} {cost}")
                else:
                    _line(FAIL, f"live {name}: model answered without a tool call: {res.content[:120]!r}")
                    failures += 1
            except LLMError as e:
                _line(FAIL, f"live {name}: {e}")
                failures += 1
    print(f"\n{'all checks passed' if not failures else f'{failures} check(s) failed'}")
    return 1 if failures else 0
