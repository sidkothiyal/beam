"""Beam CLI — remote GPU training deployment toolkit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from beam import log, complete
from beam.config import (
    BeamConfig,
    RunPodMachineConfig,
    VastAIMachineConfig,
    load_config,
    get_project_root,
)

app = typer.Typer(
    name="beam",
    help="Remote GPU training deployment toolkit.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

pulse_app = typer.Typer(help="Pulse monitoring and cron management.")
app.add_typer(pulse_app, name="pulse")

pulse_agent_app = typer.Typer(help="Pulse agent-mode (LLM diagnosis) controls.")
pulse_app.add_typer(pulse_agent_app, name="agent")


# ═══════════════════════════════════════════════════════════════════════════
# beam run
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def run(
    ip: Optional[str] = typer.Option(None, "--ip", help="Remote server IP/hostname"),
    port: Optional[str] = typer.Option(None, "-p", "--port", help="SSH port"),
    identity_file: Optional[str] = typer.Option(None, "-i", help="SSH identity file"),
    user: Optional[str] = typer.Option(None, "--user", help="SSH user"),
    provider: Optional[str] = typer.Option(
        None, "--provider", help="Cloud provider: runpod, vast.ai",
        autocompletion=complete.complete_provider,
    ),
    pod_id: Optional[str] = typer.Option(None, "--pod-id", help="Existing pod/instance ID"),
    pod_config: Optional[str] = typer.Option(
        None, "--pod-config", help="Named machine config from beam.yaml",
        autocompletion=complete.complete_pod_config,
    ),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Custom session name for easier tracking"),
    remote_project_dir: Optional[str] = typer.Option(None, "--remote-project-dir", help="Remote workspace root"),
    remote_home: Optional[str] = typer.Option(None, "--remote-home", help="Remote home directory"),
    pulse_interval: Optional[str] = typer.Option(None, "--pulse", help="Pulse cron interval (e.g. 5m, 1h). Defaults to pulse.interval in beam.yaml"),
    pulse_idle_stop: Optional[bool] = typer.Option(None, "--pulse-idle-stop/--no-pulse-idle-stop", help="Stop when GPU idle. Defaults to pulse.idle_stop in beam.yaml"),
    no_auto_stop_on_complete: bool = typer.Option(False, "--no-auto-stop-on-complete", help="Don't auto-stop when job finishes"),
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

    # Resolve pulse settings: explicit CLI flags win, else fall back to config.
    eff_pulse_interval = pulse_interval if pulse_interval is not None else cfg.pulse.interval
    eff_pulse_idle_stop = pulse_idle_stop if pulse_idle_stop is not None else cfg.pulse.idle_stop

    cmd_str = " ".join(command) if command else ""

    from beam.pipeline import RunContext, run_local, run_remote as pipeline_remote

    ctx = RunContext(
        cfg,
        repo_root,
        provider=provider or "",
        pod_id=pod_id or "",
        pod_config_name=pod_config or "",
        name=name or "",
        ip=ip or "",
        port=port or "",
        command=cmd_str,
        pulse_interval=eff_pulse_interval or "",
        pulse_idle_stop=eff_pulse_idle_stop,
        dry_run=dry_run,
    )

    if not ip and not provider:
        run_local(ctx)
    else:
        # Mirror all run output to a per-job log file. The session id is minted
        # late (inside run_remote), so start on a temp path and rename once known
        # — yielding one unified timeline that pulse will keep appending to.
        sessions_dir = ctx.logs_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        tmp_log = sessions_dir / f".run-{os.getpid()}.log"
        log.attach_job_log(tmp_log)
        try:
            pipeline_remote(ctx)
        finally:
            log.detach_job_log()
            if ctx.session_name:
                final_log = sessions_dir / f"{ctx.session_name}.log"
                if final_log.exists():
                    with open(final_log, "a", encoding="utf-8") as dst:
                        dst.write(tmp_log.read_text(encoding="utf-8"))
                    tmp_log.unlink(missing_ok=True)
                else:
                    tmp_log.rename(final_log)
            else:
                tmp_log.unlink(missing_ok=True)

        # Install pulse cron if requested
        if eff_pulse_interval and cmd_str:
            from beam import pulse
            interval_secs = pulse.parse_duration(eff_pulse_interval)
            sess_file = ctx.logs_dir / "sessions" / f"{ctx.session_name}.json"
            pulse.install_cron(
                ctx.session_name,
                sess_file,
                interval_secs,
                auto_restart=True,
                idle_stop=eff_pulse_idle_stop,
                auto_stop_on_complete=not no_auto_stop_on_complete,
                max_retries=cfg.pulse.max_retries,
                logs_dir=ctx.logs_dir,
            )


# ═══════════════════════════════════════════════════════════════════════════
# beam status
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def status(
    live: bool = typer.Option(True, "--live/--offline", help="SSH into remotes to check job state (use --offline to skip)"),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to beam.yaml"),
) -> None:
    """Show all active sessions."""
    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path) if live else None
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
    table.add_column("Job State")
    table.add_column("Est. Cost")
    table.add_column("Cron", style="green")

    for s in sessions:
        host = s.get("ip", "")
        p = s.get("port", "")
        if p and p != "22":
            host = f"{host}:{p}"

        job_state = "?"
        if live:
            try:
                info = pulse_mod.check_job_state(s, cfg)
                job_state = info.get("state", "?")
            except Exception:
                job_state = "error"

        cost = pulse_mod.compute_session_cost(logs_dir, s.get("session_id", ""), s)

        table.add_row(
            s.get("session_id", "?"),
            s.get("provider", "ssh"),
            s.get("pod_id", "-"),
            host or "-",
            job_state,
            cost,
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
        num_gpus_max: 1
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
    _hint_completion()


# ═══════════════════════════════════════════════════════════════════════════
# beam offers
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def offers(
    provider: str = typer.Option(
        ..., "--provider", help="Cloud provider: runpod, vast.ai",
        autocompletion=complete.complete_provider,
    ),
    pod_config: Optional[str] = typer.Option(
        None, "--pod-config", help="Named machine config from beam.yaml",
        autocompletion=complete.complete_pod_config,
    ),
    config_file: Optional[str] = typer.Option(None, "--config", help="Path to beam.yaml"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Quiet output"),
) -> None:
    """List compatible GPU instances available for the given config."""
    log.set_verbosity(verbose=verbose, quiet=quiet)

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path)

    if provider in ("vast.ai", "vastai"):
        _list_vastai_offers(cfg, pod_config)
    elif provider == "runpod":
        _list_runpod_offers(cfg, pod_config)
    else:
        log.error(f"Unknown provider: {provider} (supported: runpod, vast.ai)")
        raise typer.Exit(1)


def _resolve_vastai_machine_config(
    cfg: BeamConfig, pod_config_name: str | None
) -> VastAIMachineConfig:
    prov_cfg = cfg.providers.vastai
    if pod_config_name:
        for mc in prov_cfg.configs:
            if mc.name == pod_config_name:
                return mc
        log.error(f"Vast.ai config '{pod_config_name}' not found in beam.yaml")
        raise typer.Exit(1)
    if prov_cfg.fallback_order:
        name = prov_cfg.fallback_order[0]
        for mc in prov_cfg.configs:
            if mc.name == name:
                return mc
    if prov_cfg.configs:
        return prov_cfg.configs[0]
    log.error("No Vast.ai machine configs found in beam.yaml")
    raise typer.Exit(1)


def _resolve_runpod_machine_config(
    cfg: BeamConfig, pod_config_name: str | None
) -> RunPodMachineConfig:
    prov_cfg = cfg.providers.runpod
    if pod_config_name:
        for mc in prov_cfg.configs:
            if mc.name == pod_config_name:
                return mc
        log.error(f"RunPod config '{pod_config_name}' not found in beam.yaml")
        raise typer.Exit(1)
    if prov_cfg.fallback_order:
        name = prov_cfg.fallback_order[0]
        for mc in prov_cfg.configs:
            if mc.name == name:
                return mc
    if prov_cfg.configs:
        return prov_cfg.configs[0]
    log.error("No RunPod machine configs found in beam.yaml")
    raise typer.Exit(1)


def _list_vastai_offers(cfg: BeamConfig, pod_config_name: str | None) -> None:
    from beam.providers.vastai import VastAIProvider

    mc = _resolve_vastai_machine_config(cfg, pod_config_name)

    reqs = []
    if mc.gpu_name:
        reqs.append(f"gpu={mc.gpu_name}")
    if mc.gpu_ram_min is not None:
        reqs.append(f"vram>={mc.gpu_ram_min}GB")
    if mc.num_gpus_max is not None:
        reqs.append(f"gpus={mc.num_gpus}..{mc.num_gpus_max}")
    else:
        reqs.append(f"gpus>={mc.num_gpus}")
    if mc.dlperf_min is not None:
        reqs.append(f"dlperf>={mc.dlperf_min}")
    if mc.price_max is not None:
        reqs.append(f"max=${mc.price_max:.4f}/hr")

    log.step(
        f"Searching Vast.ai offers for config '{mc.name}' "
        f"({', '.join(reqs)}) [pick={mc.pick_strategy}, bid={mc.bid_strategy}] ..."
    )

    provider = VastAIProvider()
    offers_list = provider.list_compatible_offers(mc)

    if not offers_list:
        log.warn("No compatible offers found.")
        return

    is_bid = mc.offer_type == "bid"

    table = Table(title=f"Vast.ai Compatible Offers — config '{mc.name}'")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Offer ID", style="cyan")
    table.add_column("GPU")
    table.add_column("Count", justify="right")
    table.add_column("VRAM", justify="right")
    if is_bid:
        table.add_column("Min Bid", justify="right", style="yellow")
        table.add_column("Your Bid", justify="right", style="green")
    table.add_column("Total $/hr", justify="right", style="yellow")
    table.add_column("Base $/hr", justify="right", style="dim")
    table.add_column("Adj $/hr", justify="right")
    table.add_column("Search $/hr", justify="right")
    table.add_column("DLPerf", justify="right")
    table.add_column("Reliability", justify="right")
    table.add_column("Machine", justify="right", style="dim")

    for i, o in enumerate(offers_list, 1):
        gpu_name = o.get("gpu_name", "?")
        num_gpus = str(o.get("num_gpus", "?"))
        gpu_ram_mb = o.get("gpu_ram", 0)
        vram = f"{gpu_ram_mb / 1024:.0f}GB" if gpu_ram_mb else "?"
        dlperf = o.get("dlperf", 0)
        dlperf_str = f"{dlperf:.1f}" if dlperf else "-"
        reliability = o.get("reliability", 0)
        rel_str = f"{reliability:.2f}" if reliability else "-"
        machine_id = str(o.get("machine_id", "-"))
        offer_id = str(o.get("id", "?"))

        dph_base = o.get("dph_base", 0.0)
        dph_base_str = f"${dph_base:.4f}" if dph_base else "-"
        dph_adj = o.get("dph_total_adj", 0.0)
        dph_adj_str = f"${dph_adj:.4f}" if dph_adj else "-"
        search_total = (o.get("search") or {}).get("totalHour", 0.0)
        search_str = f"${search_total:.4f}" if search_total else "-"

        dph = o.get("dph_total", 0.0)
        dph_str = f"${dph:.4f}" if dph else "-"

        if is_bid:
            min_bid = o.get("min_bid", 0.0)
            computed_bid = o.get("_computed_bid", 0.0)
            table.add_row(
                str(i), offer_id, gpu_name, num_gpus, vram,
                f"${min_bid:.4f}", f"${computed_bid:.4f}",
                dph_str, dph_base_str, dph_adj_str, search_str,
                dlperf_str, rel_str, machine_id,
            )
        else:
            table.add_row(
                str(i), offer_id, gpu_name, num_gpus, vram,
                dph_str, dph_base_str, dph_adj_str, search_str,
                dlperf_str, rel_str, machine_id,
            )

    log.console.print(table)
    log.done(f"{len(offers_list)} compatible offer(s) found.")


def _list_runpod_offers(cfg: BeamConfig, pod_config_name: str | None) -> None:
    from beam.providers.runpod import RunPodProvider

    mc = _resolve_runpod_machine_config(cfg, pod_config_name)

    reqs = []
    if mc.gpu_type:
        reqs.append(f"gpu={mc.gpu_type}")
    if mc.max_cost is not None:
        reqs.append(f"max=${mc.max_cost:.2f}/hr")
    reqs.append(f"tier={mc.cloud_tier}")
    if mc.spot:
        reqs.append("spot=yes")

    log.step(
        f"Listing RunPod GPU types for config '{mc.name}' "
        f"({', '.join(reqs)}) ..."
    )

    provider = RunPodProvider()
    gpu_types = provider.list_gpu_types(mc)

    if not gpu_types:
        log.warn("No compatible GPU types found.")
        return

    table = Table(title=f"RunPod Compatible GPU Types — config '{mc.name}'")
    table.add_column("#", style="dim", justify="right")
    table.add_column("GPU Type", style="cyan")
    table.add_column("VRAM", justify="right")
    table.add_column("Community $/hr", justify="right")
    table.add_column("Secure $/hr", justify="right")
    table.add_column("Spot $/hr", justify="right", style="green")

    for i, t in enumerate(gpu_types, 1):
        name = t.get("displayName") or t.get("id", "?")
        mem = t.get("memoryInGb")
        mem_str = f"{mem}GB" if mem is not None else "?"

        def _fmt_price(key: str) -> str:
            val = t.get(key)
            return f"${float(val):.4f}" if val is not None else "-"

        table.add_row(
            str(i),
            name,
            mem_str,
            _fmt_price("communityPrice"),
            _fmt_price("securePrice"),
            _fmt_price("communitySpotPrice"),
        )

    log.console.print(table)
    log.done(f"{len(gpu_types)} compatible GPU type(s) found.")


# ═══════════════════════════════════════════════════════════════════════════
# beam attach
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def attach(
    session_id: str = typer.Argument(
        ..., help="Session ID to attach to",
        autocompletion=complete.complete_session_id,
    ),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """SSH into the remote and attach to the tmux session."""
    sess = _load_session_or_exit(session_id, config_file)
    ip = sess.get("ip", "")
    user = sess.get("user", "root")
    tmux_session = sess.get("tmux_session", session_id)

    cmd = _ssh_cmd_prefix(sess)
    cmd += [f"{user}@{ip}", "-t", f"tmux attach -t {tmux_session}"]
    os.execvp("ssh", cmd)


# ═══════════════════════════════════════════════════════════════════════════
# beam logs
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def logs(
    session_id: str = typer.Argument(
        ..., help="Session ID",
        autocompletion=complete.complete_session_id,
    ),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f", help="Follow log output"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Tail remote logs for a session."""
    sess = _load_session_or_exit(session_id, config_file)
    ip = sess.get("ip", "")
    user = sess.get("user", "root")
    tmux_session = sess.get("tmux_session", session_id)
    remote_logs_dir = sess.get("remote_logs_dir", "")

    if not remote_logs_dir:
        remote_project_dir = sess.get("remote_project_dir", "/workspace")
        cfg_path = Path(config_file) if config_file else None
        cfg = load_config(config_path=cfg_path)
        repo_name = cfg.project.name or Path.cwd().name
        remote_logs_dir = f"{remote_project_dir}/logs/{repo_name}"

    tail_flag = "-f" if follow else "-n 100"
    cmd = _ssh_cmd_prefix(sess)
    cmd += [f"{user}@{ip}", f"tail {tail_flag} {remote_logs_dir}/{tmux_session}.log"]
    os.execvp("ssh", cmd)


