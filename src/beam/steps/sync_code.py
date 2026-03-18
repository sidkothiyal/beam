"""Sync local code to the remote via rsync, respecting .gitignore."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from beam import log
from beam.ssh import SSHClient


def sync_code(
    client: SSHClient,
    repo_root: Path,
    remote_code_dir: str,
) -> None:
    """rsync the repo to the remote, excluding git-ignored files."""
    log.step("Building exclude list from git ...")

    exclude_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".excludes", delete=False
    )
    try:
        # Always-excluded paths
        exclude_file.write(
            ".git\n"
            ".deps_hash\n"
            ".heavy-deps-hash\n"
            ".venv\n"
            ".run_session.sh\n"
            "logs\n"
            "deploy/logs\n"
        )

        # Git-ignored files
        result = subprocess.run(
            [
                "git", "-C", str(repo_root),
                "ls-files", "--others", "--ignored", "--exclude-standard", "--directory",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            exclude_file.write(result.stdout)

        exclude_file.flush()
        num_excludes = sum(1 for _ in open(exclude_file.name))
        log.info(f"  {num_excludes} exclusion rule(s).")

        log.step("Ensuring remote directory exists ...")
        client.run(f"mkdir -p '{remote_code_dir}'")

        log.step(f"Syncing code -> {client.info.remote_host}:{remote_code_dir}/ ...")
        client.rsync(
            f"{repo_root}/",
            f"{remote_code_dir}/",
            exclude_file=exclude_file.name,
        )
        log.done("Code synced.")
    finally:
        Path(exclude_file.name).unlink(missing_ok=True)
