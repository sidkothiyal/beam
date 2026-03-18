"""Beam CLI — remote GPU training deployment toolkit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from beam import log
from beam.config import BeamConfig, load_config, get_project_root

app = typer.Typer(
    name="beam",
    help="Remote GPU training deployment toolkit.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

pulse_app = typer.Typer(help="Pulse monitoring and cron management.")
app.add_typer(pulse_app, name="pulse")


# ═══════════════════════════════════════════════════════════════════════════
# beam run
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def run(
    ip: Optional[str] = typer.Option(None, "--ip", help="Remote server IP/hostname"),
    port: Optional[str] = typer.Option(None, "-p", "--port", help="SSH port"),
    identity_file: Optional[str] = typer.Option(None, "-i", help="SSH identity file"),
    user: Optional[str] = typer.Option(None, "--user", help="SSH user"),
    provider: Optional[str] = typer.Option(None, "--provider", help="Cloud provider: runpod, vast.ai"),
    pod_id: Optional[str] = typer.Option(None, "--pod-id", help="Existing pod/instance ID"),
    pod_config: Optional[str] = typer.Option(None, "--pod-config", help="Named machine config from beam.yaml"),
    remote_project_dir: Optional[str] = typer.Option(None, "--remote-project-dir", help="Remote workspace root"),
    remote_home: Optional[str] = typer.Option(None, "--remote-home", help="Remote home directory"),
    pulse_interval: Optional[str] = typer.Option(None, "--pulse", help="Pulse cron interval (e.g. 5m, 1h)"),
    pulse_idle_stop: bool = typer.Option(False, "--pulse-idle-stop", help="Stop when GPU idle"),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to beam.yaml"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Quiet output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done"),
    command: Optional[list[str]] = typer.Argument(None, help="Command to run (after --)"),
) -> None:
    """Deploy and run a command locally or on a remote GPU server."""
    log.set_verbosity(verbose=verbose, quiet=quiet)

    # Build config overrides from CLI flags
    overrides: dict = {}
    if user:
        overrides["remote.user"] = user
    if remote_project_dir:
        overrides["remote.project_dir"] = remote_project_dir
    if remote_home:
        overrides["remote.home"] = remote_home
    if identity_file:
        overrides["ssh.identity_file"] = identity_file

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path, overrides=overrides)
    repo_root = get_project_root(cfg_path)

    cmd_str = " ".join(command) if command else ""

    from beam.pipeline import RunContext, run_local, run_remote as pipeline_remote

    ctx = RunContext(
        cfg,
        repo_root,
        provider=provider or "",
        pod_id=pod_id or "",
        pod_config_name=pod_config or "",
        ip=ip or "",
        port=port or "",
        command=cmd_str,
        pulse_interval=pulse_interval or "",
        pulse_idle_stop=pulse_idle_stop,
        dry_run=dry_run,
    )

    if not ip and not provider:
        run_local(ctx)
    else:
        pipeline_remote(ctx)

        # Install pulse cron if requested
        if pulse_interval and cmd_str:
            from beam import pulse
            interval_secs = pulse.parse_duration(pulse_interval)
            sess_file = ctx.logs_dir / "sessions" / f"{ctx.session_name}.json"
            pulse.install_cron(
                ctx.session_name,
                sess_file,
                interval_secs,
                auto_restart=True,
                idle_stop=pulse_idle_stop,
                max_retries=cfg.pulse.max_retries,
                logs_dir=ctx.logs_dir,
            )


# ═══════════════════════════════════════════════════════════════════════════
# beam status
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def status(
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to beam.yaml"),
) -> None:
    """Show all active sessions."""
    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    from beam import pulse as pulse_mod
    sessions = pulse_mod.list_cron_sessions(logs_dir)

    if not sessions:
        log.info("No active sessions.")
        return

    table = Table(title="Beam Sessions")
    table.add_column("Session", style="cyan")
    table.add_column("Provider")
    table.add_column("Pod/Instance")
    table.add_column("Host")
    table.add_column("Cron", style="green")

    for s in sessions:
        host = s.get("ip", "")
        p = s.get("port", "")
        if p and p != "22":
            host = f"{host}:{p}"
        table.add_row(
            s.get("session_id", "?"),
            s.get("provider", "ssh"),
            s.get("pod_id", "-"),
            host or "-",
            "active" if s.get("cron_active") else "inactive",
        )

    log.console.print(table)


# ═══════════════════════════════════════════════════════════════════════════
# beam init
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def init() -> None:
    """Generate a starter beam.yaml in the current directory."""
    target = Path.cwd() / "beam.yaml"
    if target.exists():
        log.warn(f"{target} already exists. Not overwriting.")
        raise typer.Exit(1)

    project_name = Path.cwd().name
    content = f"""\
