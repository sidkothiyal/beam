"""Copy credentials (Kaggle, WandB, HuggingFace, ...) to the remote."""

from __future__ import annotations

import os
from pathlib import Path

from beam import log
from beam.config import CredentialsConfig
from beam.ssh import SSHClient


def setup_creds(
    client: SSHClient,
    creds_cfg: CredentialsConfig,
    remote_home: str,
) -> None:
    """Setup credentials on the remote machine."""
    log.step("Setting up credentials on remote ...")

    # -- Kaggle ----------------------------------------------------------------
    local_kaggle = Path(creds_cfg.kaggle).expanduser()
    if local_kaggle.is_file():
        log.info(f"  Copying Kaggle credentials to {remote_home}/.kaggle/ ...")
        client.run(f"mkdir -p '{remote_home}/.kaggle'")
        data = local_kaggle.read_text()
        client.upload_stdin(
            data,
            f"cat > '{remote_home}/.kaggle/kaggle.json' && chmod 600 '{remote_home}/.kaggle/kaggle.json'",
        )
        log.done("Kaggle credentials copied.")
    else:
        log.warn("  No local Kaggle credentials found -- skipping.")

    # -- Environment-variable credentials --------------------------------------
    for var_name in creds_cfg.env_vars:
        value = os.environ.get(var_name, "")
        if not value:
            log.warn(f"  {var_name} not set locally -- skipping.")
            continue
        log.info(f"  Setting {var_name} in {remote_home}/.bashrc ...")
        script = (
            f"if grep -q '^export {var_name}=' '{remote_home}/.bashrc' 2>/dev/null; then "
            f"  sed -i 's|^export {var_name}=.*|export {var_name}={value}|' '{remote_home}/.bashrc'; "
            f"else "
            f"  echo 'export {var_name}={value}' >> '{remote_home}/.bashrc'; "
            f"fi"
        )
        client.run(f"bash -c {_sq(script)}")
        log.done(f"{var_name} set.")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
