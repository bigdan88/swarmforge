"""Settings & health page logic and UI (dashboard Settings page)."""
from __future__ import annotations

import asyncio
import copy
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import yaml

from .config import Config, ConfigError, parse_config


def environment_check_results(cfg: Config) -> list[dict]:
    """Thin wrapper over preflight.environment_check_results (monkeypatchable in tests)."""
    from . import preflight
    return preflight.environment_check_results(cfg)


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


# canonical health-row names, in display order
CANONICAL_CHECKS = ["Provider API keys", "Provider base_url", "GitHub CLI", "Git",
                   "Docker running", "Sandbox image built"]


def get_health_checks(cfg: Config) -> list[dict]:
    """One structured row per preflight check plus GitHub access. Never raises.

    Rows from environment_check_results are mapped by exact canonical name (no prose parsing);
    unknown names pass through as extra rows."""
    try:
        results = list(environment_check_results(cfg))
    except Exception as exc:  # noqa: BLE001
        results = [{"name": "Environment checks", "status": "fail",
                    "message": f"Environment check failed: {exc}", "fix": ""}]
    indexed: dict[str, dict] = {}
    for r in results:
        name = str(r.get("name", ""))
        if name in indexed:
            prev = indexed[name]
            prev["status"] = "fail" if "fail" in (prev["status"], r.get("status")) else "ok"
            prev["message"] = "; ".join(x for x in (prev["message"], r.get("message", "")) if x)
            prev["fix"] = "; ".join(x for x in (prev["fix"], r.get("fix", "")) if x)
        else:
            indexed[name] = {"name": name, "status": r.get("status", "ok"),
                             "message": r.get("message", ""), "fix": r.get("fix", "")}
    docker_optional = cfg.sandbox.mode != "docker"
    rows: list[dict] = []
    for name in CANONICAL_CHECKS:
        optional = name in ("GitHub CLI", "Git") or \
            (docker_optional and name in ("Docker running", "Sandbox image built"))
        if name in indexed:
            r = indexed.pop(name)
            rows.append({"name": name, "status": r["status"], "message": r["message"],
                         "fix": r["fix"], "optional": optional})
        elif docker_optional and name in ("Docker running", "Sandbox image built"):
            rows.append({"name": name, "status": "ok",
                         "message": "not used (sandbox.mode: local)", "fix": "",
                         "optional": True})
        else:
            rows.append({"name": name, "status": "ok", "message": "", "fix": "",
                         "optional": optional})
    for r in indexed.values():  # unknown names pass through verbatim
        rows.append({"name": r["name"], "status": r["status"], "message": r["message"],
                     "fix": r["fix"], "optional": False})
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


_KEY_LINE_RE = re.compile(r"^(\s*)(\S[^:#]*?)\s*:(\s.*)?$")


def _serialize_scalar(value) -> str | None:
    """Serialize a scalar to a single-line YAML value, or None if it cannot be done."""
    text = yaml.safe_dump(value, default_flow_style=False)
    lines = [ln for ln in text.splitlines() if ln != "..."]
    if len(lines) != 1:
        return None
    return lines[0]


def _split_trailing_comment(value_part: str) -> tuple[str, str]:
    """Split `' # comment'` off a value part, ignoring '#' inside quoted strings."""
    idx = value_part.find(" #")
    while idx != -1:
        before = value_part[:idx]
        if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
            return before, value_part[idx:]
        idx = value_part.find(" #", idx + 1)
    return value_part, ""


def _changed_scalar_paths(old, new, prefix=()):
    """Leaf paths whose value differs between old and new (dicts descended into).

    Returns a list of (path, new_value) or None when a change involves a container
    (dict/list) on either side, or a key removal — callers fall back to a full dump.
    """
    changes: list[tuple[tuple, object]] = []
    for key, value in new.items():
        if isinstance(value, dict):
            sub = old.get(key)
            if not isinstance(sub, dict):
                return None
            sub_changes = _changed_scalar_paths(sub, value, prefix + (key,))
            if sub_changes is None:
                return None
            changes.extend(sub_changes)
        elif isinstance(value, (list, tuple)):
            if old.get(key) != value:
                return None  # changed container: fall back
        else:
            if old.get(key) != value:
                changes.append((prefix + (key,), value))
    for key in old:
        if key not in new:
            return None
    return changes