# ═══════════════════════════════════════════════════════════════════════════
# beam ssh
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def ssh(
    session_id: str = typer.Argument(
        ..., help="Session ID to connect to",
        autocompletion=complete.complete_session_id,
    ),
    command: Optional[list[str]] = typer.Argument(
        None, help="Optional command to run on the remote (after --); omit for an interactive shell",
    ),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Open an interactive SSH shell on a session's remote (or run a command).

    Examples:
        beam ssh mysession                    # interactive shell
        beam ssh mysession -- nvidia-smi      # run a one-off command
    """
    sess = _load_session_or_exit(session_id, config_file)
    ip = sess.get("ip", "")
    user = sess.get("user", "root")
    if not ip:
        log.error(f"Session {session_id} has no remote IP.")
        raise typer.Exit(1)

    cmd = _ssh_cmd_prefix(sess)
    cmd += [f"{user}@{ip}"]
    if command:
        # -t forces a pseudo-tty so interactive remote programs behave correctly.
        cmd += ["-t", " ".join(command)]
    os.execvp("ssh", cmd)


# ═══════════════════════════════════════════════════════════════════════════
# beam scp
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def scp(
    session_id: str = typer.Argument(
        ..., help="Session ID whose remote to copy to/from",
        autocompletion=complete.complete_session_id,
    ),
    source: str = typer.Argument(
        ..., help="Source path. Prefix with 'remote:' for a path on the instance.",
    ),
    dest: str = typer.Argument(
        ..., help="Destination path. Prefix with 'remote:' for a path on the instance.",
    ),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r", help="Copy directories recursively"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Copy files to/from a session's remote.

    Prefix either path with ``remote:`` to refer to the instance. Examples:
        beam scp mysession ./data remote:/workspace/data     # upload
        beam scp mysession remote:/workspace/out ./out       # download
    """
    sess = _load_session_or_exit(session_id, config_file)
    ip = sess.get("ip", "")
    user = sess.get("user", "root")
    if not ip:
        log.error(f"Session {session_id} has no remote IP.")
        raise typer.Exit(1)

    def _translate(path: str) -> str:
        if path.startswith("remote:"):
            return f"{user}@{ip}:{path[len('remote:'):]}"
        return path

    if not source.startswith("remote:") and not dest.startswith("remote:"):
        log.error("At least one of source/dest must be a 'remote:' path.")
        raise typer.Exit(1)

    cmd = _ssh_cmd_prefix(sess, port_flag="-P")
    if recursive:
        cmd.append("-r")
    cmd += [_translate(source), _translate(dest)]
    os.execvp("scp", cmd)


# ═══════════════════════════════════════════════════════════════════════════
# beam stop
# ═══════════════════════════════════════════════════════════════════════════

@app.command()
def stop(
    session_id: str = typer.Argument(
        ..., help="Session ID to stop",
        autocompletion=complete.complete_session_id,
    ),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Destroy the instance and remove pulse cron for a session.

    This permanently tears the instance down (both RunPod and Vast.ai) so no
    further compute or storage cost accrues — it does not merely leave the
    instance in a stopped/inactive state.
    """
    from beam import pulse as pulse_mod

    sess = _load_session_or_exit(session_id, config_file)
    cfg_path = Path(config_file) if config_file else None
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"

    pulse_mod.do_destroy_stop(sess, session_id, logs_dir)
    log.done(f"Session {session_id} destroyed.")


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
    session_id: str = typer.Option(
        ..., "--session", help="Session ID",
        autocompletion=complete.complete_session_id,
    ),
    interval: str = typer.Option("5m", "--interval", help="Check interval (e.g. 5m, 1h)"),
    auto_restart: bool = typer.Option(True, "--auto-restart/--no-auto-restart"),
    idle_stop: bool = typer.Option(False, "--idle-stop"),
    auto_stop_on_complete: bool = typer.Option(True, "--auto-stop-on-complete/--no-auto-stop-on-complete"),
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
        auto_stop_on_complete=auto_stop_on_complete,
        max_retries=max_retries,
        logs_dir=logs_dir,
    )


@pulse_app.command("uninstall")
def pulse_uninstall(
    session_id: Optional[str] = typer.Option(
        None, "--session", help="Session ID (omit for all)",
        autocompletion=complete.complete_session_id,
    ),
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
    session_id: Optional[str] = typer.Option(
        None, "--session", help="Session ID",
        autocompletion=complete.complete_session_id,
    ),
    auto_restart: bool = typer.Option(False, "--auto-restart"),
    idle_stop: bool = typer.Option(False, "--idle-stop"),
    auto_stop_on_complete: bool = typer.Option(False, "--auto-stop-on-complete"),
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
        p = Path(session_file).resolve()
        if p.exists():
            import json
            data = json.loads(p.read_text())
            sid = data.get("session_id", p.stem)
            # Derive logs_dir from session file path (<logs_dir>/sessions/<id>.json)
            if p.parent.name == "sessions":
                logs_dir = p.parent.parent

    if not sid:
        log.error("Specify --session <id> or --session-file <path>")
        raise typer.Exit(1)

    pulse_mod.run_check(
        sid,
        logs_dir,
        auto_restart=auto_restart,
        idle_stop=idle_stop,
        auto_stop_on_complete=auto_stop_on_complete,
        max_retries=max_retries,
        pulse_cfg=cfg.pulse,
    )


# ═══════════════════════════════════════════════════════════════════════════
# beam pulse agent — set-key / test / diagnose
# ═══════════════════════════════════════════════════════════════════════════

@pulse_agent_app.command("set-key")
def pulse_agent_set_key(
    provider: str = typer.Option("nim", "--provider", help="LLM provider: nim, openrouter, openai"),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Store the agent LLM API key (OS keyring if available, else ~/.beam/agent.env)."""
    from beam.agent import secrets

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path)
    # Honor a custom api_key_env if the configured provider matches; else default.
    agent_llm = cfg.pulse.agent.llm
    env_name = (
        agent_llm.api_key_env
        if agent_llm.provider == provider and agent_llm.api_key_env
        else secrets.provider_env_name(provider)
    )
    value = typer.prompt(f"{provider} API key ({env_name})", hide_input=True)
    backend = secrets.set_secret(env_name, value)
    log.done(f"Stored {provider} key ({env_name}) via {backend}.")


@pulse_agent_app.command("test")
def pulse_agent_test(
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Make one live LLM call (and one tool round-trip) to verify the agent backend."""
    import time as _time

    from beam.agent.llm import AgentLLM, LLMUnavailable

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path)
    agent_cfg = cfg.pulse.agent

    llm = AgentLLM(agent_cfg.llm)
    log.kv("provider", llm.provider)
    log.kv("model", llm.model_name)
    log.kv("base_url", llm.base_url or "(default)")
    log.kv("api_key_env", agent_cfg.llm.api_key_env)
    log.kv("key_found", "yes" if llm.available() else "NO")
    if not llm.available():
        log.error("No API key resolved. Run: beam pulse agent set-key --provider " + llm.provider)
        raise typer.Exit(1)

    try:
        t0 = _time.time()
        reply = llm.complete("You are a health check.", "Reply with the single word OK.")
        log.done(f"LLM round-trip OK in {_time.time() - t0:.1f}s -- reply: {reply.strip()[:60]}")
    except LLMUnavailable as exc:
        log.error(f"LLM unavailable: {exc}")
        raise typer.Exit(1)
    except Exception as exc:  # noqa: BLE001
        log.error(f"LLM call failed: {exc}")
        raise typer.Exit(1)


@pulse_agent_app.command("diagnose")
def pulse_agent_diagnose(
    session_id: str = typer.Option(
        ..., "--session", help="Session ID",
        autocompletion=complete.complete_session_id,
    ),
    config_file: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Run a one-shot agent diagnosis for a session and print the verdict (no action taken)."""
    from beam import pulse as pulse_mod
    from beam.agent.diagnose import diagnose_session

    cfg_path = Path(config_file) if config_file else None
    cfg = load_config(config_path=cfg_path)
    repo_root = get_project_root(cfg_path)
    logs_dir = repo_root / ".beam" / "logs"
    sess = _load_session_or_exit(session_id, config_file)

    # Gather the same observations run_check would, then diagnose (advisory).
    instance_status = pulse_mod.check_provider_status(sess)
    if instance_status in ("unknown", "api_error") and sess.get("ip"):
        if pulse_mod.check_ssh_reachable(sess, cfg):
            instance_status = "reachable"
    job_info = {"state": "unknown"}
    if instance_status in ("running", "reachable") and sess.get("ip"):
        job_info = pulse_mod.check_job_state(sess, cfg)
    job_state = job_info.get("state", "unknown")

    pulse_mod._restore_env(sess)
    observations = pulse_mod._agent_observations(
        sess, instance_status, job_state, job_info,
        consecutive_failures=0, idle_strikes=0, max_retries=cfg.pulse.max_retries,
    )
    log.info(f"instance={instance_status} job={job_state}")
    verdict = diagnose_session(sess, observations, logs_dir, cfg)

    if not verdict.used_agent:
        log.error(f"Agent unavailable: {verdict.error}")
        raise typer.Exit(1)

    log.console.print()
    log.kv("decision", verdict.decision)
    log.kv("root_cause", verdict.root_cause)
    log.kv("confidence", f"{verdict.confidence:.2f}")
    log.kv("tokens", str(verdict.total_tokens))
    log.console.print(f"  [info]reason:[/info] {verdict.reason}")
    log.console.print()
    log.info("Tool trace:")
    for t in verdict.tool_trace:
        log.console.print(f"  - {t['tool']}({t.get('args', {})}) -> {t.get('summary', '')[:160]}")
    log.warn("(diagnose is advisory — no recovery action was taken)")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _ssh_cmd_prefix(sess: dict, *, port_flag: str = "-p") -> list[str]:
    """Build the leading ``ssh``/``scp`` argv shared by attach/logs/ssh/scp.

    *port_flag* is ``-p`` for ssh and ``-P`` for scp (they spell it differently).
    """
    exe = "scp" if port_flag == "-P" else "ssh"
    port = str(sess.get("port", "22"))
    identity = sess.get("identity_file", "")
    cmd = [exe]
    if port and port != "22":
        cmd += [port_flag, port]
    if identity:
        cmd += ["-i", identity]
    cmd += ["-o", "StrictHostKeyChecking=accept-new"]
    return cmd


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


def _hint_completion() -> None:
    """Auto-install shell completion if not already set up.

    Runs ``beam --install-completion <shell>`` silently, then tells the user
    to source their shell config. That source step is the only thing Python
    can't do — it must happen in the user's shell process.
    """
    shell = os.environ.get("SHELL", "")
    shell_name = Path(shell).name if shell else "bash"

    # Map each shell to the file Typer writes its completion hook into
    rc_files: dict[str, Path] = {
        "bash": Path.home() / ".bashrc",
        "zsh": Path.home() / ".zshrc",
        "fish": Path.home() / ".config" / "fish" / "completions" / "beam.fish",
    }

    # --- Already installed? Check rc file for beam completion marker ---
    rc = rc_files.get(shell_name)
    if rc and rc.exists():
        try:
            if "beam" in rc.read_text(errors="ignore"):
                return  # already present, nothing to do
        except OSError:
            pass

    # --- Run install-completion automatically ---
    beam_exe = sys.argv[0]  # path to the running beam binary
    try:
        result = subprocess.run(
            [beam_exe, "--install-completion", shell_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            source_cmd = f"source {rc}" if rc else f"source ~/.{shell_name}rc"
            log.success(
                f"✓ Tab completion installed for {shell_name}.\n"
                f"  Run this to activate in your current shell:\n"
                f"    {source_cmd}"
            )
        else:
            # Install failed — fall back to a manual hint
            log.info(
                f"Tip: enable Tab completion (session IDs, providers, configs):\n"
                f"  beam --install-completion {shell_name}"
            )
    except Exception:
        pass  # never break beam init because of completion setup


def main() -> None:
    app()


if __name__ == "__main__":
    main()
