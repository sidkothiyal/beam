"""Build the Python venv inside Docker (local image, for cache/transfer)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from beam import log
from beam.config import BeamConfig


class BuildResult:
    def __init__(self, deps_changed: bool, current_hash: str) -> None:
        self.deps_changed = deps_changed
        self.current_hash = current_hash


def build_venv(cfg: BeamConfig, repo_root: Path) -> BuildResult:
    """Hash dependency files and rebuild Docker image if changed.

    Returns a *BuildResult* with ``deps_changed`` and ``current_hash``.
    """
    hash_file = repo_root / ".deps_hash"
    docker_image = cfg.project.docker_image or cfg.project.name or "beam-venv"

    current_hash = _hash_dep_files(cfg, repo_root)

    if hash_file.exists() and hash_file.read_text().strip() == current_hash:
        log.done("Dependencies unchanged -- skipping Docker build.")
        return BuildResult(deps_changed=False, current_hash=current_hash)

    log.step("Generating dependency hashes ...")
    _generate_heavy_deps_hash(repo_root)
    hash_file.write_text(current_hash)

    log.step("Dependencies changed -- rebuilding Docker image locally ...")
    result = subprocess.run(
        ["docker", "build", "-t", f"{docker_image}:latest", str(repo_root)],
        env={**__import__("os").environ, "DOCKER_BUILDKIT": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError("Docker build failed")
    log.done("Image built.")
    return BuildResult(deps_changed=True, current_hash=current_hash)


def _hash_dep_files(cfg: BeamConfig, repo_root: Path) -> str:
    md5 = hashlib.md5()
    for name in cfg.project.dep_files:
        p = repo_root / name
        if p.exists():
            md5.update(p.read_bytes())
    return md5.hexdigest()


def _generate_heavy_deps_hash(repo_root: Path) -> None:
    """Regenerate .heavy-deps-hash from pyproject.toml heavy dep group."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    heavy = sorted(str(d) for d in data.get("dependency-groups", {}).get("heavy", []))
    h = hashlib.md5("\n".join(heavy).encode()).hexdigest()
    (repo_root / ".heavy-deps-hash").write_text(h)
