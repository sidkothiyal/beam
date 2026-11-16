# `src/beam/steps/` — Implementation Details

Deep reference for all pipeline step implementations. Covers function signatures, return types, and key implementation notes.

For **features, usage, and pipeline ordering**, see [`README.md`](./README.md).

---

## Shared Types

### `BuildResult`

```python
@dataclass
class BuildResult:
    deps_changed: bool      # True if dep files changed (venv was rebuilt)
    current_hash: str       # MD5 hash of current dep files
```

Returned by `build_venv()`. Passed to `sync_venv()` to decide whether to skip transfer.

### `MachineResult`

```python
@dataclass
class MachineResult:
    ssh_info: SSHInfo
    pod_id: str | None          # None if connecting to existing machine by IP
    provider_name: str | None   # "vastai" | "runpod" | None
    dph_total: float | None     # $/hr cost, None if unknown
```

Returned by `ensure_machine()`. Stored in `RunContext` for session saving.

### `RemoteSession`

```python
@dataclass
class RemoteSession:
    session_name: str       # tmux session name
    log_path: str           # Remote path to log file
```

Returned by `run_remote()`. `None` on `--dry-run`.

---

## `build_venv.py`

### Signature

```python
def build_venv(
    cfg: BeamConfig,
    repo_root: Path,
) -> BuildResult
```

### Implementation Notes

**Dep file discovery:**
```python
DEP_FILES = [
    "pyproject.toml", "requirements.txt", "requirements*.txt",
    "setup.py", "setup.cfg", "Pipfile",
]
# Found by glob in repo_root
```

**Hash computation:**
```python
import hashlib
h = hashlib.md5()
for f in sorted(found_dep_files):
    h.update(f.read_bytes())
current_hash = h.hexdigest()
```

**Cache check:**
```python
hash_file = repo_root / ".deps_hash"
if hash_file.exists() and hash_file.read_text().strip() == current_hash:
    return BuildResult(deps_changed=False, current_hash=current_hash)
```

**Docker build:**
```python
# Uses Dockerfile in repo_root (or generates one)
subprocess.run(["docker", "build", "-t", "beam-env:latest", str(repo_root)], check=True)
hash_file.write_text(current_hash)
return BuildResult(deps_changed=True, current_hash=current_hash)
```

---

## `push_image.py`

### Signature

```python
def push_image(
    cfg: BeamConfig,
    ghcr_image: str,    # e.g. "ghcr.io/user/beam-env:abc123"
) -> None
```

### Implementation Notes

**Auth:**
```python
token = os.environ.get("DOCKER_GHRC_TOKEN") or os.environ.get("GITHUB_TOKEN")
username = os.environ["GITHUB_USERNAME"]
subprocess.run(["docker", "login", "ghcr.io", "-u", username, "--password-stdin"],
               input=token.encode(), check=True)
```

**Tag + Push:**
```python
subprocess.run(["docker", "tag", "beam-env:latest", ghcr_image], check=True)
subprocess.run(["docker", "push", ghcr_image], check=True)
```

**Force push:** Set `FORCE_PUSH=1` to push even when `BuildResult.deps_changed=False`.

---

## `ensure_machine.py`

### Signature

```python
def ensure_machine(
    cfg: BeamConfig,
    provider_name: str | None = None,
    pod_id: str | None = None,
    pod_config: str | None = None,
    ssh_info: SSHInfo | None = None,    # Pre-built SSHInfo for IP path
) -> MachineResult
```

### IP Path (existing machine)

```python
if ssh_info:
    client = SSHClient(ssh_info)
    client.wait_for_ssh(timeout=30)
    # If provider known: get_status() → handle recovery_policy
    return MachineResult(ssh_info=ssh_info, pod_id=pod_id, ...)
```

