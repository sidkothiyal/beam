"""Build the Python venv inside Docker (local image, for cache/transfer)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from beam import log
from beam.config import BeamConfig


class BuildResult:
    def __init__(
        self,
        deps_changed: bool,
        current_hash: str,
        image_tag: str = "latest",
        built: bool = False,
    ) -> None:
        self.deps_changed = deps_changed  # dep-file content changed (venv contents)
        self.current_hash = current_hash
        self.image_tag = image_tag        # local docker tag actually produced
        self.built = built                # docker build actually ran this call


def build_venv(
    cfg: BeamConfig,
    repo_root: Path,
    *,
    base_image: str = "",
    image_tag: str = "latest",
) -> BuildResult:
    """Hash dependency files and rebuild the Docker image if needed.

    *base_image* sets the Dockerfile ``RUNTIME_BASE_IMAGE`` build arg (per
    provider); *image_tag* is the local tag to produce (e.g. ``runpod``) so
    provider builds don't clobber each other. Rebuilds when the dep files change
    **or** the target local tag doesn't exist yet (e.g. first build for a new
    provider base). Returns a *BuildResult*.
    """
    hash_file = repo_root / ".deps_hash"
    docker_image = cfg.project.docker_image or cfg.project.name or "beam-venv"
    local_ref = f"{docker_image}:{image_tag}"

    # A base image is optional: when omitted, the Dockerfile's own
    # RUNTIME_BASE_IMAGE default applies. But a value that was *provided yet
    # blank* (e.g. `base_image: "   "` in beam.yaml, or an env that expanded to
    # nothing) is a misconfiguration -- passing `--build-arg RUNTIME_BASE_IMAGE=`
    # would blank the FROM instruction and fail cryptically. Catch it early with
    # an actionable message instead.
    effective_base = (base_image or "").strip()
    if base_image and not effective_base:
        raise RuntimeError(
            "base_image was provided but is blank/whitespace. Either set a valid "
            "runtime base image (e.g. 'nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04') "
            "in beam.yaml under providers.<provider>.configs[*].base_image, or remove "
            "the key entirely to use the Dockerfile's built-in default."
        )

    current_hash = _hash_dep_files(cfg, repo_root)
    deps_unchanged = hash_file.exists() and hash_file.read_text().strip() == current_hash

    if deps_unchanged and _image_exists(local_ref):
        log.done(f"Dependencies unchanged and {local_ref} present -- skipping Docker build.")
        return BuildResult(
            deps_changed=False, current_hash=current_hash, image_tag=image_tag, built=False
        )

    # .deps_hash / .heavy-deps-hash are COPYed by the Dockerfile as cache-bust
    # sentinels; keep them base-independent so the venv layer stays shared.
    log.step("Generating dependency hashes ...")
    _generate_heavy_deps_hash(repo_root)
    hash_file.write_text(current_hash)

    reason = "Dependencies changed" if not deps_unchanged else f"{local_ref} not built yet"
    cmd = ["docker", "build", "-t", local_ref]
    if effective_base:
        cmd += ["--build-arg", f"RUNTIME_BASE_IMAGE={effective_base}"]
        base_desc = effective_base
    else:
        base_desc = "Dockerfile default RUNTIME_BASE_IMAGE"
    cmd.append(str(repo_root))
    log.step(f"{reason} -- building {local_ref} FROM {base_desc} ...")
    result = subprocess.run(
        cmd,
        env={**__import__("os").environ, "DOCKER_BUILDKIT": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker build failed for {local_ref} (base image: {base_desc}).\n"
            f"  - If a base image was set, verify it exists and is spelled correctly, "
            f"and that you're authenticated if it is private (docker login).\n"
            f"  - The Dockerfile needs a non-empty RUNTIME_BASE_IMAGE; a blank value "
            f"makes the FROM instruction fail.\n"
            f"  - See the docker build output above for the underlying error."
        )
    log.done("Image built.")
    return BuildResult(
        deps_changed=not deps_unchanged,
        current_hash=current_hash,
        image_tag=image_tag,
        built=True,
    )


def _image_exists(ref: str) -> bool:
    """True if a local Docker image with *ref* exists."""
    return subprocess.run(
        ["docker", "image", "inspect", ref],
        capture_output=True,
    ).returncode == 0


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
