"""Ensure uv is installed on the remote machine."""

from __future__ import annotations

from beam import log
from beam.ssh import SSHClient


def setup_uv(client: SSHClient, remote_home: str) -> None:
    """Install uv on the remote if not present, and set UV_LINK_MODE."""
    log.step("Ensuring uv is available on remote ...")
    script = f"""\
export HOME='{remote_home}'
if command -v uv >/dev/null 2>&1 || [ -x '{remote_home}/.local/bin/uv' ]; then
    echo 'uv is already installed.'
else
    echo 'Installing uv ...'
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
if [ -f "${{HOME}}/.bashrc" ] && ! grep -q UV_LINK_MODE "${{HOME}}/.bashrc" 2>/dev/null; then
    echo 'export UV_LINK_MODE=copy' >> "${{HOME}}/.bashrc"
fi
"""
    client.run(f"bash -c {_sq(script)}")
    log.done("uv ready.")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
