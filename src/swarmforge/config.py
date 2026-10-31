"""YAML configuration loading and validation.

All settings live in one YAML file (see swarmforge.example.yaml). Secrets are never
stored in the YAML: providers reference an environment variable *name* instead.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

AGENT_ROLES = ("planner", "reviewer", "tester")  # plus any developer roles


class ConfigError(ValueError):
    pass


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str
    pricing_url: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


@dataclass
class RoleConfig:
    name: str
    provider: str
    model: str
    temperature: float = 0.2
    max_steps: int = 40
    timeout_s: int = 900
    max_output_tokens: int = 8192
    # Optional manual price override (USD per 1M tokens) used for estimates.
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class Limits:
    budget_usd: float = 20.0
    max_runtime_s: int = 3600
    max_concurrent_agents: int = 2
    max_repair_cycles: int = 3
    max_tasks: int = 8
    max_conflict_resolutions: int = 3
    task_retries: int = 1          # extra attempts for a coding task whose agent failed
    max_planning_attempts: int = 2


@dataclass
class SandboxConfig:
    mode: str = "docker"  # docker | local
    image: str = "swarmforge-sandbox:latest"
    base_image: str = "python:3.12-slim"
    network: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 256
    command_timeout_s: int = 300
    docker_binary: str = "docker"


@dataclass
class ProjectConfig:
    test_command: str = "python -m pytest -q"
    setup_command: str | None = None


@dataclass
class GitHubConfig:
    token_env: str = "GITHUB_TOKEN"
    branch_prefix: str = "swarmforge/"
    require_push_approval: bool = True
    create_pr: bool = True
    api_url: str = "https://api.github.com"


@dataclass
class ReviewConfig:
    blocking_severities: list[str] = field(default_factory=lambda: ["critical", "major"])
    allow_same_model_review: bool = False


@dataclass
class KnowledgeConfig:
    enabled: bool = True
    vault: str = "knowledge"           # folder of Markdown notes; relative to the config file
    librarian_role: str = "librarian"  # role that distills notes after a run (falls back to planner)
    max_notes_in_prompt: int = 6
    max_notes_per_run: int = 6

    resolved_vault: Path | None = None


@dataclass
class ApprovalConfig:
    timeout_s: int = 3600              # how long a dashboard approval request waits before declining


@dataclass
class Config:
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    developer_roles: list[str]
    limits: Limits = field(default_factory=Limits)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    workdir: Path = field(default_factory=lambda: Path("~/.swarmforge").expanduser())
    source_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.workdir / "swarmforge.db"

    @property
    def librarian_role(self) -> str:
        r = self.knowledge.librarian_role
        return r if r in self.roles else "planner"

    @property
    def vault_path(self) -> Path:
        return self.knowledge.resolved_vault or (Path.cwd() / self.knowledge.vault)

    def role(self, name: str) -> RoleConfig:
        if name not in self.roles:
            raise ConfigError(f"role '{name}' is not configured")
        return self.roles[name]

    def provider_for(self, role: str) -> ProviderConfig:
        return self.providers[self.role(role).provider]


def _build(dc, data: dict | None, ctx: str):
    data = dict(data or {})
    known = {f for f in dc.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{ctx}: unknown keys {sorted(unknown)}")
    try:
        return dc(**data)
    except TypeError as e:  # pragma: no cover - defensive
        raise ConfigError(f"{ctx}: {e}") from e


def parse_config(raw: dict, source_path: Path | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    providers = {}
    for name, p in (raw.get("providers") or {}).items():
        providers[name] = _build(ProviderConfig, {"name": name, **(p or {})}, f"providers.{name}")
    if not providers:
        raise ConfigError("at least one provider is required")

    roles = {}
    for name, r in (raw.get("roles") or {}).items():
        roles[name] = _build(RoleConfig, {"name": name, **(r or {})}, f"roles.{name}")
        if roles[name].provider not in providers:
            raise ConfigError(f"roles.{name}: unknown provider '{roles[name].provider}'")

    dev_roles = list(raw.get("developer_roles") or [])
    if not dev_roles:
        raise ConfigError("developer_roles must list at least one role")
    for r in ["planner", "reviewer", "tester", *dev_roles]:
        if r not in roles:
            raise ConfigError(f"role '{r}' must be configured under roles:")

    workdir = Path(os.path.expandvars(str(raw.get("workdir", "~/.swarmforge")))).expanduser()
    if not workdir.is_absolute() and source_path is not None:
        workdir = (source_path.parent / workdir).resolve()

    cfg = Config(
        providers=providers,
        roles=roles,
        developer_roles=dev_roles,
        limits=_build(Limits, raw.get("limits"), "limits"),
        sandbox=_build(SandboxConfig, raw.get("sandbox"), "sandbox"),
        project=_build(ProjectConfig, raw.get("project"), "project"),
        github=_build(GitHubConfig, raw.get("github"), "github"),
        review=_build(ReviewConfig, raw.get("review"), "review"),
        knowledge=_build(KnowledgeConfig, raw.get("knowledge"), "knowledge"),
        approval=_build(ApprovalConfig, raw.get("approval"), "approval"),
        workdir=workdir,
        source_path=source_path,
    )
    vault = Path(os.path.expandvars(cfg.knowledge.vault)).expanduser()
    if not vault.is_absolute():
        vault = ((source_path.parent if source_path else Path.cwd()) / vault).resolve()
    cfg.knowledge.resolved_vault = vault
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    lim = cfg.limits
    if lim.budget_usd <= 0:
        raise ConfigError("limits.budget_usd must be > 0")
    if lim.max_concurrent_agents < 1:
        raise ConfigError("limits.max_concurrent_agents must be >= 1")
    if not 0 <= lim.max_repair_cycles <= 10:
        raise ConfigError("limits.max_repair_cycles must be between 0 and 10")
    if cfg.sandbox.mode not in ("docker", "local"):
        raise ConfigError("sandbox.mode must be 'docker' or 'local'")
    if cfg.sandbox.network not in ("none", "bridge"):
        raise ConfigError("sandbox.network must be 'none' or 'bridge'")
    if not cfg.github.branch_prefix or cfg.github.branch_prefix in ("main", "master"):
        raise ConfigError("github.branch_prefix must be a non-protected prefix like 'swarmforge/'")
    sev = {"critical", "major", "minor", "info"}
    if not set(cfg.review.blocking_severities) <= sev:
        raise ConfigError(f"review.blocking_severities must be a subset of {sorted(sev)}")
    if not cfg.review.allow_same_model_review:
        rev = cfg.roles["reviewer"]
        for d in cfg.developer_roles:
            dr = cfg.roles[d]
            if (dr.provider, dr.model) == (rev.provider, rev.model):
                raise ConfigError(
                    f"reviewer uses the same model as developer role '{d}' ({rev.model}). "
                    "An implementation model must not approve its own work; pick a different "
                    "reviewer model or set review.allow_same_model_review: true explicitly."
                )


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def find_config(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else [
        Path("swarmforge.yaml"),
        Path("swarmforge.yml"),
        Path("~/.swarmforge/swarmforge.yaml").expanduser(),
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise ConfigError(
        "no config file found; copy swarmforge.example.yaml to swarmforge.yaml "
        "or pass --config PATH"
    )


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    load_dotenv(path.parent / ".env")
    load_dotenv(Path.cwd() / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_config(raw, source_path=path)
