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

        # Derived remote paths
        repo_name = cfg.project.name or repo_root.name
        self.remote_code_dir = f"{cfg.remote.project_dir}/{repo_name}"
        self.remote_venv_dir = f"{self.remote_code_dir}/.venv"
        self.remote_logs_dir = f"{cfg.remote.project_dir}/logs/{repo_name}"

        # Logs directory (local)
        self.logs_dir = repo_root / ".beam" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Registry image
        github_user = os.environ.get("GITHUB_USERNAME", "")
        if github_user:
            self.ghcr_image = (
                f"ghcr.io/{github_user}/"
                f"{cfg.registry.ghcr_image_name}:{cfg.registry.ghcr_tag}"
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
        ctx.build_result = build_venv_mod.build_venv(cfg, ctx.repo_root)

        if ctx.build_result.deps_changed or os.environ.get("FORCE_PUSH") == "1":
            log.banner(2, total_steps, "Push image to ghcr.io")
            push_image_mod.push_image(cfg, ctx.ghcr_image)
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
        ctx.build_result = build_venv_mod.build_venv(cfg, ctx.repo_root)

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
    )
    if result:
        ctx.session_name = result.session_name

    # Save session state
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


def _get_recovery_policy(ctx: RunContext) -> str:
    """Read recovery_policy from the relevant machine config."""
    if ctx.provider == "runpod":
        for mc in ctx.cfg.providers.runpod.configs:
            if mc.name == ctx.pod_config_name:
                return mc.recovery_policy
        if ctx.cfg.providers.runpod.configs:
            return ctx.cfg.providers.runpod.configs[0].recovery_policy
    elif ctx.provider in ("vast.ai", "vastai"):
        for mc in ctx.cfg.providers.vastai.configs:
            if mc.name == ctx.pod_config_name:
                return mc.recovery_policy
        if ctx.cfg.providers.vastai.configs:
            return ctx.cfg.providers.vastai.configs[0].recovery_policy
    return "restart"