# Beam configuration — {project_name}
# Docs: https://github.com/your-org/beam

project:
  name: {project_name}
  docker_image: {project_name}
  dep_files: [pyproject.toml, uv.lock]

registry:
  ghcr_image_name: {project_name}-env
  ghcr_tag: latest

remote:
  project_dir: /workspace
  home: /root
  user: root

ssh:
  connect_timeout: 20
  retry_attempts: 5
  retry_interval: 10

credentials:
  kaggle: ~/.kaggle/kaggle.json
  env_vars:
    - WANDB_API_KEY
    - HUGGING_FACE_HUB_TOKEN

providers:
  runpod:
    configs:
      - name: default
        gpu_type: "NVIDIA A40"
        gpu_count: 1
        cloud_tier: secure
        spot: true
        recovery_policy: new
        image: "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
        container_disk_size: 30
        volume_size: 50
        volume_path: /workspace
        ports: ["22/tcp"]
        ssh_public_key_files: [~/.ssh/id_ed25519.pub]
    fallback_order: [default]

  vastai:
    configs:
      - name: default
        gpu_name: "A100 PCIE"
        num_gpus: 1
        offer_type: bid
        price: 0.16
        price_max: 0.20
        price_step: 0.005
        disk_gb: 50
        recovery_policy: new
    fallback_order: [default]

pulse:
  gpu_idle_threshold: 5
  gpu_idle_max_strikes: 3
  max_retries: 5
"""
    target.write_text(content)
    log.done(f"Created {target}")
    log.info("Edit it to match your project, then run: beam run --provider <name> -- <command>")


# ═══════════════════════════════════════════════════════════════════════════
# beam attach
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def attach(
    session_id: str = typer.Argument(..., help="Session ID to attach to"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """SSH into the remote and attach to the tmux session."""
    sess = _load_session_or_exit(session_id, config_file)
    ip = sess.get("ip", "")
    port = sess.get("port", "22")
    user = sess.get("user", "root")
    identity = sess.get("identity_file", "")

    cmd = ["ssh"]
    if port and port != "22":
        cmd += ["-p", port]
    if identity:
        cmd += ["-i", identity]
    cmd += [f"{user}@{ip}", "-t", f"tmux attach -t {session_id}"]
    os.execvp("ssh", cmd)


# ═══════════════════════════════════════════════════════════════════════════
# beam logs
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def logs(
    session_id: str = typer.Argument(..., help="Session ID"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Tail remote logs for a session."""
    sess = _load_session_or_exit(session_id, config_file)
    ip = sess.get("ip", "")
    port = sess.get("port", "22")
    user = sess.get("user", "root")
    identity = sess.get("identity_file", "")
    remote_project_dir = sess.get("remote_project_dir", "/workspace")

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path)
    repo_name = cfg.project.name or Path.cwd().name
    remote_logs_dir = f"{remote_project_dir}/logs/{repo_name}"

    cmd = ["ssh"]
    if port and port != "22":
        cmd += ["-p", port]
    if identity:
        cmd += ["-i", identity]
    cmd += [f"{user}@{ip}", f"tail -f {remote_logs_dir}/{session_id}.log"]
    os.execvp("ssh", cmd)


