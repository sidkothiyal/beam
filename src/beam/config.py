"""Configuration system — Pydantic models, YAML loader, CLI override merging."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Credential → env-var name mapping (used in resolution and setup_creds)
# ---------------------------------------------------------------------------

# Maps CredentialsConfig field name → canonical env var name.
# Only launch credentials (needed locally to provision machines / push images).
CRED_ENV_MAP: dict[str, str] = {
    "runpod_api_key": "RUNPOD_API_KEY",
    "vast_api_key": "VAST_API_KEY",
    "github_username": "GITHUB_USERNAME",
    "docker_ghcr_token": "DOCKER_GHRC_TOKEN",
}

# Reverse mapping: env var name → field name
_ENV_TO_CRED_FIELD: dict[str, str] = {v: k for k, v in CRED_ENV_MAP.items()}


# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class ProjectConfig(BaseModel):
    name: str = ""
    docker_image: str = ""
    dep_files: list[str] = Field(default_factory=lambda: ["pyproject.toml", "uv.lock"])


class RegistryConfig(BaseModel):
    ghcr_image_name: str = "loom-env"
    ghcr_tag: str = "latest"


class RemoteConfig(BaseModel):
    project_dir: str = "/workspace"
    home: str = "/root"
    user: str = "root"


class SSHConfig(BaseModel):
    connect_timeout: int = 20
    retry_attempts: int = 5
    retry_interval: int = 10
    identity_file: str = ""


class CredentialFile(BaseModel):
    """A local file to sync to the remote machine."""
    local: str          # local path (supports ~)
    remote: str         # path relative to remote_home (e.g. ".kaggle/kaggle.json")
    chmod: str = "600"  # permissions to set after upload


class CredentialsConfig(BaseModel):
    # -- Launch credentials (needed locally; set in beam.yaml or via env vars) --
    # Used to provision cloud machines and push Docker images.
    # Env vars always take priority over yaml values.
    runpod_api_key: str = ""
    vast_api_key: str = ""
    github_username: str = ""
    docker_ghcr_token: str = ""

    # -- Env vars to forward to the remote machine's ~/.bashrc --
    # Launch credentials above are forwarded automatically.
    # Put remote-only tokens here (WANDB_API_KEY, HUGGING_FACE_HUB_TOKEN, etc.)
    # and any other arbitrary env vars to sync.
    env_vars: list[str] = Field(default_factory=list)

    # -- Credential files to sync to the remote machine --
    credential_files: list[CredentialFile] = Field(default_factory=list)

    def get_env_value(self, env_var_name: str) -> str:
        """Return the resolved value for *env_var_name*.

        Checks the named launch credential fields first (already resolved from
        yaml + env in ``load_config``), then falls back to ``os.environ``.
        """
        field = _ENV_TO_CRED_FIELD.get(env_var_name)
        if field:
            return getattr(self, field, "") or os.environ.get(env_var_name, "")
        return os.environ.get(env_var_name, "")

    def resolved_env_vars(self) -> dict[str, str]:
        """Return a dict of all env vars (from ``env_vars`` list) resolved to
        their current values. Entries with no value are omitted."""
        result: dict[str, str] = {}
        for var in self.env_vars:
            value = self.get_env_value(var)
            if value:
                result[var] = value
        return result


class RunPodMachineConfig(BaseModel):
    name: str = ""
    gpu_type: str = ""
    gpu_count: int = 1
    cloud_tier: str = "community"
    spot: bool = False
    recovery_policy: str = "restart"
    image: str = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
    template_id: str = ""
    container_disk_size: int = 30
    volume_size: int = 50
    volume_path: str = "/workspace"
    network_volume_id: str = ""
    min_memory_gb: int = 20
    min_vcpu: int = 1
    max_cost: float | None = None
    ports: list[str] = Field(default_factory=lambda: ["22/tcp"])
    ssh_public_key_files: list[str] = Field(default_factory=list)
    env_vars: str = ""


class VastAIMachineConfig(BaseModel):
    name: str = ""
    instance_label: str = ""
    gpu_name: str = ""
    num_gpus: int = 1
    num_gpus_max: int | None = None
    gpu_ram_min: float | None = None  # minimum per-GPU VRAM in GB
    machine_id: int | None = None
    offer_type: str = "bid"
    offer_limit: int = 10
    price: float = 0.16
    price_max: float = 0.20
    price_step: float = 0.005
    disk_gb: int = 50
    runtype: str = "ssh"
    target_state: str = "running"
    recovery_policy: str = "new"
    image: str = ""
    onstart: str = ""
    env: str = ""
    pick_strategy: str = "best_value"  # how to rank offers: cheapest, best_value, score
    bid_strategy: str = "multiplier"   # how to price the bid: multiplier, percentile, fixed
    bid_multiplier: float = 1.1
    bid_percentile: float = 0.5
    reliability_min: float | None = None
    dph_max: float | None = None
    dlperf_min: float | None = None
    dlperf_per_dphtotal_min: float | None = None


class RunPodProviderConfig(BaseModel):
    configs: list[RunPodMachineConfig] = Field(default_factory=list)
    fallback_order: list[str] = Field(default_factory=list)


class VastAIProviderConfig(BaseModel):
    configs: list[VastAIMachineConfig] = Field(default_factory=list)
    fallback_order: list[str] = Field(default_factory=list)


class ProvidersConfig(BaseModel):
    runpod: RunPodProviderConfig = Field(default_factory=RunPodProviderConfig)
    vastai: VastAIProviderConfig = Field(default_factory=VastAIProviderConfig)


class AgentLLMConfig(BaseModel):
    """LLM backend for pulse agent mode (litellm OpenAI-compatible gateway)."""
    provider: str = "nim"                        # nim | openrouter | openai
    model: str = "qwen/qwen3.5-397b-a17b"        # NIM default
    base_url: str | None = None                  # else PROVIDER_BASE[provider]
    # Names the env var / secret holding the API key — never the key itself
    # (beam.yaml is committed). Resolved via the 3-tier secret resolver
    # (env -> ~/.beam/agent.env -> keyring) at check time.
    api_key_env: str = "NVIDIA_API_KEY"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: int = 90                            # per-call ceiling (seconds)
    retries: int = 3                             # litellm num_retries
    max_tool_iterations: int = 8                 # tool-calling loop cap


class PulseAgentConfig(BaseModel):
    """Agentic diagnosis layer over the deterministic pulse decision matrix.

    When ``enabled`` is False (default), pulse behaves exactly as before. When
    enabled, "interesting" health states are routed to an LLM tool-calling loop
    that investigates (reads logs/history/metrics) and emits a recovery
    decision mapped onto the existing ``do_*`` actions. Always degrades to the
    deterministic matrix on any LLM failure.
    """
    enabled: bool = False                        # master switch

    llm: AgentLLMConfig = Field(default_factory=AgentLLMConfig)

    # -- Gating: which deterministic observations invoke the agent. --
    # Healthy 'running' is excluded to save cost/latency; GPU-idle handling
    # remains deterministic in v1 (the existing strikes mechanism is well-tuned).
    trigger_on: list[str] = Field(default_factory=lambda: [
        "failed", "crashed", "outbid", "provider_down",
    ])

    # -- Tool toggles --
    read_logs: bool = True
    log_tail_lines: int = 400
    log_max_bytes: int = 65536
    disk_check: bool = True
    history_lookback: int = 50
    read_job_log: bool = True             # expose the unified per-job run+pulse log
    job_log_tail_lines: int = 300

    # -- Canonical per-project doc (operator knowledge fed to the agent) --
    # Path relative to the repo root; "" disables. Human content is preserved;
    # the agent only maintains its own marker-delimited section.
    project_doc: str = "BEAM_AGENT.md"
    project_doc_max_bytes: int = 8192

    use_wandb: bool = True
    wandb_entity: str = ""
    wandb_project: str = ""

    allow_server_switch: bool = True

    # -- Safety --
    advisory: bool = False                       # dry-run: log verdict, take no action
    max_agent_restarts: int = 8                  # agent-driven restart cap per session
    crash_loop_window: int = 3600               # seconds to scan for repeated root_cause
    crash_loop_threshold: int = 4               # same root_cause N times -> escalate
    escalate_command: str = ""                  # optional shell hook on escalate


class PulseConfig(BaseModel):
    gpu_idle_threshold: int = 5
    gpu_idle_max_strikes: int = 3
    gpu_idle_samples: int = 5
    gpu_idle_sample_interval: int = 2
    max_retries: int = 5
    auto_stop_on_complete: bool = True
    agent: PulseAgentConfig = Field(default_factory=PulseAgentConfig)


class BeamConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    pulse: PulseConfig = Field(default_factory=PulseConfig)


# ---------------------------------------------------------------------------
# Config discovery and loading
# ---------------------------------------------------------------------------

CONFIG_FILENAMES = ("beam.yaml", "beam.yml")


def find_config_file(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for beam.yaml."""
    current = (start or Path.cwd()).resolve()
    for _ in range(50):  # safety bound
        for name in CONFIG_FILENAMES:
            candidate = current / name
            if candidate.is_file():
                return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_config(
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> BeamConfig:
    """Load config with resolution: defaults -> YAML -> env -> overrides.

    *overrides* is a flat dict of dotted-path keys produced by the CLI, e.g.
    ``{"remote.user": "ubuntu", "ssh.connect_timeout": 30}``.
    """
    raw: dict[str, Any] = {}
    resolved_path = config_path or find_config_file()
    if resolved_path and resolved_path.is_file():
        raw = load_yaml(resolved_path)

    cfg = BeamConfig.model_validate(raw)

    # Resolve named credentials: env vars take priority over yaml values.
    for field, env_var in CRED_ENV_MAP.items():
        env_val = os.environ.get(env_var, "")
        if env_val:
            setattr(cfg.credentials, field, env_val)

    # Environment variable overrides
    if os.environ.get("BEAM_REMOTE_USER"):
        cfg.remote.user = os.environ["BEAM_REMOTE_USER"]
    if os.environ.get("BEAM_REMOTE_PROJECT_DIR"):
        cfg.remote.project_dir = os.environ["BEAM_REMOTE_PROJECT_DIR"]

    # CLI overrides
    if overrides:
        _apply_overrides(cfg, overrides)

    return cfg


def _apply_overrides(cfg: BeamConfig, overrides: dict[str, Any]) -> None:
    """Apply flat dotted-path overrides to a loaded config."""
    for key, value in overrides.items():
        if value is None:
            continue
        parts = key.split(".")
        obj: Any = cfg
        for part in parts[:-1]:
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is not None:
            field = parts[-1]
            if hasattr(obj, field):
                setattr(obj, field, value)


def get_project_root(config_path: Path | None = None) -> Path:
    """Return the project root — directory containing beam.yaml, or cwd."""
    found = config_path or find_config_file()
    if found:
        return found.parent
    return Path.cwd().resolve()
