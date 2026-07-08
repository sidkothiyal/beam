"""Pipeline orchestrator — runs all deployment steps in order."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from beam import log, session
from beam.config import BeamConfig, get_project_root
from beam.ssh import SSHClient, SSHInfo
from beam.steps import (
    build_venv as build_venv_mod,
    push_image as push_image_mod,
    ensure_machine as ensure_machine_mod,
    sync_venv as sync_venv_mod,
    sync_code as sync_code_mod,
    setup_creds as setup_creds_mod,
    setup_uv as setup_uv_mod,
    run_remote as run_remote_mod,
)


# Fallback runtime base image per provider when a machine config leaves
# ``base_image`` empty. Chosen so the base is cached on that provider's hosts,
# making only our thin apt+venv layers pull. Overridable in beam.yaml.
DEFAULT_BASE_IMAGES: dict[str, str] = {
    "runpod": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404",
    "vastai": "nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04",
}


def _provider_slug(provider: str) -> str:
    """Canonical short slug for a provider (used in docker/ghcr tags)."""
    if provider == "runpod":
        return "runpod"
    if provider in ("vast.ai", "vastai"):
        return "vastai"
    return ""


def _resolve_machine_config(cfg: BeamConfig, provider: str, pod_config_name: str):
    """Return the machine config for *provider* named *pod_config_name*,
    falling back to the first configured entry (or None)."""
    if provider == "runpod":
        configs = cfg.providers.runpod.configs
    elif provider in ("vast.ai", "vastai"):
        configs = cfg.providers.vastai.configs
    else:
        return None
    for mc in configs:
        if mc.name == pod_config_name:
            return mc
    return configs[0] if configs else None


class RunContext:
    """Carries mutable state through the pipeline."""

    def __init__(
        self,
        cfg: BeamConfig,
        repo_root: Path,
        *,
        provider: str = "",
        pod_id: str = "",
        pod_config_name: str = "",
        name: str = "",
        ip: str = "",
        port: str = "",
        command: str = "",
        pulse_interval: str = "",
        pulse_idle_stop: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.repo_root = repo_root
        self.provider = provider
        self.pod_id = pod_id
        self.pod_config_name = pod_config_name
        self.name = name
        self.ip = ip
        self.port = port
        self.command = command
        self.pulse_interval = pulse_interval
        self.pulse_idle_stop = pulse_idle_stop
        self.dry_run = dry_run

        # Populated during pipeline
        self.build_result: build_venv_mod.BuildResult | None = None
        self.ssh_client: SSHClient | None = None
        self.session_name: str = ""
        self.ghcr_image: str = ""
        self.use_registry_venv: bool = False

        # Per-provider runtime base image (Dockerfile RUNTIME_BASE_IMAGE) and the
        # local docker tag for this build. Both keyed off the provider so runpod
        # and vast builds/images don't clobber one another.
        slug = _provider_slug(self.provider)
        self.image_tag = slug or "latest"
        self.base_image = ""
        if slug:
            mc = _resolve_machine_config(cfg, self.provider, self.pod_config_name)
            self.base_image = (getattr(mc, "base_image", "") or "").strip() if mc else ""
            if not self.base_image:
                self.base_image = DEFAULT_BASE_IMAGES.get(slug, "")
            # Known provider but nothing to build FROM (e.g. a newly added provider
            # with no base_image in beam.yaml and no DEFAULT_BASE_IMAGES entry). The
            # build still succeeds via the Dockerfile default, but the pull won't be
            # provider-optimized -- surface it loudly so it can be configured.
            if not self.base_image:
                log.warn(
                    f"No base_image resolved for provider '{self.provider}'. Falling "
                    f"back to the Dockerfile default base -- fresh-instance pulls may be "
                    f"slower. Set providers.{slug}.configs[*].base_image in beam.yaml to a "
                    f"base cached on this provider's hosts."
                )

        # Derived remote paths
        repo_name = cfg.project.name or repo_root.name
        self.remote_code_dir = f"{cfg.remote.project_dir}/{repo_name}"
        self.remote_venv_dir = f"{self.remote_code_dir}/.venv"
        self.remote_logs_dir = f"{cfg.remote.project_dir}/logs/{repo_name}"

        # Logs directory (local)
        self.logs_dir = repo_root / ".beam" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Registry image. The tag carries a provider suffix so each provider gets
        # its own base-specific image (e.g. loom-env:latest-runpod), while sharing
        # the identical venv layer (ghcr dedups it by digest).
        github_user = cfg.credentials.github_username
        if github_user:
            tag = cfg.registry.ghcr_tag
            if slug:
                tag = f"{tag}-{slug}"
            self.ghcr_image = (
                f"ghcr.io/{github_user}/"
                f"{cfg.registry.ghcr_image_name}:{tag}"
            )
        if self.provider and github_user:
            self.use_registry_venv = True


def run_local(ctx: RunContext) -> None:
    """Local mode — just exec the command."""
    if not ctx.command:
        log.done("Nothing to run (no command after --).")
        return

    log.success("Running locally ...")

    # Expose Python headers for torch.compile / triton
    try:
        py_include = subprocess.run(
            ["uv", "run", "python", "-c",
             "import sysconfig; print(sysconfig.get_path('include'))"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        os.environ["CFLAGS"] = f"-I{py_include} {os.environ.get('CFLAGS', '')}"
        os.environ["CPATH"] = f"{py_include}{':' + os.environ['CPATH'] if os.environ.get('CPATH') else ''}"
    except Exception:
        pass

    os.chdir(ctx.repo_root)
    os.execvp("bash", ["bash", "-c", ctx.command])


def run_remote(ctx: RunContext) -> None:
    """Remote mode — full deployment pipeline."""
    cfg = ctx.cfg
    total_steps = 7

    if not ctx.command:
        log.warn("No command provided -- will sync/setup only (no remote execution).")

    # Determine ordering based on whether we push to registry first
    if ctx.use_registry_venv and ctx.ghcr_image:
        # Provider + registry: build -> push -> ensure machine
        log.banner(1, total_steps, "Build venv locally")
        ctx.build_result = build_venv_mod.build_venv(
            cfg, ctx.repo_root, base_image=ctx.base_image, image_tag=ctx.image_tag
        )

        if ctx.build_result.built or os.environ.get("FORCE_PUSH") == "1":
            log.banner(2, total_steps, "Push image to ghcr.io")
            push_image_mod.push_image(cfg, ctx.ghcr_image, ctx.build_result.image_tag)
        else:
            log.banner(2, total_steps, "Push image to ghcr.io (skipped, up-to-date)")
            log.done("Image unchanged -- using existing registry image.")

        log.banner(3, total_steps, "Ensure machine is running")
        machine = ensure_machine_mod.ensure_machine(
            cfg,
            provider_name=ctx.provider,
            pod_id=ctx.pod_id,
            pod_config_name=ctx.pod_config_name,
            ip=ctx.ip,
            port=ctx.port,
            ghcr_image=ctx.ghcr_image,
        )
    else:
        # Manual --ip or provider without registry: ensure machine first
        log.banner(1, total_steps, "Ensure machine is running")
        machine = ensure_machine_mod.ensure_machine(
            cfg,
            provider_name=ctx.provider,
            pod_id=ctx.pod_id,
            pod_config_name=ctx.pod_config_name,
            ip=ctx.ip,
            port=ctx.port,
            ghcr_image=ctx.ghcr_image,
        )

        log.banner(2, total_steps, "Build venv locally")
        ctx.build_result = build_venv_mod.build_venv(
            cfg, ctx.repo_root, base_image=ctx.base_image, image_tag=ctx.image_tag
        )

    # Update context from machine result
    ctx.pod_id = machine.pod_id
    ctx.ip = machine.ssh_info.host
    ctx.port = machine.ssh_info.port

    ctx.ssh_client = SSHClient(info=machine.ssh_info, cfg=cfg.ssh)

    log.info("")
    log.kv("Remote host", ctx.ssh_client.info.remote_host)
    log.kv("Remote code dir", ctx.remote_code_dir)
    log.kv("Remote venv", ctx.remote_venv_dir)
    log.kv("Remote logs", ctx.remote_logs_dir)
    if ctx.pod_id:
        log.kv("Pod ID", ctx.pod_id)
    if ctx.provider:
        log.kv("Provider", ctx.provider)

    docker_image = cfg.project.docker_image or cfg.project.name or "beam-venv"

    # Step 3 (or 4): sync venv
    log.banner(3, total_steps, "Sync venv to remote")
    assert ctx.build_result is not None
    sync_venv_mod.sync_venv(
        ctx.ssh_client,
        ctx.build_result,
        remote_code_dir=ctx.remote_code_dir,
        remote_venv_dir=ctx.remote_venv_dir,
        docker_image=docker_image,
        use_registry_venv=ctx.use_registry_venv,
    )

    # Step 4: sync code
    log.banner(4, total_steps, "Sync code to remote")
    sync_code_mod.sync_code(ctx.ssh_client, ctx.repo_root, ctx.remote_code_dir)

    # Step 5: credentials
    log.banner(5, total_steps, "Setup credentials")
    setup_creds_mod.setup_creds(ctx.ssh_client, cfg.credentials, cfg.remote.home)

    # Step 6: uv
    log.banner(6, total_steps, "Ensure uv on remote")
    setup_uv_mod.setup_uv(ctx.ssh_client, cfg.remote.home)

    # Step 7: run
    log.banner(7, total_steps, "Launch remote session")
    result = run_remote_mod.run_remote(
        ctx.ssh_client,
        ctx.command,
        remote_code_dir=ctx.remote_code_dir,
        remote_logs_dir=ctx.remote_logs_dir,
        remote_home=cfg.remote.home,
        name=ctx.name,
        env_vars=cfg.credentials.resolved_env_vars(),
    )
    remote_state_dir = ""
    if result:
        ctx.session_name = result.session_name
        remote_state_dir = result.remote_state_dir

    # Save session state (including machine config snapshot for pulse restarts)
    machine_cfg_dict = _snapshot_machine_config(ctx)
    session.save_session(
        ctx.logs_dir,
        session_id=ctx.session_name or f"setup-{int(__import__('time').time())}",
        provider=ctx.provider,
        pod_id=ctx.pod_id,
        ip=ctx.ip,
        port=ctx.port,
        user=cfg.remote.user,
        identity_file=cfg.ssh.identity_file,
        remote_project_dir=cfg.remote.project_dir,
        remote_home=cfg.remote.home,
        command=ctx.command,
        pod_config_name=ctx.pod_config_name,
        recovery_policy=_get_recovery_policy(ctx),
        ghcr_image=ctx.ghcr_image,
        remote_state_dir=remote_state_dir,
        remote_logs_dir=ctx.remote_logs_dir,
        remote_code_dir=ctx.remote_code_dir,
        tmux_session=ctx.session_name,
        repo_root=str(ctx.repo_root),
        name=ctx.name,
        machine_config=machine_cfg_dict,
        credentials=cfg.credentials,
        extra={"dph_total": machine.dph_total} if machine.dph_total else None,
    )

    # Summary
    log.final_banner()
    if ctx.session_name:
        ssh_disp = ctx.ssh_client.ssh_display
        host = ctx.ssh_client.info.remote_host
        log.info(f"  Attach:   ssh {ssh_disp} {host} -t 'tmux attach -t {ctx.session_name}'")
        log.info(f"  Logs:     ssh {ssh_disp} {host} 'tail -f {ctx.remote_logs_dir}/{ctx.session_name}.log'")
        log.info(f"  Sessions: ssh {ssh_disp} {host} 'tmux ls'")
    if ctx.pod_id and ctx.provider == "runpod":
        log.info(f"  Stop pod:     beam stop {ctx.session_name}")
    if ctx.pod_id and ctx.provider in ("vast.ai", "vastai"):
        log.info(f"  Stop instance: beam stop {ctx.session_name}")
    log.info("")


def _snapshot_machine_config(ctx: RunContext) -> dict | None:
    """Serialize the machine config that was used, for pulse restart."""
    mc = _resolve_machine_config(ctx.cfg, ctx.provider, ctx.pod_config_name)
    return mc.model_dump() if mc else None


def _get_recovery_policy(ctx: RunContext) -> str:
    """Read recovery_policy from the relevant machine config."""
    mc = _resolve_machine_config(ctx.cfg, ctx.provider, ctx.pod_config_name)
    return mc.recovery_policy if mc else "restart"
