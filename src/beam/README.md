# `src/beam/` — Core Modules

This directory contains all top-level beam modules. Each module has a single, well-defined responsibility.

For **deep implementation details** (class signatures, method docs, data flow), see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

For **provider implementations**, see [`providers/README.md`](./providers/README.md).

For **pipeline step implementations**, see [`steps/README.md`](./steps/README.md).

---

## `__init__.py`

Defines the package version: `__version__ = "0.1.0"`.

```python
from beam import __version__
print(__version__)  # "0.1.0"
```

---

## `cli.py` — Command-Line Interface

**What it does:** Defines all user-facing CLI commands using Typer. Routes `beam run` to the right execution path based on flags.

**When to use it:** When adding new commands, changing flag names, or modifying how commands are routed to pipeline/local execution.

### Commands

```bash
beam run [OPTIONS] [COMMAND]...    # Deploy and run a training job
beam status                        # Show all sessions
beam init                          # Create beam.yaml in current dir
beam offers --provider vastai      # List GPU offers
beam attach <session-id>           # Attach to tmux session
beam logs <session-id>             # Tail training logs
beam stop <session-id>             # Stop instance + remove pulse cron
beam pulse list                    # List active pulse cron jobs
beam pulse install <session-id>    # Install pulse cron
beam pulse uninstall <session-id>  # Remove pulse cron
beam pulse check <session-id>      # Run one health check immediately
```

### Execution Paths for `beam run`

```
beam run python train.py                        → run_local() via os.execvp
beam run --ip 1.2.3.4 python train.py          → pipeline on existing machine
beam run --provider vastai python train.py     → provision + pipeline
```

---

## `config.py` — Configuration

**What it does:** Pydantic v2 model hierarchy for all beam configuration. Loads `beam.yaml`, applies env var overrides, then CLI overrides.

**When to use it:** When reading config in any module — import `load_config()`. When adding new config fields, edit the relevant model class.

### Loading Config

```python
from beam.config import load_config

# Load from beam.yaml (found by walking up from cwd)
cfg = load_config()

# Load from specific path
cfg = load_config(config_path="./my-beam.yaml")

# With CLI overrides (dotted-path dict)
cfg = load_config(overrides={"remote.user": "ubuntu", "remote.project_dir": "/home/ubuntu"})
```

### Config Hierarchy

```
BeamConfig
├── project: ProjectConfig
│   ├── name: str
│   └── repo_root: Path
├── registry: RegistryConfig
│   └── image: str | None
├── remote: RemoteConfig
│   ├── user: str  (env: BEAM_REMOTE_USER)
│   └── project_dir: str  (env: BEAM_REMOTE_PROJECT_DIR)
├── ssh: SSHConfig
│   ├── identity_file: str | None
│   └── port: int
├── credentials: CredentialsConfig
│   ├── wandb_api_key: str
│   ├── hugging_face_hub_token: str
│   └── kaggle_json_path: str | None
├── providers: ProvidersConfig
│   ├── vastai: VastAIProviderConfig
│   │   └── machines: list[VastAIMachineConfig]
│   └── runpod: RunPodProviderConfig
│       └── machines: list[RunPodMachineConfig]
└── pulse: PulseConfig
    ├── enabled: bool
    ├── interval_minutes: int
    ├── idle_stop: bool
    └── ...
```

### Resolution Order

1. Model defaults (Pydantic field defaults)
2. `beam.yaml` (found by walking up from cwd, or `--config` path)
3. Environment variables (`BEAM_REMOTE_USER`, `BEAM_REMOTE_PROJECT_DIR`)
4. CLI overrides (passed as dotted-path dict: `{"remote.user": "ubuntu"}`)

---

## `log.py` — Logging

**What it does:** Rich-based terminal logging with verbosity control. All beam output goes through this module.

**When to use it:** Import and call log functions anywhere you need terminal output.

### Usage

