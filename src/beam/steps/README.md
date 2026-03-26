# `src/beam/steps/` — Pipeline Steps

Each file in this package is one step in the deploy pipeline. Steps are plain functions called by `pipeline.py` in sequence.

For **deep implementation details** (function signatures, dataclasses, key implementation notes), see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## Overview

The pipeline runs 7 steps. Which 7 depends on whether registry-venv mode is active.

### Pipeline Ordering

**With registry venv** (when `GITHUB_USERNAME` is set and `registry.image` is configured):
```
[1] build_venv      — Build venv in Docker locally
[2] push_image      — Push Docker image to ghcr.io
[3] ensure_machine  — Provision or connect to remote instance
[4] sync_venv       — Link /app/.venv on remote (from registry image)
[5] sync_code       — rsync code to remote
[6] setup_creds     — Write API keys to .bashrc
[7] run_remote      — Write launch script + start tmux
```

**Without registry venv** (direct stream mode):
```
[1] ensure_machine  — Provision or connect to remote instance
[2] build_venv      — Build venv in Docker locally
[3] sync_venv       — Stream venv to remote via gzip pipe
[4] sync_code       — rsync code to remote
[5] setup_creds     — Write API keys to .bashrc
[6] setup_uv        — Install uv on remote if missing
[7] run_remote      — Write launch script + start tmux
```

The key difference: registry mode provisions first (needs the instance to pull the image on-demand), while stream mode builds locally first so we have something to stream.

---

## Step-by-Step Guide

### `build_venv.py` — Build Python Venv

**What it does:** Builds a Python virtual environment inside a Docker container. Uses MD5 hashing of dependency files to skip rebuilds when nothing changed.

**When it runs:** Step 1 (registry) or Step 2 (no registry).

**Key behavior:**
- Hashes `pyproject.toml`, `requirements*.txt`, `setup.py`, etc. → compares to `.deps_hash`
- If unchanged → skips Docker build, returns `deps_changed=False`
- If changed → `docker build` → updates `.deps_hash`
- Returns `BuildResult(deps_changed: bool, current_hash: str)`

```bash
# Triggered when any dep file changes:
beam run --provider vastai python train.py
```

---

### `push_image.py` — Push Docker Image

**What it does:** Logs in to `ghcr.io` and pushes the built Docker image. Only runs in registry-venv mode.

**When it runs:** Step 2 (registry mode only). Skipped entirely in stream mode.

**Key behavior:**
- `docker login ghcr.io` with `DOCKER_GHRC_TOKEN` or `GITHUB_TOKEN`
- `docker tag local-image ghcr.io/{GITHUB_USERNAME}/beam-env:{hash}`
- `docker push`
- Respects `FORCE_PUSH` env var to push even if hash unchanged

---

### `ensure_machine.py` — Provision or Recover Remote Machine

**What it does:** Gets a running remote machine — either by connecting to an existing one or provisioning a new one.

**When it runs:** Step 3 (registry) or Step 1 (no registry). Always runs.

**Key behavior:**

*IP path (existing machine):*
- Validates SSH connectivity
- Checks machine status via provider API
- If stopped → `provider.start_instance()`
- Applies `recovery_policy` if in bad state

*Provider path (new instance):*
- Iterates `fallback_order` of machine configs
- 15 second pause between attempts
- Vast.ai: catches `OutbidError`, retries up to 3 times
- Returns `MachineResult(ssh_info, pod_id, provider_name, dph_total)`

---

### `sync_venv.py` — Transfer Venv to Remote

**What it does:** Gets the Python venv onto the remote machine.

**When it runs:** Step 4 (registry) or Step 3 (no registry). Always runs.

**Two modes:**

*Registry mode:*
- Creates symlink: `{remote_code_dir}/.venv → /app/.venv`
- Assumes the Docker image is already on the machine with venv at `/app/.venv`
- Very fast — no data transfer

*Stream mode:*
- Checks remote `.deps_hash` — if hash matches → skip transfer
- `docker cp container:/app/.venv - | gzip` → SSH pipe → `gunzip | tar -x`
- Repairs Python interpreter symlinks after extraction
- Updates remote `.deps_hash`

---

### `sync_code.py` — Sync Code to Remote

**What it does:** rsyncs local code to `{remote_code_dir}` on the remote machine.

**When it runs:** Step 5 (registry) or Step 4 (no registry). Always runs.

**Key behavior:**
- Builds exclude list: hardcoded patterns + `git ls-files --others --ignored`
- `rsync --delete` to remove stale files on remote
- Excludes: `.git/`, `__pycache__/`, `*.pyc`, `.beam/`, node_modules, `.venv/`, etc.

---

### `setup_creds.py` — Setup Credentials

**What it does:** Copies credentials to the remote machine.

**When it runs:** Step 6 (registry) or Step 5 (no registry). Always runs.

**Key behavior:**
- Copies `kaggle.json` to `~/.kaggle/kaggle.json` if configured
- Appends `export WANDB_API_KEY=...` to remote `~/.bashrc`
- Appends `export HUGGING_FACE_HUB_TOKEN=...` to remote `~/.bashrc`
- Idempotent — checks if already set before appending

---

### `setup_uv.py` — Install uv

**What it does:** Ensures `uv` (Python package manager) is installed on the remote.

**When it runs:** Step 6 (no registry mode only). Skipped in registry mode (Docker image already has uv).

**Key behavior:**
- Checks `which uv` on remote
- If missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Appends `UV_LINK_MODE=copy` to remote `~/.bashrc`

---

### `run_remote.py` — Launch Training Job

**What it does:** Writes a launch script to the remote and starts it in a tmux session.

**When it runs:** Step 7 (both modes). Always runs.

**Key behavior:**

Writes `.run_session.sh` to `{remote_code_dir}`:
```bash
#!/bin/bash
# Sets PATH to include .venv/bin and common tool paths
# Sets env vars (WANDB_API_KEY, HF_TOKEN, torch distributed headers)
# Clears old sentinel files, writes RUNNING sentinel with PID
# Runs the training command with stdout/stderr tee'd to log file
# On exit: writes FINISHED or FAILED sentinel based on exit code
```

Then launches: `tmux new-session -d -s {name} 'bash .run_session.sh'`

Returns `RemoteSession(session_name, log_path)` or `None` on `--dry-run`.

---

## See Also

- **[`ARCHITECTURE.md`](./ARCHITECTURE.md)** — Full function signatures and implementation details
- **[`../pipeline.py`](../pipeline.py)** — Pipeline orchestrator that calls these steps
- **[`../README.md`](../README.md)** — Core module docs including RunContext