def _parse_key_lines(lines: list[str]) -> dict:
    """Map key-path tuples to {'line', 'indent', 'value'} using an indent stack."""
    parsed: dict[tuple, dict] = {}
    stack: list[tuple[int, tuple]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_LINE_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = (stack[-1][1] + (m.group(2),)) if stack else (m.group(2),)
        parsed[path] = {"line": i, "indent": indent, "value": m.group(3) or ""}
        stack.append((indent, path))
    return parsed


def _block_extents(parsed: dict) -> dict:
    extents = {path: info["line"] for path, info in parsed.items()}
    for path, info in parsed.items():
        for depth in range(1, len(path)):
            ancestor = path[:depth]
            if ancestor in extents:
                extents[ancestor] = max(extents[ancestor], info["line"])
    return extents


def patch_config_text(original_text: str, old: dict, new: dict) -> str | None:
    """Return original_text with only the changed scalar values edited.

    Preserves comments, blank lines, indentation style and key order. Returns None
    when the layout cannot be matched (or a change involves containers); the caller
    then falls back to a full yaml.safe_dump.
    """
    try:
        lines = original_text.splitlines()
        # Whole top-level dict sections missing from the original are appended at the end.
        section_inserts: dict[tuple, dict] = {}
        for key, value in new.items():
            if isinstance(value, dict):
                if key not in old or old[key] is None:
                    section_inserts[(key,)] = value
                elif not isinstance(old[key], dict):
                    return None
        walk_new = {k: v for k, v in new.items() if (k,) not in section_inserts}
        walk_old = {k: v for k, v in old.items() if (k,) not in section_inserts}
        changes = _changed_scalar_paths(walk_old, walk_new)
        if changes is None:
            return None

        parsed = _parse_key_lines(lines)
        replacements = [(p, v) for p, v in changes if p in parsed]
        insertions = [(p, v) for p, v in changes if p not in parsed]

        # In-place value replacements keep line numbers valid.
        for path, value in replacements:
            info = parsed[path]
            m = _KEY_LINE_RE.match(lines[info["line"]])
            if m is None:
                return None
            value_part = m.group(3) or ""
            if value_part.strip().startswith(("|", ">")):
                return None  # block scalar: cannot patch in place safely
            _body, comment = _split_trailing_comment(value_part)
            serialized = _serialize_scalar(value)
            if serialized is None:
                return None
            lines[info["line"]] = (m.group(1) + m.group(2) + ": " + serialized
                                    + ((" " + comment) if comment else ""))

        # Insertions shift line numbers: re-parse between each one.
        for path, value in insertions:
            parsed = _parse_key_lines(lines)
            extents = _block_extents(parsed)
            parent = path[:-1]
            key = path[-1]
            if parent not in parsed:
                return None
            pinfo = parsed[parent]
            child_indents = [q_info["indent"] for q, q_info in parsed.items()
                             if q[:-1] == parent]
            indent = min(child_indents) if child_indents else pinfo["indent"] + 2
            serialized = _serialize_scalar(value)
            if serialized is None:
                return None
            lines.insert(extents[parent] + 1, " " * indent + f"{key}: {serialized}")

        for (key,), subtree in section_inserts.items():
            if not lines or lines[-1].strip():
                lines.append("")
            lines.append(yaml.safe_dump({key: subtree}, sort_keys=False).strip("\n"))
        return "\n".join(lines) + ("\n" if original_text.endswith("\n") else "")
    except Exception:  # noqa: BLE001 - any layout surprise falls back to a full dump
        return None


def save_config_with_backup(path, raw: dict) -> Path:
    """Write raw to path, backing up the previous file first.

    When the file already exists, only the changed values are edited in the original
    text so comments and formatting survive; unparseable/unexpected YAML falls back
    to a full yaml.safe_dump. Returns the backup path (backup only written if the
    file existed)."""
    p = Path(path)
    backup = p.with_name(p.name + ".bak")
    if p.exists():
        shutil.copy2(p, backup)
        text = p.read_text(encoding="utf-8")
        patched = None
        try:
            old = yaml.safe_load(text)
            if old is None:
                old = {}
            if isinstance(old, dict):
                patched = patch_config_text(text, old, raw)
        except Exception:  # noqa: BLE001 - unparseable original falls back to a full dump
            patched = None
        if patched is not None:
            p.write_text(patched, encoding="utf-8")
        else:
            p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return backup


# ==================================================================== UI ===
import os  # noqa: E402

import streamlit as st  # noqa: E402


def render(cfg) -> None:
    """Render the Settings & health page (called by dashboard.page_settings).

    Every helper is looked up as a module global at call time so tests can monkeypatch it."""
    flash = st.session_state.pop("set_saved", None)
    if flash:
        st.success(flash)
    if cfg is None:
        st.error("No swarmforge.yaml found. Start the dashboard with `swarmforge dashboard` "
                 "from your SwarmForge folder.")
        return
    st.header("Settings")

    # ------------------------------------------------------------- health
    st.subheader("Health")
    try:
        rows = get_health_checks(cfg)
    except Exception as exc:  # noqa: BLE001 - a broken check must never crash the page
        st.error(f"Health checks failed: {exc}")
        rows = []
    for row in rows:
        ok = row.get("status") == "ok"
        line = f"{'✅' if ok else '❌'} **{row['name']}**"
        if not ok and row.get("optional"):
            line += " (optional)"
        if row.get("message"):
            line += f" — {row['message']}"
        st.markdown(line)
        if not ok and row.get("fix"):
            st.code(row["fix"])

    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("Re-check", key="set_recheck"):
            st.rerun()
    with b2:
        if st.button("Build sandbox image", key="set_build"):
            with st.expander("Sandbox build output", expanded=True):
                box = st.empty()
                buf: list[str] = []

                def _on_line(line: str) -> None:
                    buf.append(line)
                    box.code("\n".join(buf))

                rc = build_sandbox_image(cfg, on_line=_on_line)
            if rc == 0:
                st.success("Sandbox image built.")
            else:
                st.error(f"Sandbox build failed (exit code {rc}).")
    with b3:
        if st.button("Test models", key="set_test_models"):
            with st.spinner("Testing models..."):
                results = test_models(cfg)
            for r in results:
                if r.get("cost_usd") is None:
                    cost = "cost unavailable"
                else:
                    cost = (f"tokens={r.get('total_tokens')}, cost=${r['cost_usd']:.6f} "
                            f"({r.get('cost_source', 'unavailable')})")
                st.markdown(f"{'✅' if r.get('ok') else '❌'} {r['role']} ({r['model']}): "
                            f"{r.get('detail', '')} — {cost}")

    # ----------------------------------------------------------- api keys
    st.caption("API keys are read from the environment: only whether each variable is set is shown "
               "here, and key values are never written to swarmforge.yaml.")
    for pname, pcfg in cfg.providers.items():
        st.markdown(f"Provider **{pname}** — key env var `{pcfg.api_key_env}`: "
                    f"{'set ✅' if os.environ.get(pcfg.api_key_env) else 'MISSING ❌'}")

    # ------------------------------------------------------------- models
    st.subheader("Models")
    chosen: dict[str, str] = {}
    for role, rcfg in cfg.roles.items():
        c_name, c_model, c_temp, c_steps = st.columns([2, 3, 1, 1])
        with c_name:
            st.markdown(f"**{role}**")
        with c_model:
            options = provider_models(cfg, rcfg.provider)
            free = ""
            if options is None:  # provider unreachable: offer the current model plus free text
                options = [rcfg.model]
                free = st.text_input("Other model", key=f"set_model_free_{role}") or ""
            if rcfg.model not in options:
                options = [rcfg.model, *options]
            picked = st.selectbox("Model", options, index=options.index(rcfg.model),
                                  key=f"set_model_{role}", accept_new_options=True)
            chosen[role] = free or picked or rcfg.model
        with c_temp:
            st.number_input("Temperature", 0.0, 2.0, step=0.1, format="%.2f",
                            value=min(2.0, max(0.0, float(rcfg.temperature))),
                            key=f"set_temp_{role}")
        with c_steps:
            st.number_input("Max steps", 1, 500,
                            value=min(500, max(1, int(rcfg.max_steps))), key=f"set_steps_{role}")

    # ------------------------------------------------------------- limits
    st.subheader("Limits")
    l1, l2, l3, l4 = st.columns(4)
    with l1:
        st.number_input("Budget (USD)", min_value=0.1, step=0.5, format="%.2f",
                        value=max(0.1, float(cfg.limits.budget_usd)), key="set_budget")
    with l2:
        st.number_input("Max runtime (s)", min_value=60, step=60,
                        value=max(60, int(cfg.limits.max_runtime_s)), key="set_runtime")
    with l3:
        st.number_input("Concurrent agents", min_value=1, max_value=8,
                        value=min(8, max(1, int(cfg.limits.max_concurrent_agents))),
                        key="set_agents")
    with l4:
        st.number_input("Repair cycles", min_value=0, max_value=10,
                        value=min(10, max(0, int(cfg.limits.max_repair_cycles))), key="set_cycles")

    # --------------------------------------------------------------- save
    st.caption("Saving validates the edited values first and only then writes swarmforge.yaml "
               "(keeping a swarmforge.yaml.bak backup; comments and formatting in the original "
               "file are preserved). API keys are never stored in the YAML.")
    if st.button("Save settings", type="primary", key="set_save"):
        if cfg.source_path is None:
            st.error("This config has no source file; nothing to save.")
            return
        raw = read_raw(cfg.source_path)
        model_edits = {role: {"model": chosen.get(role, rcfg.model),
                              "temperature": float(st.session_state[f"set_temp_{role}"]),
                              "max_steps": int(st.session_state[f"set_steps_{role}"])}
                       for role, rcfg in cfg.roles.items()}
        limits_edits = {"budget_usd": float(st.session_state["set_budget"]),
                        "max_runtime_s": int(st.session_state["set_runtime"]),
                        "max_concurrent_agents": int(st.session_state["set_agents"]),
                        "max_repair_cycles": int(st.session_state["set_cycles"])}
        updated = apply_edits(raw, model_edits, limits_edits)
        errors = validate_for_save(updated, cfg.source_path)
        if errors:
            for err in errors:
                st.error(err)
        else:
            save_config_with_backup(cfg.source_path, updated)
            st.cache_resource.clear()
            st.session_state["set_saved"] = (
                f"Saved {cfg.source_path.name} (backup: {cfg.source_path.name}.bak) — "
                "new values apply on the next page load.")
            st.rerun()