# ═══════════════════════════════════════════════════════════════════════════
# beam stop
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def stop(
    session_id: str = typer.Argument(..., help="Session ID to stop"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Stop the instance and remove pulse cron for a session."""
    from beam import pulse as pulse_mod

    sess = _load_session_or_exit(session_id, config_file)
    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    pulse_mod.do_idle_stop(sess, session_id, logs_dir)
    log.done(f"Session {session_id} stopped.")


# ═══════════════════════════════════════════════════════════════════════════
# beam pulse list / install / uninstall / check
# ═══════════════════════════════════════════════════════════════════════════

@pulse_app.command("list")
def pulse_list(
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """List all tracked sessions and their cron status."""
    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    from beam import pulse as pulse_mod
    sessions = pulse_mod.list_cron_sessions(logs_dir)
    if not sessions:
        log.info("No sessions found.")
        return

    table = Table(title="Pulse Sessions")
    table.add_column("Session", style="cyan")
    table.add_column("Provider")
    table.add_column("Pod/Instance")
    table.add_column("Host")
    table.add_column("Cron", style="green")

    for s in sessions:
        host = s.get("ip", "")
        p = s.get("port", "")
        if p and p != "22":
            host = f"{host}:{p}"
        table.add_row(
            s.get("session_id", "?"),
            s.get("provider", "ssh"),
            s.get("pod_id", "-"),
            host or "-",
            "active" if s.get("cron_active") else "inactive",
        )

    log.console.print(table)


@pulse_app.command("install")
def pulse_install(
    session_id: str = typer.Option(..., "--session", help="Session ID"),
    interval: str = typer.Option("5m", "--interval", help="Check interval (e.g. 5m, 1h)"),
    auto_restart: bool = typer.Option(True, "--auto-restart/--no-auto-restart"),
    idle_stop: bool = typer.Option(False, "--idle-stop"),
    max_retries: int = typer.Option(5, "--max-retries"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Install a pulse cron job for a session."""
    from beam import pulse as pulse_mod

    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    sess_file = logs_dir / "sessions" / f"{session_id}.json"
    if not sess_file.exists():
        log.error(f"Session file not found: {sess_file}")
        raise typer.Exit(1)

    interval_secs = pulse_mod.parse_duration(interval)
    pulse_mod.install_cron(
        session_id,
        sess_file,
        interval_secs,
        auto_restart=auto_restart,
        idle_stop=idle_stop,
        max_retries=max_retries,
        logs_dir=logs_dir,
    )


@pulse_app.command("uninstall")
def pulse_uninstall(
    session_id: Optional[str] = typer.Option(None, "--session", help="Session ID (omit for all)"),
    all_sessions: bool = typer.Option(False, "--all", help="Remove all pulse cron jobs"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Remove pulse cron job(s)."""
    from beam import pulse as pulse_mod

    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    if all_sessions:
        pulse_mod.uninstall_cron("", logs_dir=logs_dir)
    elif session_id:
        pulse_mod.uninstall_cron(session_id, logs_dir=logs_dir)
    else:
        log.error("Specify --session <id> or --all")
        raise typer.Exit(1)


@pulse_app.command("check")
def pulse_check(
    session_file: Optional[str] = typer.Option(None, "--session-file", help="Session JSON file"),
    session_id: Optional[str] = typer.Option(None, "--session", help="Session ID"),
    auto_restart: bool = typer.Option(False, "--auto-restart"),
    idle_stop: bool = typer.Option(False, "--idle-stop"),
    max_retries: int = typer.Option(5, "--max-retries"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Run a single health check for a session."""
    from beam import pulse as pulse_mod

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path)
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    # Resolve session_id from file or argument
    sid = session_id or ""
    if session_file:
        p = Path(session_file)
        if p.exists():
            import json
            data = json.loads(p.read_text())
            sid = data.get("session_id", p.stem)

    if not sid:
        log.error("Specify --session <id> or --session-file <path>")
        raise typer.Exit(1)

    pulse_mod.run_check(
        sid,
        logs_dir,
        auto_restart=auto_restart,
        idle_stop=idle_stop,
        max_retries=max_retries,
        pulse_cfg=cfg.pulse,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _load_session_or_exit(session_id: str, config_file: str | None) -> dict:
    from beam import session as sess_mod
    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"
    try:
        return sess_mod.load_session(logs_dir, session_id)
    except FileNotFoundError:
        log.error(f"Session not found: {session_id}")
        log.info("Run 'beam status' to see active sessions.")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
