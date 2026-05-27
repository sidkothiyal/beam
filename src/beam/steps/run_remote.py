"""Launch a command in a tmux session on the remote machine."""

from __future__ import annotations

import time

from beam import log
from beam.ssh import SSHClient


class RemoteSession:
    def __init__(self, session_name: str, remote_state_dir: str = "") -> None:
        self.session_name = session_name
        self.remote_state_dir = remote_state_dir


def run_remote(
    client: SSHClient,
    command: str,
    *,
    remote_code_dir: str,
    remote_logs_dir: str,
    remote_home: str,
    name: str = "",
    reuse_session_name: str = "",
    env_vars: dict[str, str] | None = None,
) -> RemoteSession | None:
    """Write a run script on the remote and start it in tmux.

    Returns a *RemoteSession* with the tmux session name, or None if no
    command was provided.  Pass *reuse_session_name* to keep the same
    session identity (used by soft restart).
    *env_vars* is a dict of environment variables to export inside the
    remote run script (e.g. WANDB_API_KEY, HUGGING_FACE_HUB_TOKEN, ...).
    """
    if not command:
        log.done("Setup complete -- no command provided, skipping execution.")
        return None

    if reuse_session_name:
        session_name = reuse_session_name
    else:
        ts = int(time.time())
        session_name = f"{name}-{ts}" if name else f"train-{ts}"
    remote_state_dir = f"{remote_logs_dir}/.beam_state/{session_name}"

    # Escape single quotes for embedding in the remote script
    remote_cmd = command.replace("'", "'\\''")

    env_exports = "\n".join(
        f"export {k}={v}" for k, v in (env_vars or {}).items() if v
    )

    log.step(f"Starting tmux session {session_name} on remote ...")

    # Build the run script and launch it inside tmux.
    # The script manages RUNNING / FINISHED / FAILED sentinel files so that
    # pulse can determine job state without parsing logs.
    script = f"""\
set -euo pipefail

mkdir -p '{remote_logs_dir}'

cat > '{remote_code_dir}/.run_session.sh' <<'INNER_EOF'
#!/bin/bash
export PATH="{remote_home}/.local/bin:$PATH"
export HOME="{remote_home}"
export UV_LINK_MODE=copy
{env_exports}
cd '{remote_code_dir}'

# Expose Python headers for torch.compile / triton
_py_include=$(uv run python -c 'import sysconfig; print(sysconfig.get_path("include"))')
export CFLAGS="-I${{_py_include}} ${{CFLAGS:-}}"
export CPATH="${{_py_include}}${{CPATH:+:$CPATH}}"

mkdir -p '{remote_logs_dir}'
[ -e logs ] || ln -sfn '{remote_logs_dir}' logs

# -- sentinel file state tracking --
_BEAM_STATE='{remote_state_dir}'
mkdir -p "$_BEAM_STATE"
rm -f "$_BEAM_STATE/RUNNING" "$_BEAM_STATE/FINISHED" "$_BEAM_STATE/FAILED"
echo "$$|$(date -u +%s)" > "$_BEAM_STATE/RUNNING"

set +e
bash -c '{remote_cmd}' 2>&1 | tee '{remote_logs_dir}/{session_name}.log'
_EXIT_CODE=${{PIPESTATUS[0]}}
set -e

rm -f "$_BEAM_STATE/RUNNING"
if [ $_EXIT_CODE -eq 0 ]; then
    echo "$(date -u +%s)" > "$_BEAM_STATE/FINISHED"
else
    echo "$_EXIT_CODE|$(date -u +%s)" > "$_BEAM_STATE/FAILED"
fi
exit $_EXIT_CODE
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

    return RemoteSession(session_name=session_name, remote_state_dir=remote_state_dir)


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
