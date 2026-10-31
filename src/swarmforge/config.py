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
    # tried in order when the main model is busy/unavailable for longer than limits.busy_wait_s
    fallback_models: list[str] = field(default_factory=list)


@dataclass
class Limits:
    budget_usd: float = 20.0
    max_runtime_s: int = 21600     # 6 hours
    busy_wait_s: int = 900         # keep retrying a busy/rate-limited model this long before falling back
    max_concurrent_agents: int = 2
    one_run_per_repo: bool = True     # a run waits while an earlier run on the same repository is active
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
    dependency_install: bool = True   # install the project's declared packages (network only for that step)
    install_network: str = "bridge"
    install_timeout_s: int = 900
    reuse_container: bool = True      # agents' run_command execs into one long-lived container per agent
    dev_tools: bool = True            # processes, headless browser and check_code for developers/tester


@dataclass
class WatchConfig:
    """`swarmforge watch`: turn labelled GitHub issues into queued runs."""
    repos: list = field(default_factory=list)   # e.g. ["bigdan88/swarmforge"]
    label: str = "swarmforge"                   # add this label to an issue to hand it to the swarm
    interval_s: int = 120
    approval: str = "dashboard"                 # dashboard (ask before pushing) | yes (push the draft PR)
    budget_usd: float | None = None
    max_cycles: int | None = None
    comment: bool = True                        # comment on the issue when queued / finished


@dataclass
class ProjectConfig:
    test_command: str = "python -m pytest -q"
    agent_test_command: str | None = None  # quicker suite agents run while working (optional)
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
class SelfHealConfig:
    enabled: bool = True
    max_attempts: int = 2              # automatic retries after a failure (0 = never retry)
    time_factor: float = 1.5           # each retry gets this much more time (compounding) ...
    budget_factor: float = 1.5         # ... and budget ...
    extra_repair_cycles: int = 1       # ... and repair cycles ...
    steps_factor: float = 1.5          # ... and agent steps per session
    max_total_budget_usd: float = 0.0  # cap for a run plus all its retries; 0 = 3x the first run's budget
    diagnose: bool = True              # ask a model why a run failed (cheap, ~1 call)
    diagnosis_role: str = "planner"
    diagnosis_budget_usd: float = 0.5


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
    self_heal: SelfHealConfig = field(default_factory=SelfHealConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    active_variant: str | None = None            # variant id from `swarmforge variants` applied to every run
    prompt_addenda: dict = field(default_factory=dict)   # filled from the active variant (role group -> text)
    variant_applied: bool = False                # runtime flag: the variant overlay is already applied
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
        self_heal=_build(SelfHealConfig, raw.get("self_heal"), "self_heal"),
        watch=_build(WatchConfig, raw.get("watch"), "watch"),
        active_variant=(str(raw["active_variant"]) if raw.get("active_variant") else None),
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
    sh = cfg.self_heal
    if not 0 <= sh.max_attempts <= 5:
        raise ConfigError("self_heal.max_attempts must be between 0 and 5")
    if sh.time_factor < 1 or sh.budget_factor < 1 or sh.steps_factor < 1:
        raise ConfigError("self_heal factors must be >= 1")
    if sh.diagnosis_role not in cfg.roles:
        sh.diagnosis_role = "planner"
    if cfg.sandbox.mode not in ("docker", "local"):
        raise ConfigError("sandbox.mode must be 'docker' or 'local'")
    if cfg.sandbox.install_network not in ("none", "bridge"):
        raise ConfigError("sandbox.install_network must be 'none' or 'bridge'")
    if cfg.sandbox.network not in ("none", "bridge"):
        raise ConfigError("sandbox.network must be 'none' or 'bridge'")
    if not cfg.github.branch_prefix or cfg.github.branch_prefix in ("main", "master"):
        raise ConfigError("github.branch_prefix must be a non-protected prefix like 'swarmforge/'")
    sev = {"critical", "major", "minor", "info"}
    if not set(cfg.review.blocking_severities) <= sev:
        raise ConfigError(f"review.blocking_severities must be a subset of {sorted(sev)}")
    if not cfg.review.allow_same_model_review:
        rev = cfg.roles["reviewer"]
        rev_models = {rev.model, *rev.fallback_models}
        for d in cfg.developer_roles:
            dr = cfg.roles[d]
            overlap = rev_models & {dr.model, *dr.fallback_models}
            if dr.provider == rev.provider and overlap:
                raise ConfigError(
                    f"reviewer and developer role '{d}' share model(s) {sorted(overlap)} (including fallbacks); "
                    "an implementation model must not approve its own work.")
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
