"""Push the venv Docker image to GitHub Container Registry."""

from __future__ import annotations

import os
import subprocess

from beam import log
from beam.config import BeamConfig


def push_image(cfg: BeamConfig, ghcr_image: str) -> None:
    """Tag and push the local Docker image to ghcr.io."""
    github_username = os.environ.get("GITHUB_USERNAME", "")
    token = os.environ.get("DOCKER_GHRC_TOKEN") or os.environ.get("GITHUB_TOKEN", "")

    if not github_username:
        raise RuntimeError("GITHUB_USERNAME is not set. Required to push to ghcr.io.")
    if not token:
        raise RuntimeError(
            "DOCKER_GHRC_TOKEN (or GITHUB_TOKEN) is not set. "
            "Create a PAT with read:packages and write:packages."
        )

    docker_image = cfg.project.docker_image or cfg.project.name or "beam-venv"

    log.step("Logging in to ghcr.io ...")
    login = subprocess.run(
        ["docker", "login", "ghcr.io", "-u", github_username, "--password-stdin"],
        input=token,
        text=True,
        capture_output=True,
    )
    if login.returncode != 0:
        raise RuntimeError(f"docker login ghcr.io failed: {login.stderr}")
    log.done("Logged in to ghcr.io.")

    log.step(f"Tagging and pushing {ghcr_image} ...")
    subprocess.run(
        ["docker", "tag", f"{docker_image}:latest", ghcr_image],
        check=True,
    )
    subprocess.run(["docker", "push", ghcr_image], check=True)
    log.done(f"Pushed {ghcr_image}.")
