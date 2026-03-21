"""Session state — save/load/list/delete (JSON files)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sessions_dir(logs_dir: Path) -> Path:
    d = logs_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_path(logs_dir: Path, session_id: str) -> Path:
    return _sessions_dir(logs_dir) / f"{session_id}.json"


def save_session(
    logs_dir: Path,
    *,
    session_id: str,
    provider: str = "",
    pod_id: str = "",
    ip: str = "",
    port: str = "",
    user: str = "root",
    identity_file: str = "",
    remote_project_dir: str = "/workspace",
    remote_home: str = "/root",
    command: str = "",
    pod_config_name: str = "",
    recovery_policy: str = "restart",
    ghcr_image: str = "",
    remote_state_dir: str = "",
    remote_logs_dir: str = "",
    remote_code_dir: str = "",
    tmux_session: str = "",
    repo_root: str = "",
    name: str = "",
    machine_config: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Persist session state to a JSON file. Returns the file path."""
    data: dict[str, Any] = {
        "session_id": session_id,
        "provider": provider,
        "pod_id": pod_id,
        "ip": ip,
        "port": port,
        "user": user,
        "identity_file": identity_file,
        "remote_project_dir": remote_project_dir,
        "remote_home": remote_home,
        "command": command,
        "pod_config_name": pod_config_name,
        "recovery_policy": recovery_policy,
        "ghcr_image": ghcr_image,
        "remote_state_dir": remote_state_dir,
        "remote_logs_dir": remote_logs_dir,
        "remote_code_dir": remote_code_dir,
        "tmux_session": tmux_session,
        "repo_root": repo_root,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Preserve API keys for pulse auto-restart (same as shell version)
        "runpod_api_key": os.environ.get("RUNPOD_API_KEY", ""),
        "vast_api_key": os.environ.get("VAST_API_KEY", ""),
        "wandb_api_key": os.environ.get("WANDB_API_KEY", ""),
        "hf_token": os.environ.get("HUGGING_FACE_HUB_TOKEN", ""),
        "github_username": os.environ.get("GITHUB_USERNAME", ""),
    }
    # Snapshot the machine config used to create the instance so that
    # pulse auto-restart can recreate without depending on beam.yaml.
    if machine_config:
        data["machine_config"] = machine_config
    if extra:
        data.update(extra)

    p = session_path(logs_dir, session_id)
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p


def load_session(logs_dir: Path, session_id: str) -> dict[str, Any]:
    p = session_path(logs_dir, session_id)
    if not p.exists():
        raise FileNotFoundError(f"Session file not found: {p}")
    return json.loads(p.read_text())


def list_sessions(logs_dir: Path) -> list[dict[str, Any]]:
    """Return all saved sessions sorted by creation time (newest first)."""
    d = _sessions_dir(logs_dir)
    sessions = []
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            sessions.append(json.loads(f.read_text()))
        except Exception:
            pass
    return sessions


def delete_session(logs_dir: Path, session_id: str) -> None:
    p = session_path(logs_dir, session_id)
    p.unlink(missing_ok=True)