**Recovery policy handling:**
```python
if status == "stopped":
    provider.start_instance(pod_id)
    ssh_info = provider.wait_ready(pod_id)
elif status in ("exited", "error"):
    if recovery_policy == "restart":
        provider.start_instance(pod_id)
        ssh_info = provider.wait_ready(pod_id)
    elif recovery_policy == "destroy":
        provider.destroy_instance(pod_id)
        # fall through to provider path
    elif recovery_policy == "ignore":
        pass  # try anyway
```

### Provider Path (new instance)

```python
# Get ordered list of machine configs to try
configs = get_fallback_order(cfg, provider_name, pod_config)

for i, (provider, machine_cfg) in enumerate(configs):
    if i > 0:
        time.sleep(15)   # 15s between attempts
    try:
        pod_id = provider.create_instance(machine_cfg)
        ssh_info = provider.wait_ready(pod_id)
        return MachineResult(ssh_info, pod_id, provider_name, dph_total)
    except OutbidError:
        if outbid_retries < _MAX_OUTBID_RETRIES:
            outbid_retries += 1
            continue   # retry same config with higher bid
        raise
    except Exception:
        continue  # try next fallback
```

**Self-competition guard (Vast.ai bid path):** before the offer loop,
`_create_one` calls `provider.owned_machine_ids()` (when
`cfg.avoid_owned_machines`, default on) and passes the result to every
`resolve_offer_id(..., exclude_machine_ids=...)` call. This drops offers on
hosts the account already has a live instance on, so a second `beam run` —
which with `pick_strategy=best_value` would otherwise deterministically pick the
same offer — lands on a different machine instead of outbidding itself.

**Constants:**
```python
_MAX_OUTBID_RETRIES = 3
_FALLBACK_DELAY_SECONDS = 15
```

---

## `sync_venv.py`

### Signature

```python
def sync_venv(
    client: SSHClient,
    build: BuildResult,
    cfg: BeamConfig,
    remote_code_dir: str,
    ghcr_image: str | None = None,
) -> None
```

### Registry Mode

```python
if ghcr_image:
    # Symlink remote .venv → /app/.venv (from Docker image layer)
    client.run(f"ln -sfn /app/.venv {remote_code_dir}/.venv")
    return
```

### Stream Mode

```python
# Check remote hash
remote_hash = client.run(f"cat {remote_code_dir}/.deps_hash 2>/dev/null || echo ''")
if remote_hash.stdout.strip() == build.current_hash and not build.deps_changed:
    log.info("venv unchanged, skipping transfer")
    return

# Stream: docker cp → gzip → ssh pipe
client.pipe_to_remote(
    local_command=f"docker cp beam-env-container:/app/.venv - | gzip",
    remote_command=f"cd {remote_code_dir} && gunzip | tar -x",
)

# Repair interpreter symlinks (Python symlinks are absolute, need fixing)
python_path = client.run(f"ls {remote_code_dir}/.venv/bin/python*").stdout.strip()
client.run(f"ln -sfn {remote_code_dir}/.venv/bin/python3.11 {remote_code_dir}/.venv/bin/python")

# Update remote hash
client.run(f"echo '{build.current_hash}' > {remote_code_dir}/.deps_hash")
```

---

## `sync_code.py`

### Signature

```python
def sync_code(
    client: SSHClient,
    repo_root: Path,
    remote_code_dir: str,
) -> None
```

### Exclude List

**Hardcoded excludes:**
```python
HARDCODED_EXCLUDES = [
    ".git/", "__pycache__/", "*.pyc", "*.pyo", "*.pyd",
    ".beam/", ".venv/", "node_modules/", "dist/", "build/",
    "*.egg-info/", ".DS_Store", "*.so", "*.dylib",
]
```

**Git-ignored files:**
```python
result = subprocess.run(
    ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
    cwd=repo_root, capture_output=True, text=True
)
git_excludes = result.stdout.strip().splitlines()
```

**rsync call:**
```python
client.rsync(
    local_path=str(repo_root) + "/",
    remote_path=remote_code_dir,
    exclude=HARDCODED_EXCLUDES + git_excludes,
    delete=True,
)
```

---

## `setup_creds.py`

### Signature

