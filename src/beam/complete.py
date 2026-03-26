"""Shell completion callbacks for Beam CLI.

These functions are called by the shell (bash/zsh/fish) when the user presses
Tab. They must be fast — no network calls, minimal imports. Session ID
completion only reads filenames from .beam/logs/sessions/, which is instant.
"""

from __future__ import annotations

from pathlib import Path


def _find_logs_dir() -> Path | None:
    """Walk up from cwd to find the .beam/logs/ directory.

    First tries to find beam.yaml and uses its parent; falls back to walking
    up looking for .beam/logs/ directly.
    """
    # Try config-based discovery first
    try:
        from beam.config import find_config_file
        cfg = find_config_file()
        if cfg:
            candidate = cfg.parent / ".beam" / "logs"
            if candidate.is_dir():
                return candidate
    except Exception:
        pass

    # Direct walk-up fallback
    current = Path.cwd()
    for _ in range(10):
        candidate = current / ".beam" / "logs"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def complete_session_id(incomplete: str) -> list[str]:
    """Return session IDs that match the incomplete prefix.

    Reads only filenames from .beam/logs/sessions/ — no JSON parsing, instant.
    Excludes pulse_history_* files which live in the same directory.
    """
    logs_dir = _find_logs_dir()
    if not logs_dir:
        return []
    sessions_dir = logs_dir / "sessions"
    if not sessions_dir.is_dir():
        return []
    return [
        p.stem
        for p in sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if p.stem.startswith(incomplete)
        and not p.name.startswith("pulse_history_")
    ]


def complete_provider(incomplete: str) -> list[str]:
    """Return provider names matching the incomplete prefix."""
    providers = ["runpod", "vast.ai"]
    return [p for p in providers if p.startswith(incomplete)]


def complete_pod_config(incomplete: str) -> list[str]:
    """Return named machine config names from beam.yaml matching the prefix.

    Loads beam.yaml — slightly heavier than the others, but only fires on
    --pod-config completion. Fails silently if config can't be loaded.
    """
    try:
        from beam.config import load_config
        cfg = load_config()
        names: list[str] = []
        names += [mc.name for mc in cfg.providers.runpod.configs if mc.name]
        names += [mc.name for mc in cfg.providers.vastai.configs if mc.name]
        return [n for n in names if n.startswith(incomplete)]
    except Exception:
        return []
