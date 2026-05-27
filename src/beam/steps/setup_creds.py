"""Copy credentials (Kaggle, WandB, HuggingFace, ...) to the remote."""

from __future__ import annotations

from pathlib import Path

from beam import log
from beam.config import CredentialsConfig, CRED_ENV_MAP
from beam.ssh import SSHClient


def setup_creds(
    client: SSHClient,
    creds_cfg: CredentialsConfig,
    remote_home: str,
) -> None:
    """Setup credentials on the remote machine."""
    log.step("Setting up credentials on remote ...")

    # -- Credential files ------------------------------------------------------
    for cf in creds_cfg.credential_files:
        local_path = Path(cf.local).expanduser()
        if not local_path.is_file():
            log.warn(f"  Credential file not found locally: {cf.local} -- skipping.")
            continue
        remote_path = (
            cf.remote if cf.remote.startswith("/")
            else f"{remote_home}/{cf.remote}"
        )
        remote_dir = str(Path(remote_path).parent)
        log.info(f"  Syncing {cf.local} → {remote_path} ...")
        client.run(f"mkdir -p '{remote_dir}'")
        data = local_path.read_text()
        client.upload_stdin(
            data,
            f"cat > '{remote_path}' && chmod {cf.chmod} '{remote_path}'",
        )
        log.done(f"  {local_path.name} synced.")

    # -- Environment-variable credentials --------------------------------------
    # Collect env vars: always include the named credential fields that map to
    # env var names, then add any extra vars listed in env_vars.
    named_env_vars = list(CRED_ENV_MAP.values())
    extra_env_vars = [v for v in creds_cfg.env_vars if v not in named_env_vars]
    all_env_vars = named_env_vars + extra_env_vars

    for var_name in all_env_vars:
        value = creds_cfg.get_env_value(var_name)
        if not value:
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
        log.done(f"  {var_name} set.")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