```python
def setup_creds(
    client: SSHClient,
    creds_cfg: CredentialsConfig,
    remote_home: str,
) -> None
```

### Implementation Notes

**Kaggle:**
```python
if creds_cfg.kaggle_json_path:
    client.run(f"mkdir -p {remote_home}/.kaggle")
    client.rsync(
        local_path=str(Path(creds_cfg.kaggle_json_path).expanduser()),
        remote_path=f"{remote_home}/.kaggle/kaggle.json",
    )
    client.run(f"chmod 600 {remote_home}/.kaggle/kaggle.json")
```

**API keys in .bashrc:**
```python
for key, value in [
    ("WANDB_API_KEY", creds_cfg.wandb_api_key),
    ("HUGGING_FACE_HUB_TOKEN", creds_cfg.hugging_face_hub_token),
]:
    if value:
        # Idempotent: only append if not already present
        client.run(
            f"grep -q 'export {key}=' {remote_home}/.bashrc || "
            f"echo 'export {key}={value}' >> {remote_home}/.bashrc"
        )
```

---

## `setup_uv.py`

### Signature

```python
def setup_uv(
    client: SSHClient,
    remote_home: str,
) -> None
```

### Implementation Notes

```python
# Check if uv exists
if not client.check("which uv"):
    client.run_stream("curl -LsSf https://astral.sh/uv/install.sh | sh")

# Add UV_LINK_MODE=copy to .bashrc (avoids hardlink issues on some filesystems)
client.run(
    f"grep -q 'UV_LINK_MODE' {remote_home}/.bashrc || "
    f"echo 'export UV_LINK_MODE=copy' >> {remote_home}/.bashrc"
)
```

---

## `run_remote.py`

### Signature

```python
def run_remote(
    client: SSHClient,
    command: list[str],
    cfg: BeamConfig,
    remote_code_dir: str,
    remote_logs_dir: str,
    name: str = "",
    auto_stop_on_complete: bool = True,
    dry_run: bool = False,
) -> RemoteSession | None
```

### Launch Script (`.run_session.sh`)

Written to `{remote_code_dir}/.run_session.sh`:

```bash
#!/bin/bash
set -e

# PATH setup
export PATH="{remote_code_dir}/.venv/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Source bashrc for credentials
source ~/.bashrc 2>/dev/null || true

# Torch distributed env vars (for multi-GPU)
export NCCL_DEBUG=INFO
export TORCH_DISTRIBUTED_DEBUG=INFO

# State dir
STATE_DIR="{remote_logs_dir}/.beam_state"
mkdir -p "$STATE_DIR" "{remote_logs_dir}"

# Clear old sentinels
rm -f "$STATE_DIR/RUNNING" "$STATE_DIR/FINISHED" "$STATE_DIR/FAILED"

# Write RUNNING sentinel
echo "$$|$(date -Iseconds)" > "$STATE_DIR/RUNNING"

# Run command, tee to log
LOG_FILE="{remote_logs_dir}/{name}.log"
{command_str} 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

# Write exit sentinel
rm -f "$STATE_DIR/RUNNING"
if [ $EXIT_CODE -eq 0 ]; then
    echo "$(date -Iseconds)" > "$STATE_DIR/FINISHED"
else
    echo "$EXIT_CODE|$(date -Iseconds)" > "$STATE_DIR/FAILED"
fi

exit $EXIT_CODE
```

### tmux Launch

```python
tmux_session = name or f"beam-{session_id[:8]}"
client.run(
    f"tmux new-session -d -s {tmux_session} "
    f"'bash {remote_code_dir}/.run_session.sh'"
)
return RemoteSession(session_name=tmux_session, log_path=log_file)
```

**Dry run:** Prints the launch script content and tmux command, returns `None`.

---

## See Also

- **[`README.md`](./README.md)** — Features, usage, pipeline ordering
- **[`../pipeline.py`](../pipeline.py)** — Calls these steps in sequence
- **[`../ARCHITECTURE.md`](../ARCHITECTURE.md)** — RunContext fields and pipeline variants