```python
from beam.log import info, success, warn, error, step, done, debug, banner, kv, set_verbosity

set_verbosity(verbose=True, quiet=False)

info("Connecting to remote...")           # Blue, suppressed in quiet mode
success("Connected!")                     # Green, suppressed in quiet mode
warn("SSH key not found, using default")  # Yellow, always shown
error("Failed to connect")               # Red "Error: ...", always shown
step("Syncing code")                     # Yellow step indicator, suppressed in quiet
done("Code synced")                      # Green checkmark, suppressed in quiet
debug("rsync args: ...")                 # Dim, only shown in verbose mode

banner(3, 7, "Sync Code")                # Step banner: [3/7] Sync Code
final_banner("Deploy complete!")         # Final success banner
kv("Instance", "pod-abc123")            # Key: Value output
```

### Verbosity

- `verbose=True` → shows debug messages
- `quiet=True` → suppresses info, success, step, done (warn and error always show)
- Default: neither (info/success/step/done shown, debug hidden)

---

## `pipeline.py` — Deploy Pipeline

**What it does:** Orchestrates the 7-step deploy pipeline. Builds `RunContext`, decides which pipeline variant to use, and calls steps in order.

**When to use it:** Called by `cli.py` for all remote deployments. Edit when changing pipeline ordering or adding steps.

### Pipeline Variants

**With registry venv** (`GITHUB_USERNAME` set + registry configured):
1. `build_venv` — Build venv in Docker
2. `push_image` — Push to ghcr.io
3. `ensure_machine` — Provision instance
4. `sync_venv` — Link `/app/.venv` on remote
5. `sync_code` — rsync code
6. `setup_creds` — Write credentials
7. `run_remote` — Launch tmux session

**Without registry venv** (direct sync):
1. `ensure_machine` — Provision instance
2. `build_venv` — Build venv locally
3. `sync_venv` — Stream venv to remote
4. `sync_code` — rsync code
5. `setup_creds` — Write credentials
6. `setup_uv` — Install uv on remote
7. `run_remote` — Launch tmux session

### RunContext

`RunContext` is the central state object. It holds config, SSH info, derived paths, and session data. All steps receive it (or fields from it).

Key derived paths:
- `remote_code_dir` = `{remote.project_dir}/{project.name}`
- `remote_venv_dir` = `{remote_code_dir}/.venv`
- `remote_logs_dir` = `{remote.project_dir}/logs/{project.name}`
- `logs_dir` = `{repo_root}/.beam/logs`

---

## `pulse.py` — Health Monitoring

**What it does:** Cron-based health monitoring for running jobs. Checks job state via sentinel files, decides on actions, and executes restart strategies. Manages backoff and audit logging.

**When to use it:** Called by `beam pulse check` and the installed cron job. Edit when changing health-check or restart logic.

### Installing Pulse

```python
from beam.pulse import install_pulse, uninstall_pulse, run_check

# Install cron job for a session
install_pulse(session_id="abc123", interval_minutes=5)

# Remove cron job
uninstall_pulse(session_id="abc123")

# Run one health check
run_check(session_id="abc123")
```

### How It Works

1. Reads session JSON to get SSH info + remote paths
2. Connects to remote and reads sentinel files (`RUNNING`, `FINISHED`, `FAILED`)
3. Gets machine status from provider API
4. Applies decision matrix → picks action
5. Executes action (stop, soft restart, auto restart, destroy+restart)
6. Logs decision to `pulse_history_{id}.jsonl`

### Backoff

```python
wait_seconds = min(60 * (2 ** restart_count), 1800)
# 0→60s, 1→120s, 2→240s, ..., max 1800s (30 min)
```

### Agent Mode (optional)

The deterministic matrix above never reads logs, so it cannot tell a transient
failure (spot preemption → restart) from a deterministic one (code bug, NaN
loss, full disk → stop). Set `pulse.agent.enabled: true` in `beam.yaml` to route
failed/crashed/outbid/down states through an **LLM tool-calling agent** that
reads logs, history, disk, GPU, and wandb metrics, then decides — and stops
crash-loops instead of restarting forever. Off by default; falls back to the
deterministic matrix whenever the LLM is unavailable. See
[`agent/README.md`](./agent/README.md).

```bash
beam pulse agent set-key --provider nim   # store the LLM key (keyring / ~/.beam/agent.env)
beam pulse agent test                     # verify the backend
beam pulse agent diagnose --session <id>  # one-shot diagnosis, no action
```

