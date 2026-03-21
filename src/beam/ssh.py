"""SSH / rsync subprocess wrappers with retry logic and ControlMaster support."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable

from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from beam import log
from beam.config import SSHConfig


class SSHError(Exception):
    pass


@dataclass
class SSHInfo:
    host: str
    port: str
    user: str = "root"
    identity_file: str = ""

    @property
    def remote_host(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass
class SSHClient:
    """Wrapper around ssh / rsync / ssh-keygen subprocess calls."""

    info: SSHInfo
    cfg: SSHConfig
    _control_path: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._control_path = str(
            Path(tempfile.gettempdir()) / f"beam_ssh_{self.info.host}_{self.info.port}"
        )

    # -- base option builders --------------------------------------------------

    def _ssh_opts(self, *, batch: bool = False) -> list[str]:
        opts: list[str] = []
        if self.info.port:
            opts += ["-p", self.info.port]
        if self.info.identity_file:
            opts += ["-i", self.info.identity_file]
        opts += ["-o", "StrictHostKeyChecking=accept-new"]
        opts += ["-o", f"ConnectTimeout={self.cfg.connect_timeout}"]
        if batch:
            opts += ["-o", "BatchMode=yes"]
        # ControlMaster multiplexing
        opts += ["-o", f"ControlPath={self._control_path}"]
        opts += ["-o", "ControlMaster=auto"]
        opts += ["-o", "ControlPersist=120"]
        return opts

    def _rsync_ssh(self) -> str:
        parts = ["ssh"]
        parts += [f"-o ConnectTimeout={self.cfg.connect_timeout}"]
        if self.info.port:
            parts += [f"-p {self.info.port}"]
        if self.info.identity_file:
            parts += [f"-i {self.info.identity_file}"]
        parts += [f"-o ControlPath={self._control_path}"]
        parts += ["-o ControlMaster=auto"]
        parts += ["-o ControlPersist=120"]
        return " ".join(parts)

    @property
    def ssh_display(self) -> str:
        """Human-readable SSH flags for copy-paste instructions."""
        parts: list[str] = []
        if self.info.port:
            parts += [f"-p {self.info.port}"]
        if self.info.identity_file:
            parts += [f"-i {self.info.identity_file}"]
        return " ".join(parts)

    # -- operations ------------------------------------------------------------

    def remove_from_known_hosts(self) -> None:
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        if not known_hosts.exists():
            return
        kh = str(known_hosts)
        if self.info.port and self.info.port != "22":
            subprocess.run(
                ["ssh-keygen", "-R", f"[{self.info.host}]:{self.info.port}", "-f", kh],
                capture_output=True,
            )
        subprocess.run(
            ["ssh-keygen", "-R", self.info.host, "-f", kh],
            capture_output=True,
        )

    def run(self, command: str, *, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run a command on the remote via SSH, with retries."""
        return self._run_with_retry(command, check=check, input=input)

    def _run_once(self, command: str, *, check: bool, input: str | None = None) -> subprocess.CompletedProcess[str]:
        cmd = ["ssh"] + self._ssh_opts() + [self.info.remote_host, command]
        log.debug(f"ssh> {command}")
        result = subprocess.run(cmd, capture_output=True, text=True, input=input)
        if check and result.returncode != 0:
            raise SSHError(
                f"SSH command failed (rc={result.returncode}): {command}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result

    def _run_with_retry(self, command: str, *, check: bool, input: str | None = None) -> subprocess.CompletedProcess[str]:
        attempts = self.cfg.retry_attempts
        interval = self.cfg.retry_interval
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._run_once(command, check=check, input=input)
            except SSHError as exc:
                last_err = exc
                if attempt < attempts:
                    log.warn(f"SSH command failed (attempt {attempt}/{attempts}), retrying in {interval}s ...")
                    import time
                    time.sleep(interval)
        raise last_err  # type: ignore[misc]

    def check(self) -> bool:
        """Return True if remote is reachable."""
        cmd = (
            ["ssh"]
            + self._ssh_opts(batch=True)
            + [self.info.remote_host, "echo ok"]
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cfg.connect_timeout + 5)
        return result.returncode == 0

    def run_stream(self, command: str) -> subprocess.CompletedProcess[str]:
        """Run a command with stdout/stderr streaming to the console."""
        cmd = ["ssh"] + self._ssh_opts() + [self.info.remote_host, command]
        log.debug(f"ssh (stream)> {command}")
        return subprocess.run(cmd, text=True)

    def pipe_to_remote(
        self,
        local_proc: subprocess.Popen[bytes],
        remote_command: str,
    ) -> int:
        """Pipe stdout of *local_proc* into a remote command via SSH.

        Returns the SSH process return code.
        """
        cmd = ["ssh"] + self._ssh_opts() + [self.info.remote_host, remote_command]
        ssh_proc = subprocess.Popen(cmd, stdin=local_proc.stdout)
        local_proc.stdout.close()  # type: ignore[union-attr]
        ssh_proc.communicate()
        local_proc.wait()
        return ssh_proc.returncode

    def upload_stdin(self, data: str | bytes, remote_command: str) -> subprocess.CompletedProcess[str]:
        """Pipe *data* to a remote command via SSH stdin."""
        cmd = ["ssh"] + self._ssh_opts() + [self.info.remote_host, remote_command]
        input_val = data if isinstance(data, str) else None
        input_bytes = data if isinstance(data, bytes) else None
        if input_bytes is not None:
            result = subprocess.run(cmd, input=input_bytes, capture_output=True)
            return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"))
        return subprocess.run(cmd, input=input_val, capture_output=True, text=True)

    def rsync(
        self,
        source: str,
        dest: str,
        *,
        exclude_file: str | None = None,
        delete: bool = True,
        extra_args: list[str] | None = None,
    ) -> None:
        """rsync with retries."""
        attempts = self.cfg.retry_attempts
        interval = self.cfg.retry_interval

        cmd = [
            "rsync", "-avz", "--progress",
            "--no-owner", "--no-group", "--omit-dir-times",
        ]
        if delete:
            cmd.append("--delete")
        if exclude_file:
            cmd += [f"--exclude-from={exclude_file}"]
        cmd += ["--filter=:- .gitignore"]
        if extra_args:
            cmd += extra_args
        cmd += ["-e", self._rsync_ssh(), source, f"{self.info.remote_host}:{dest}"]

        for attempt in range(1, attempts + 1):
            log.debug(f"rsync attempt {attempt}/{attempts}")
            result = subprocess.run(cmd, text=True)
            if result.returncode == 0:
                return
            if attempt < attempts:
                log.warn(f"rsync failed (attempt {attempt}/{attempts}), retrying in {interval}s ...")
                import time
                time.sleep(interval)
        raise SSHError(f"rsync failed after {attempts} attempts")

    def wait_for_ssh(
        self,
        max_wait: int = 600,
        initial_delay: int = 15,
        status_fn: "Callable[[], dict | None] | None" = None,
    ) -> bool:
        """Poll until SSH is reachable. Returns True on success.

        *status_fn*, if provided, is called each iteration and should return
        a dict with optional keys:

        - ``"status"``  – human-readable status string for logging.
        - ``"stop"``    – if truthy, abort immediately and return False.
        - ``"message"`` – reason string logged when stopping.

        This lets callers inject provider-level health checks (e.g. outbid
        detection) without coupling ssh.py to any specific provider.
        """
        import time

        if initial_delay > 0:
            log.step(f"Waiting {initial_delay}s for network to become routable ...")
            time.sleep(initial_delay)

        log.step(f"Verifying SSH connectivity to {self.info.remote_host} ...")
        elapsed = initial_delay
        interval = 10
        while elapsed < max_wait:
            # Check provider status first — bail early if instance is dead
            status_label = ""
            if status_fn is not None:
                try:
                    result = status_fn()
                except Exception:
                    result = None
                if result:
                    status_label = result.get("status", "")
                    if result.get("stop"):
                        msg = result.get("message", "instance unhealthy")
                        log.warn(f"SSH wait aborted: {msg}")
                        return False

            try:
                if self.check():
                    log.done("SSH connection verified.")
                    return True
            except Exception:
                pass

            extra = f" [{status_label}]" if status_label else ""
            log.info(f"  Waiting for SSH ...{extra} ({elapsed}s / {max_wait}s)")
            time.sleep(interval)
            elapsed += interval

        log.warn(f"SSH not reachable after {max_wait}s.")
        return False

    def close_control_master(self) -> None:
        """Tear down the ControlMaster connection if active."""
        cmd = [
            "ssh",
            "-o", f"ControlPath={self._control_path}",
            "-O", "exit",
            self.info.remote_host,
        ]
        subprocess.run(cmd, capture_output=True)
