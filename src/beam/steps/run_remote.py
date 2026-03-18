"""Launch a command in a tmux session on the remote machine."""

from __future__ import annotations

import os
import time

from beam import log
from beam.ssh import SSHClient


class RemoteSession:
    def __init__(self, session_name: str) -> None:
        self.session_name = session_name


def run_remote(
    client: SSHClient,
    command: str,
    *,
    remote_code_dir: str,
    remote_logs_dir: str,
    remote_home: str,
) -> RemoteSession | None:
    """Write a run script on the remote and start it in tmux.

    Returns a *RemoteSession* with the tmux session name, or None if no
    command was provided.
    """
    if not command:
        log.done("Setup complete -- no command provided, skipping execution.")
        return None

    session_name = f"train-{int(time.time())}"

    # Escape single quotes for embedding in the remote script
    remote_cmd = command.replace("'", "'\\''")

    wandb_export = ""
    if os.environ.get("WANDB_API_KEY"):
        wandb_export = f"export WANDB_API_KEY={os.environ['WANDB_API_KEY']}"
    hf_export = ""
    if os.environ.get("HUGGING_FACE_HUB_TOKEN"):
        hf_export = f"export HUGGING_FACE_HUB_TOKEN={os.environ['HUGGING_FACE_HUB_TOKEN']}"

    log.step(f"Starting tmux session {session_name} on remote ...")

    # Build the run script and launch it inside tmux
    script = f"""\
set -euo pipefail

mkdir -p '{remote_logs_dir}'

cat > '{remote_code_dir}/.run_session.sh' <<'INNER_EOF'
#!/bin/bash
export PATH="{remote_home}/.local/bin:$PATH"
export HOME="{remote_home}"
export UV_LINK_MODE=copy
{wandb_export}
{hf_export}
cd '{remote_code_dir}'

# Expose Python headers for torch.compile / triton
_py_include=$(uv run python -c 'import sysconfig; print(sysconfig.get_path("include"))')
export CFLAGS="-I${{_py_include}} ${{CFLAGS:-}}"
export CPATH="${{_py_include}}${{CPATH:+:$CPATH}}"

mkdir -p '{remote_logs_dir}'
[ -e logs ] || ln -sfn '{remote_logs_dir}' logs

bash -c '{remote_cmd}' 2>&1 | tee '{remote_logs_dir}/{session_name}.log'
INNER_EOF
chmod +x '{remote_code_dir}/.run_session.sh'

tmux new-session -d -s '{session_name}' '{remote_code_dir}/.run_session.sh'
"""
    client.run(f"bash -c {_sq(script)}")

    log.done("Remote session started.")
    ssh_disp = client.ssh_display
    host = client.info.remote_host
    log.info(f"  Attach:   ssh {ssh_disp} {host} -t 'tmux attach -t {session_name}'")
    log.info(f"  Logs:     ssh {ssh_disp} {host} 'tail -f {remote_logs_dir}/{session_name}.log'")
    log.info(f"  Sessions: ssh {ssh_disp} {host} 'tmux ls'")

    return RemoteSession(session_name=session_name)


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
