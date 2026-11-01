"""Settings & health page logic and UI (dashboard Settings page)."""
from __future__ import annotations

import asyncio
import copy
import shutil
import subprocess
from pathlib import Path

import httpx
import yaml

from .config import Config, ConfigError, parse_config


def environment_problems(cfg: Config) -> list[str]:
    """Thin wrapper over preflight.environment_problems (monkeypatchable in tests)."""
    from . import preflight
    return preflight.environment_problems(cfg)


def github_access(cfg: Config) -> tuple[bool, str]:
    """Check that a GitHub token is present and valid. Never raises."""
    try:
        from . import github
        token = github.get_token(cfg.github)
        if not token:
            return False, "set GITHUB_TOKEN in the .env next to swarmforge.yaml or run `gh auth login`"
        r = httpx.get(f"{cfg.github.api_url}/user",
                      headers={"Authorization": f"Bearer {token}", "User-Agent": "swarmforge"},
                      timeout=10)
        if r.status_code == 200:
            return True, ""
        return False, f"GitHub rejected the token (HTTP {r.status_code}); check the token in .env"
    except Exception as exc:  # noqa: BLE001 - a broken check must never crash the page
        return False, f"GitHub not reachable ({exc}); check network or github.api_url"


# canonical row name -> substring used to find the matching problem string
CHECK_SPECS: list[tuple[str, str]] = [
    ("Provider API keys", "API key"),
    ("Provider base_url", "base_url"),
    ("GitHub CLI", "gh CLI"),
    ("Git", "Git is not installed"),
    ("Docker running", "Docker"),
    ("Sandbox image built", "sandbox image"),
]


def _split_problem(text: str) -> tuple[str, str]:
    """Split a preflight problem string into (message, fix)."""
    if " Fix:" in text:
        msg, _, fix = text.partition(" Fix:")
        return msg.strip(), fix.strip()
    if "Fix:" in text:
        msg, _, fix = text.partition("Fix:")
        return msg.strip(), fix.strip()
    return text.strip(), ""


def get_health_checks(cfg: Config) -> list[dict]:
    """One structured row per preflight check plus GitHub access. Never raises."""
    rows: list[dict] = []
    try:
        problems = list(environment_problems(cfg))
    except Exception as exc:  # noqa: BLE001
        problems = [f"Environment check failed: {exc}"]
    matched: set[int] = set()
    docker_optional = cfg.sandbox.mode != "docker"
    for name, needle in CHECK_SPECS:
        hit = next((i for i, p in enumerate(problems) if needle in p), None)
        if hit is None:
            if docker_optional and name in ("Docker running", "Sandbox image built"):
                rows.append({"name": name, "status": "ok",
                             "message": "not used (sandbox.mode: local)", "fix": "",
                             "optional": True})
            else:
                rows.append({"name": name, "status": "ok", "message": "", "fix": "",
                             "optional": name in ("GitHub CLI", "Git")})
            continue
        matched.add(hit)
        msg, fix = _split_problem(problems[hit])
        optional = name in ("GitHub CLI", "Git") or (docker_optional and name in
                                                     ("Docker running", "Sandbox image built"))
        rows.append({"name": name, "status": "fail", "message": msg, "fix": fix,
                     "optional": optional})
    for i, p in enumerate(problems):
        if i in matched:
            continue
        msg, fix = _split_problem(p)
        rows.append({"name": msg.split(".")[0].strip(), "status": "fail", "message": msg,
                     "fix": fix, "optional": False})
    try:
        gh_ok, gh_fix = github_access(cfg)
    except Exception as exc:  # noqa: BLE001
        gh_ok, gh_fix = False, f"Check failed: {exc}"
    rows.append({"name": "GitHub access", "status": "ok" if gh_ok else "fail",
                 "message": "" if gh_ok else "GitHub token missing or invalid",
                 "fix": gh_fix, "optional": True})
    return rows


def provider_models(cfg: Config, provider_name: str) -> list[str] | None:
    """Fetch the provider's model list; None when unreachable. Never raises."""
    try:
        pc = cfg.providers[provider_name]
        headers = {"User-Agent": "swarmforge"}
        key = pc.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = httpx.get(f"{pc.base_url}/models", headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        return sorted(m["id"] for m in r.json().get("data", []))
    except Exception:  # noqa: BLE001
        return None


def test_models(cfg: Config, roles: list[str] | None = None) -> list[dict]:
    """Send the same tiny tool-calling request as `checks.run_checks(live=True)` per role."""
    from .llm import Budget, LLMClient, LLMError

    async def _run() -> list[dict]:
        llm = LLMClient(cfg, budget=Budget(1.0), max_retries=1, busy_wait_s=0.0)
        tool = {"type": "function", "function": {
            "name": "report_status", "description": "Report a status word.",
            "parameters": {"type": "object", "properties": {"status": {"type": "string"}},
                           "required": ["status"]}}}
        out: list[dict] = []
        for name in (roles or list(cfg.roles)):
            row = {"role": name, "model": cfg.role(name).model, "ok": False, "detail": "",
                   "total_tokens": None, "cost_usd": None, "cost_source": "unavailable"}
            try:
                res = await llm.chat(name, [
                    {"role": "system", "content": "You are a connectivity test. Always use the provided tool."},
                    {"role": "user", "content": "Call report_status with status 'ready'."}],
                    tools=[tool])
                u = res.usage
                row["total_tokens"] = u.total_tokens
                row["cost_usd"] = u.cost_usd
                row["cost_source"] = u.cost_source
                if res.tool_calls:
                    tc = res.tool_calls[0]["function"]
                    row["ok"] = True
                    row["detail"] = f"{tc['name']}({tc.get('arguments')})"
                else:
                    row["detail"] = f"no tool call: {res.content!r}"
            except LLMError as e:
                row["detail"] = str(e)
            except Exception as e:  # noqa: BLE001
                row["ok"] = False
                row["detail"] = str(e)
            out.append(row)
        return out

    return asyncio.run(_run())


def build_sandbox_image(cfg: Config, on_line=None) -> int:
    """Stream the same docker build DockerSandbox.build_image runs; return the exit code."""
    from importlib import resources

    dockerfile = resources.files("swarmforge").joinpath("resources/Dockerfile.sandbox")
    dockerfile_dir = str(dockerfile.parent)
    if not shutil.which(cfg.sandbox.docker_binary):
        if on_line:
            on_line("docker binary not found")
        return 127
    cmd = [cfg.sandbox.docker_binary, "build", "-f", str(dockerfile),
           "--build-arg", f"BASE_IMAGE={cfg.sandbox.base_image}",
           "-t", cfg.sandbox.image, dockerfile_dir]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line:
            on_line(line.rstrip("\n"))
    return proc.wait()


def read_raw(path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def apply_edits(raw: dict, model_edits: dict[str, dict], limits_edits: dict) -> dict:
    """Return a deep copy of raw with role/limit edits applied. Providers are untouched."""
    new = copy.deepcopy(raw)
    roles = new.setdefault("roles", {})
    for role, fields in (model_edits or {}).items():
        roles.setdefault(role, {}).update(fields)
    new.setdefault("limits", {}).update(limits_edits or {})
    return new


def validate_for_save(raw: dict, path) -> list[str]:
    try:
        parse_config(raw, source_path=Path(path))
        return []
    except ConfigError as e:
        return [str(e)]


def save_config_with_backup(path, raw: dict) -> Path:
    p = Path(path)
    backup = p.with_name(p.name + ".bak")
    if p.exists():
        backup.write_bytes(p.read_bytes())
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return backup
