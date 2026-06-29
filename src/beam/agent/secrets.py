"""Machine-local secret resolution for the pulse agent's LLM backend.

The pulse agent runs from cron, whose wrapper script exports only PATH and HOME
(see ``pulse.install_cron``). The LLM API key is therefore *not* in the process
environment at check time. This module resolves a named secret from, in order:

1. ``os.environ``                       — works for interactive/manual runs.
2. ``~/.beam/agent.env`` (chmod 600)    — machine-local, gitignored, in $HOME
                                          (which cron exports), so it survives
                                          the cron context with no wrapper change.
3. OS keyring (timeout-guarded)         — more secure; never hangs the monitor.

Ported from the ``kb`` project's proven pattern. Set ``BEAM_NO_KEYRING=1`` to
skip the keyring tier entirely (useful on headless boxes where D-Bus blocks).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

# Keyring service namespace and the machine-local fallback file.
_KEYRING_SERVICE = "beam"
AGENT_ENV_FILE = Path.home() / ".beam" / "agent.env"

# Default env-var name per provider (mirrors kb's PROVIDER_ENV).
PROVIDER_ENV: dict[str, str] = {
    "nim": "NVIDIA_API_KEY",
    "openrouter": "BEAM_OPENROUTER_KEY",
    "openai": "BEAM_OPENAI_KEY",
}


def provider_env_name(provider: str) -> str:
    """Return the default secret/env-var name for *provider*."""
    return PROVIDER_ENV.get(provider, f"BEAM_{provider.upper()}_KEY")


def _load_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    if AGENT_ENV_FILE.exists():
        for line in AGENT_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _keyring_disabled() -> bool:
    return os.environ.get("BEAM_NO_KEYRING", "").lower() in ("1", "true", "yes")


def _keyring_op(op: str, *args: str, timeout: float = 3.0) -> tuple[bool, Optional[str]]:
    """Run a keyring call under a hard timeout.

    The Secret Service backend (libsecret/D-Bus) can block indefinitely over SSH
    or on a headless box with no unlocked keyring daemon, which would hang the
    whole monitor. We run the call on a daemon thread and give up after
    ``timeout``, treating a block as "keyring unavailable".
    """
    if _keyring_disabled():
        return False, None
    try:
        import keyring  # optional dependency
    except Exception:
        return False, None

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["v"] = getattr(keyring, op)(_KEYRING_SERVICE, *args)
            box["ok"] = True
        except Exception:
            box["ok"] = False

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():  # blocked on the backend — abandon it
        return False, None
    return bool(box.get("ok")), box.get("v")  # type: ignore[return-value]


def get_secret(name: str) -> Optional[str]:
    """Resolve a secret from (in order) process env, agent.env file, then keyring.

    Local sources are checked first so a blocked/locked keyring never even gets
    called when the secret is already available locally.
    """
    if not name:
        return None
    if name in os.environ and os.environ[name]:
        return os.environ[name]
    file_val = _load_env_file().get(name)
    if file_val:
        return file_val
    ok, val = _keyring_op("get_password", name)
    return val if ok else None


def set_secret(name: str, value: str) -> str:
    """Store a secret in the OS keyring if reachable, else agent.env. Returns backend."""
    ok, _ = _keyring_op("set_password", name, value)
    if ok:
        return "keyring"
    AGENT_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = _load_env_file()
    env[name] = value
    # Write atomically (temp + rename) so a concurrent cron read never sees a
    # half-written file.
    tmp = AGENT_ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(AGENT_ENV_FILE)
    return str(AGENT_ENV_FILE)