The agent is further informed by two project-level inputs:

- **`BEAM_AGENT.md`** (committed, repo root) — operator knowledge fed to the agent
  every check; it also records durable learnings into its own marker-delimited
  section after a successful check (`pulse.agent.project_doc`).
- **Unified per-job log** — `.beam/logs/sessions/<id>.log` captures every `beam run`
  step **and** every `beam pulse` check for the job in one timeline (`log.py`'s
  `attach_job_log`), which the agent reads via `read_job_log`.

---

## `session.py` — Session State

**What it does:** Reads and writes JSON session files. Each session captures everything needed to reconnect, restart, or monitor a job.

**When to use it:** Read sessions in `beam status`, `beam attach`, `beam stop`, `pulse.py`. Write session after pipeline completes.

### Usage

```python
from beam.session import save_session, load_session, list_sessions, find_session

# Save a session after deploy
save_session(session)

# Load by ID
session = load_session("abc123")

# List all sessions
sessions = list_sessions()

# Find by partial ID or name
session = find_session("abc")
```

### Session Fields

Key fields stored in each `.beam/logs/sessions/{id}.json`:
- `session_id`, `provider`, `pod_id`, `ip`, `port`, `user`, `identity_file`
- `remote_project_dir`, `remote_home`, `command`, `pod_config_name`
- `recovery_policy`, `ghcr_image`
- `remote_state_dir`, `remote_logs_dir`, `remote_code_dir`, `tmux_session`
- `created_at`
- API keys: `runpod_api_key`, `vast_api_key`, `wandb_api_key`, `hf_token`, `github_username`
- `extra: dict` for arbitrary additional data

---

## `ssh.py` — SSH/rsync Wrappers

**What it does:** Low-level SSH and rsync subprocess wrappers with ControlMaster multiplexing. All remote operations in beam go through `SSHClient`.

**When to use it:** Use `SSHClient` anywhere you need to run commands on the remote machine or transfer files.

### Usage

```python
from beam.ssh import SSHInfo, SSHClient

info = SSHInfo(host="1.2.3.4", port=22, user="root", identity_file="~/.ssh/id_rsa")
client = SSHClient(info)

# Run a command (raises on non-zero exit)
client.run("echo hello")

# Check if command succeeds (returns bool)
ok = client.check("test -f /workspace/code/train.py")

# Stream output in real-time
client.run_stream("python train.py")

# rsync local → remote
client.rsync(local_path="./src", remote_path="/workspace/myproject/src")

# Wait for SSH to become available
client.wait_for_ssh(timeout=120)

# Clean up ControlMaster socket
client.close_control_master()
```

### ControlMaster

`SSHClient` uses SSH ControlMaster with `ControlPersist=120s`. The first `run()` call establishes the connection; subsequent calls reuse it. This avoids repeated authentication for the many SSH calls in a pipeline. Call `close_control_master()` when done.

---

## See Also

- **[`ARCHITECTURE.md`](./ARCHITECTURE.md)** — Deep implementation details for all modules above
- **[`providers/README.md`](./providers/README.md)** — RunPod and Vast.ai provider docs
- **[`steps/README.md`](./steps/README.md)** — Pipeline step docs

---

## `complete.py` — Shell Completion Callbacks

**What it does:** Provides fast Tab-completion callbacks for `cli.py`. Called by the shell (bash/zsh/fish) when the user presses Tab — never during normal command execution.

**Key functions:**

- `complete_session_id(incomplete)` — Returns session IDs from `.beam/logs/sessions/` matching the prefix. Reads filenames only (no JSON parsing), sorted newest-first.
- `complete_provider(incomplete)` — Returns `["runpod", "vast.ai"]` filtered by prefix.
- `complete_pod_config(incomplete)` — Loads `beam.yaml` and returns named machine config names matching the prefix. Fails silently if config not found.

**When to edit:** Add a new callback here when you add a CLI option that should complete from a dynamic list. Wire it into `cli.py` via `autocompletion=complete.your_callback`.

**Setup (user-facing):** `beam --install-completion` — one-time per shell. See the project `README.md` for the full completion table.
