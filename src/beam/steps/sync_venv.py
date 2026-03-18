"""Transfer or link .venv on the remote machine."""

from __future__ import annotations

import subprocess

from beam import log
from beam.ssh import SSHClient
from beam.steps.build_venv import BuildResult


def sync_venv(
    client: SSHClient,
    build: BuildResult,
    *,
    remote_code_dir: str,
    remote_venv_dir: str,
    docker_image: str,
    use_registry_venv: bool = False,
) -> None:
    """Sync venv to the remote — either symlink or stream from Docker."""
    if use_registry_venv:
        _symlink_registry_venv(client, remote_code_dir, remote_venv_dir)
        return

    _stream_venv(
        client,
        build,
        remote_code_dir=remote_code_dir,
        remote_venv_dir=remote_venv_dir,
        docker_image=docker_image,
    )


def _symlink_registry_venv(
    client: SSHClient, remote_code_dir: str, remote_venv_dir: str
) -> None:
    """Instance was created from ghcr.io image; venv is at /app/.venv."""
    log.step("Linking remote venv to container /app/.venv ...")
    script = f"""\
set -e
mkdir -p '{remote_code_dir}'
if [ -L '{remote_venv_dir}' ]; then
  target=$(readlink '{remote_venv_dir}')
  if [ "$target" = '/app/.venv' ]; then
    echo 'Venv already linked to /app/.venv.'
    exit 0
  fi
fi
if [ -d '{remote_venv_dir}' ] && [ ! -L '{remote_venv_dir}' ]; then
  rm -rf '{remote_venv_dir}'
fi
ln -sfn /app/.venv '{remote_venv_dir}'
echo 'Linked {remote_venv_dir} -> /app/.venv'
"""
    client.run(f"bash -c {_shell_quote(script)}")
    log.done("Venv linked from container image.")


def _stream_venv(
    client: SSHClient,
    build: BuildResult,
    *,
    remote_code_dir: str,
    remote_venv_dir: str,
    docker_image: str,
) -> None:
    """Stream .venv from local Docker image to remote via docker cp | gzip | ssh | tar."""
    remote_hash = ""
    try:
        r = client.run(f"cat '{remote_venv_dir}/.deps_hash' 2>/dev/null || echo ''", check=False)
        remote_hash = r.stdout.strip()
    except Exception:
        pass

    if not build.deps_changed and remote_hash == build.current_hash:
        log.done("Venv on remote is up-to-date -- skipping transfer.")
        return

    log.step("Venv not found or outdated on remote -- transferring ...")
    log.info("  Extracting .venv from Docker image and streaming to remote ...")

    attempts = client.cfg.retry_attempts
    interval = client.cfg.retry_interval

    for attempt in range(1, attempts + 1):
        container_id = ""
        try:
            # Create a temporary container to access image filesystem
            r = subprocess.run(
                ["docker", "create", f"{docker_image}:latest", "true"],
                capture_output=True, text=True, check=True,
            )
            container_id = r.stdout.strip()

            # Prepare remote directory
            client.run(
                f"mkdir -p '{remote_code_dir}' && rm -rf '{remote_venv_dir}' && mkdir -p '{remote_venv_dir}'"
            )

            # Stream: docker cp -> gzip -> ssh -> gunzip -> tar
            docker_cp = subprocess.Popen(
                ["docker", "cp", f"{container_id}:/app/.venv", "-"],
                stdout=subprocess.PIPE,
            )
            gzip_proc = subprocess.Popen(
                ["gzip"],
                stdin=docker_cp.stdout,
                stdout=subprocess.PIPE,
            )
            docker_cp.stdout.close()  # type: ignore[union-attr]

            rc = client.pipe_to_remote(
                gzip_proc,
                f"gunzip | tar xf - -C '{remote_venv_dir}' --strip-components=1 --no-same-owner",
            )
            if rc == 0:
                _cleanup_container(container_id)
                break

            raise RuntimeError(f"Venv transfer SSH pipe returned {rc}")

        except Exception as exc:
            _cleanup_container(container_id)
            if attempt < attempts:
                log.warn(f"Venv transfer failed (attempt {attempt}/{attempts}), retrying in {interval}s ...")
                import time
                time.sleep(interval)
            else:
                raise RuntimeError(f"Failed to transfer venv after {attempts} attempts") from exc

    # Store hash on remote
    client.run(f"echo '{build.current_hash}' > '{remote_venv_dir}/.deps_hash'")

    # Repair venv interpreter symlinks
    log.step("Repairing venv interpreter symlinks on remote ...")
    fixup = f"""\
set -e
REMOTE_VENV_DIR='{remote_venv_dir}'
REMOTE_PY=$(command -v python3 || true)
if [ -z "$REMOTE_PY" ]; then REMOTE_PY=$(command -v python || true); fi
if [ -n "$REMOTE_PY" ] && [ -d "$REMOTE_VENV_DIR" ]; then
  rm -f "${{REMOTE_VENV_DIR}}/bin/python3" "${{REMOTE_VENV_DIR}}/bin/python"
  ln -sf "$REMOTE_PY" "${{REMOTE_VENV_DIR}}/bin/python3"
  ln -sf python3 "${{REMOTE_VENV_DIR}}/bin/python"
  PY_HOME=$(dirname "$(dirname "$REMOTE_PY")")
  PY_VERSION=$("$REMOTE_PY" -c 'import sys; print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")' 2>/dev/null || true)
  if [ -n "$PY_VERSION" ] && [ -f "${{REMOTE_VENV_DIR}}/pyvenv.cfg" ]; then
    sed -i "s|^home = .*|home = $PY_HOME|" "${{REMOTE_VENV_DIR}}/pyvenv.cfg"
    sed -i "s|^version = .*|version = $PY_VERSION|" "${{REMOTE_VENV_DIR}}/pyvenv.cfg"
  fi
fi
"""
    client.run(f"bash -c {_shell_quote(fixup)}")
    log.done("Venv transferred to remote.")


def _cleanup_container(container_id: str) -> None:
    if container_id:
        subprocess.run(
            ["docker", "rm", container_id],
            capture_output=True,
        )


def _shell_quote(s: str) -> str:
    """Single-quote a string for bash -c."""
    return "'" + s.replace("'", "'\\''") + "'"
