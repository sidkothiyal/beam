"""Configuration system — Pydantic models, YAML loader, CLI override merging."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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


class CredentialsConfig(BaseModel):
    kaggle: str = "~/.kaggle/kaggle.json"
    env_vars: list[str] = Field(
        default_factory=lambda: ["WANDB_API_KEY", "HUGGING_FACE_HUB_TOKEN"]
    )


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


class PulseConfig(BaseModel):
    gpu_idle_threshold: int = 5
    gpu_idle_max_strikes: int = 3
    gpu_idle_samples: int = 5
    gpu_idle_sample_interval: int = 2
    max_retries: int = 5
    auto_stop_on_complete: bool = True


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

    # Environment variable overrides (provider API keys are NOT part of the
    # config model — they are read at runtime by the provider clients).
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
