# Beam — Project Architecture & Reference

> **Purpose of this document:** Provide full context for coding LLMs (and humans) to
> understand, navigate, and modify the Beam codebase quickly.

## What Beam Is

Beam is a CLI tool for remote GPU training deployment. It provisions GPU instances on
**RunPod** or **Vast.ai**, builds and transfers Python virtual environments, syncs code,
sets up credentials, and launches training jobs in tmux sessions — all from a single
`beam run` command. A **Pulse** subsystem provides cron-based health monitoring with
auto-restart, GPU idle detection, and cost tracking.

---

## File Tree

```
beam/
├── pyproject.toml                     # Package metadata, deps, entry point
├── README.md                          # Quick-start guide
├── ARCHITECTURE.md                    # This file
├── examples/
│   └── elemental-beam.yaml            # Full example beam.yaml config
└── src/beam/
    ├── __init__.py                    # Package version (__version__)
    ├── cli.py                         # Typer CLI — all user-facing commands
    ├── config.py                      # Pydantic config models + YAML loader
    ├── log.py                         # Rich-based logging utilities
    ├── pipeline.py                    # Orchestrates the 7-step deploy pipeline
    ├── pulse.py                       # Health monitoring, cron, auto-restart
    ├── session.py                     # Session state persistence (JSON files)
    ├── ssh.py                         # SSH/rsync subprocess wrappers
    ├── providers/
    │   ├── base.py                    # Abstract ProviderBase interface
    │   ├── runpod.py                  # RunPod REST API client
    │   └── vastai.py                  # Vast.ai REST API client
    └── steps/
        ├── __init__.py                # Empty (package marker)
        ├── build_venv.py              # Build Python venv in Docker
        ├── push_image.py              # Push Docker image to ghcr.io
        ├── ensure_machine.py          # Provision or recover a remote machine
        ├── sync_venv.py               # Transfer/link .venv on remote
        ├── sync_code.py               # rsync code to remote
        ├── setup_creds.py             # Copy credentials to remote
        ├── setup_uv.py                # Install uv on remote
        └── run_remote.py              # Launch command in tmux session
```

---

## Build & Dependencies

**`pyproject.toml`** — Uses `hatchling` as build backend. Package name is `beam-deploy`,
version `0.1.0`, requires Python >= 3.11.

Runtime dependencies:
- `typer>=0.9` — CLI framework
- `pydantic>=2.0` — Config validation
- `pyyaml>=6.0` — YAML parsing
- `httpx>=0.27` — HTTP client for provider APIs
- `tenacity>=8.0` — Retry utilities (used in ssh.py)
- `rich>=13.0` — Terminal output formatting

Entry point: `beam = "beam.cli:app"` — the CLI is invoked as the `beam` command.

Install: `uv pip install -e .`

---

## CLI Commands (`src/beam/cli.py`)

The CLI is a Typer app with a `pulse` sub-app. All commands are defined in `cli.py`.

### `beam run`

The primary command. Deploys and runs a command on a remote GPU server.

**Key flags:**
| Flag | Description |
|------|-------------|
| `--ip` | Remote server IP (skips provider provisioning) |
| `-p / --port` | SSH port |
| `-i` | SSH identity file |
| `--user` | SSH user |
| `--provider` | Cloud provider: `runpod` or `vast.ai` |
| `--pod-id` | Existing pod/instance ID |
| `--pod-config` | Named machine config from beam.yaml |
| `-n / --name` | Custom session name for easier tracking (default: `train-{timestamp}`) |
| `--pulse` | Pulse cron interval (e.g. `5m`, `1h`) |
| `--pulse-idle-stop` | Stop instance when GPU is idle |
| `--no-auto-stop-on-complete` | Don't auto-stop when job finishes |
| `--config` | Path to beam.yaml |
| `--verbose / -v` | Verbose output |
| `--quiet / -q` | Suppress output |
| `--dry-run` | Show what would be done |
| `[command]` | Command to run (after `--`) |

**Behavior:**
- No `--ip` and no `--provider` → runs locally via `os.execvp("bash", ...)`.
- `--ip` given → connects to existing machine, runs the remote pipeline.
- `--provider` given → provisions a new machine, then runs the remote pipeline.
- If `--pulse` is specified with a command, installs a cron job after deployment.

### `beam status`

Lists all active sessions in a Rich table. Uses `pulse.list_cron_sessions()`.
`--live` flag SSHes into remotes to check real-time job state (slower).

### `beam init`

Generates a starter `beam.yaml` in the current directory with sensible defaults.

### `beam offers`

Lists compatible GPU instances available for a given machine config, allowing
users to preview pricing and availability before committing to `beam run`.

**Key flags:**
| Flag | Description |
|------|-------------|
| `--provider` | Cloud provider: `runpod` or `vast.ai` (required) |
| `--pod-config` | Named machine config from beam.yaml |
| `--config` | Path to beam.yaml |
| `--verbose / -v` | Verbose output |
| `--quiet / -q` | Suppress output |

**Behavior:**
- **Vast.ai:** Calls `POST /bundles/` with the search body built from the
  resolved `VastAIMachineConfig`. Offers are sorted according to the config's
  `bid_strategy`. Each offer is annotated with `_computed_bid` — the bid price
  the strategy would produce. Results are displayed in a Rich table showing
  offer ID, GPU name, count, VRAM, min bid, computed bid, DLPerf, reliability,
  and machine ID.
- **RunPod:** Calls `GET /gpu-types` and filters results by the resolved
  `RunPodMachineConfig`'s `gpu_type` and `max_cost` constraints. Results are
  displayed in a Rich table showing GPU type, VRAM, and pricing tiers
  (community, secure, spot).
- If `--pod-config` is omitted, uses the first config in `fallback_order`
  (or the first config in the list if no fallback order is set).

### `beam attach <session-id>`

SSHes into the remote and attaches to the tmux session via `os.execvp("ssh", ...)`.
Uses the `tmux_session` field from the session JSON to find the correct tmux session.
Adds `StrictHostKeyChecking=accept-new` to avoid interactive prompts on new hosts.

### `beam logs <session-id>`

Tails remote logs via SSH (`tail -f <remote_logs_dir>/<session_id>.log`). Uses the
stored `remote_logs_dir` and `tmux_session` from the session JSON rather than
reconstructing paths from config. Supports `--follow/--no-follow` (`-f`) flag
(defaults to follow mode).

### `beam stop <session-id>`

Stops the cloud instance, removes its pulse cron job, cleans up pulse state files,
and deletes the session JSON file via `do_idle_stop()` → `uninstall_cron()`.

### `beam pulse list`

Lists sessions and their cron status.

### `beam pulse install`

Manually installs a pulse cron job for a session.

### `beam pulse uninstall`

Removes pulse cron job(s). Supports `--session <id>` or `--all`.

### `beam pulse check`

Runs a single health check for a session. This is the command that cron invokes.

### Helper: `_load_session_or_exit()`

Loads a session JSON or exits with an error message. Used by `attach`, `logs`, `stop`.

---

## Configuration System (`src/beam/config.py`)

### Pydantic Models (nested hierarchy)

```
BeamConfig
├── project: ProjectConfig
│     name, docker_image, dep_files
├── registry: RegistryConfig
│     ghcr_image_name, ghcr_tag
├── remote: RemoteConfig
│     project_dir, home, user
├── ssh: SSHConfig
│     connect_timeout, retry_attempts, retry_interval, identity_file
├── credentials: CredentialsConfig
│     kaggle (path), env_vars (list of env var names)
├── providers: ProvidersConfig
│   ├── runpod: RunPodProviderConfig
│   │     configs: list[RunPodMachineConfig], fallback_order: list[str]
│   └── vastai: VastAIProviderConfig
│         configs: list[VastAIMachineConfig], fallback_order: list[str]
└── pulse: PulseConfig
      gpu_idle_threshold, gpu_idle_max_strikes, gpu_idle_samples,
      gpu_idle_sample_interval, max_retries, auto_stop_on_complete
```

### `RunPodMachineConfig` fields

`name`, `gpu_type`, `gpu_count`, `cloud_tier`, `spot`, `recovery_policy`, `image`,
`template_id`, `container_disk_size`, `volume_size`, `volume_path`,
`network_volume_id`, `min_memory_gb`, `min_vcpu`, `max_cost`, `ports`,
`ssh_public_key_files`, `env_vars`

### `VastAIMachineConfig` fields

`name`, `instance_label`, `gpu_name`, `num_gpus`, `num_gpus_max`, `gpu_ram_min`, `machine_id`,
`offer_type`, `offer_limit`, `price`, `price_max`, `price_step`, `disk_gb`,
`runtype`, `target_state`, `recovery_policy`, `image`, `onstart`, `env`,
`bid_strategy`, `bid_multiplier`, `bid_percentile`,
`reliability_min`, `dph_max`, `dlperf_min`, `dlperf_per_dphtotal_min`

### Config Resolution Order

1. **Defaults** — Pydantic model defaults
2. **YAML** — `beam.yaml` (found by walking up from cwd)
3. **Environment variables** — `BEAM_REMOTE_USER`, `BEAM_REMOTE_PROJECT_DIR`
4. **CLI overrides** — Flat dotted-path dict (e.g. `{"remote.user": "ubuntu"}`)

### Key functions

- `find_config_file(start?)` — Walk up directory tree looking for `beam.yaml` / `beam.yml`.
- `load_config(config_path?, overrides?)` — Full resolution pipeline.
- `load_yaml(path)` — Load YAML file.
- `_apply_overrides(cfg, overrides)` — Apply flat dotted-path overrides.
- `get_project_root(config_path?)` — Directory containing `beam.yaml`, or cwd.

---

## Pipeline Orchestrator (`src/beam/pipeline.py`)

### `RunContext`

A mutable context object that carries state through the pipeline. Created in `cli.py`
and threaded through all steps.

**Initialized from CLI args:**
`cfg`, `repo_root`, `provider`, `pod_id`, `pod_config_name`, `name`, `ip`, `port`,
`command`, `pulse_interval`, `pulse_idle_stop`, `dry_run`

**Populated during pipeline:**
`build_result`, `ssh_client`, `session_name`, `ghcr_image`, `use_registry_venv`

**Derived paths:**
- `remote_code_dir` = `{remote.project_dir}/{project.name}`
- `remote_venv_dir` = `{remote_code_dir}/.venv`
- `remote_logs_dir` = `{remote.project_dir}/logs/{project.name}`
- `logs_dir` = `{repo_root}/.beam/logs` (local)

**Registry image logic:**
If `GITHUB_USERNAME` is set, `ghcr_image` is constructed as
`ghcr.io/{username}/{ghcr_image_name}:{ghcr_tag}`. If both `provider` and
`GITHUB_USERNAME` are set, `use_registry_venv` is `True`.

### `run_local(ctx)`

Runs the command locally via `os.execvp("bash", ...)`. Sets `CFLAGS` and `CPATH`
for Python headers (torch.compile / triton support).

### `run_remote(ctx)` — The 7-Step Pipeline

The step ordering depends on whether registry venv is used:

**With registry venv** (`use_registry_venv=True`):
1. Build venv locally (Docker)
2. Push image to ghcr.io (if deps changed)
3. Ensure machine is running (provider creates instance from the ghcr image)
4. Sync venv (symlink to `/app/.venv`)
5. Sync code (rsync)
6. Setup credentials
7. Launch remote session (tmux)

**Without registry venv** (manual `--ip` or provider without `GITHUB_USERNAME`):
1. Ensure machine is running
2. Build venv locally (Docker)
3. Sync venv (stream via `docker cp | gzip | ssh | tar`)
4. Sync code (rsync)
5. Setup credentials
6. Ensure uv on remote
7. Launch remote session (tmux)

After the pipeline, session state is saved and a summary with SSH/attach commands is
printed.

---

## Pipeline Steps (`src/beam/steps/`)

### `build_venv.py`

- **`build_venv(cfg, repo_root) -> BuildResult`**
- Hashes `dep_files` (typically `pyproject.toml` + `uv.lock`) with MD5.
- Compares against `.deps_hash` file; skips rebuild if unchanged.
- When changed: regenerates `.heavy-deps-hash` from `pyproject.toml`
  `[dependency-groups].heavy`, then runs `docker build`.
- `BuildResult` carries `deps_changed: bool` and `current_hash: str`.

### `push_image.py`

- **`push_image(cfg, ghcr_image)`**
- Logs into `ghcr.io` using `GITHUB_USERNAME` and `DOCKER_GHRC_TOKEN` (or `GITHUB_TOKEN`).
- Tags the local Docker image and pushes to ghcr.io.

### `ensure_machine.py`

- **`ensure_machine(cfg, ...) -> MachineResult`**
- Two code paths:
  - **IP given:** Check reachability (3 attempts). If unreachable and provider+pod_id
    given, try recovery. Recovery respects `recovery_policy` (`"new"` = destroy and
    create fresh, `"restart"` = restart existing).
  - **Provider given:** Create or start via provider with fallback configs.
- **Fallback order:** Tries machine configs in the `fallback_order` list. If one fails,
  waits 15s and tries the next.
- **Vast.ai specifics:** Calls `resolve_offer_id()` which applies the configured
  `bid_strategy` to select an offer and compute a bid price.  The strategy-computed
  `bid_price` is set on the create payload; progressive bidding (increments by
  `price_step` up to `price_max`) still serves as a fallback.
  If a bid instance is outbid during `wait_ready()`, `_create_one()` catches
  `OutbidError`, destroys the dead instance, excludes that offer, and retries with the
  next best offer (up to `_MAX_OUTBID_RETRIES` = 3 retries).
- **Loading-aware timeouts:** Both providers' `wait_ready()` methods start with a
  300s default timeout but dynamically extend to 600s (10 min) when they detect the
  machine is actively loading (e.g. pulling a Docker image for the first time).
  This prevents premature timeouts on cold starts while keeping the short timeout
  for genuine failures. Vast.ai detects loading via `actual_status` (`"loading"`,
  `"creating"`, `"connecting"`). RunPod detects loading when `desiredStatus` is
  `"RUNNING"` but `runtime.status` is not yet available. Both providers also detect
  interruption/reclamation of spot instances and exit the wait loop immediately
  instead of waiting until timeout.
- `MachineResult` carries `ssh_info`, `pod_id`, `provider_name`, `dph_total`.

### `sync_venv.py`

- **`sync_venv(client, build, ...)`**
- Two modes:
  - **Registry venv:** Symlinks `{remote_code_dir}/.venv -> /app/.venv` (image already
    has the venv from ghcr.io).
  - **Stream venv:** `docker create` → `docker cp` → `gzip` → SSH pipe → `gunzip | tar`
    on remote. Checks remote `.deps_hash` to skip if up-to-date. Repairs venv
    interpreter symlinks after transfer (`python3` → system Python).

### `sync_code.py`

- **`sync_code(client, repo_root, remote_code_dir)`**
- Builds an exclude file from hardcoded patterns (`.git`, `.venv`, `.deps_hash`, etc.)
  plus `git ls-files --others --ignored`.
- rsync with `--delete`, `--filter=:- .gitignore`, and the exclude file.

### `setup_creds.py`

- **`setup_creds(client, creds_cfg, remote_home)`**
- Copies `~/.kaggle/kaggle.json` to remote via `upload_stdin`.
- For each env var in `creds_cfg.env_vars`: reads local value, writes/updates
  `export VAR=value` in remote `~/.bashrc`.

### `setup_uv.py`

- **`setup_uv(client, remote_home)`**
- Checks if `uv` is installed; if not, installs via `curl https://astral.sh/uv/install.sh`.
- Appends `export UV_LINK_MODE=copy` to `.bashrc` if not present.

### `run_remote.py`

- **`run_remote(client, command, ..., name="") -> RemoteSession | None`**
- Generates a session name: `{name}-{unix_timestamp}` if a custom name is provided
  via `--name`, otherwise `train-{unix_timestamp}`.
- Writes a run script (`.run_session.sh`) to the remote that:
  - Sets `PATH`, `HOME`, `UV_LINK_MODE`, `WANDB_API_KEY`, `HUGGING_FACE_HUB_TOKEN`.
  - Exposes Python headers for `torch.compile` / triton.
  - Symlinks `logs` directory.
  - Manages sentinel files in `{remote_logs_dir}/.beam_state/`:
    - `RUNNING` — contains `PID|unix_timestamp` while job runs.
    - `FINISHED` — contains `unix_timestamp` on success.
    - `FAILED` — contains `exit_code|unix_timestamp` on failure.
  - Tees output to `{remote_logs_dir}/{session_name}.log`.
- Starts the script in a tmux session via `tmux new-session -d -s <name>`.
- Returns `RemoteSession(session_name, remote_state_dir)`.

---

## Provider Abstraction (`src/beam/providers/`)

### `base.py` — `ProviderBase` (ABC)

All providers must implement:
- `create_instance(config: dict) -> str` — Returns instance/pod ID.
- `start_instance(instance_id)` — Start a stopped instance.
- `stop_instance(instance_id)` — Stop (but keep) an instance.
- `destroy_instance(instance_id)` — Permanently destroy.
- `get_status(instance_id) -> str` — Normalised: `running`, `stopped`, `exited`,
  `unknown`, `error`, `api_error`.
- `get_ssh_info(instance_id) -> SSHInfo | None`
- `wait_ready(instance_id, timeout)` — Poll until `running`.
- `wait_ssh_info(instance_id, timeout) -> SSHInfo` — Poll until SSH info available.

### `runpod.py` — `RunPodProvider`

- REST API base: `https://rest.runpod.io/v1`
- Auth: `RUNPOD_API_KEY` env var (or passed to constructor).
- HTTP client: `httpx.Client` with Bearer token.
- `build_create_payload(cfg, ghcr_image?)` — Builds POST `/pods` JSON from
  `RunPodMachineConfig`. Handles SSH public keys (reads files, concatenates into
  `PUBLIC_KEY` env var) and extra env vars.
- `list_gpu_types(cfg?)` — Calls `GET /gpu-types` and optionally filters by
  `gpu_type` name and `max_cost`. Sorts results by cheapest price for the
  config's cloud tier and spot preference. Used by `beam offers --provider runpod`.
- Status: Reads `runtime.status` or `desiredStatus` from pod info.
- SSH info: Reads `publicIp` and `portMappings["22"]` from pod info.
- `wait_ready()` — Checks `runtime.status` directly (not falling back to
  `desiredStatus`) so the pod is only considered ready when the container is
  actually running, not merely requested. Falls back to `publicIp` +
  `portMappings` presence for API responses that omit `runtime.status`.
  If `desiredStatus` is `"RUNNING"` but no runtime is available yet (image
  pull in progress), extends timeout from 300s to 600s. Detects spot instance
  reclamation (`desiredStatus` flips to `"EXITED"` / `"TERMINATED"`) and
  raises `ProviderError` immediately instead of waiting until timeout.

### `vastai.py` — `VastAIProvider`

- REST API base: `https://console.vast.ai/api/v0`
- Auth: `VAST_API_KEY` env var (or passed to constructor).
- `OutbidError` — Raised when a bid instance is outbid.
- `ResolvedOffer` dataclass — `offer_id`, `dph_total`, `gpu_name`, `num_gpus`,
  `bid_price`.
- `list_compatible_offers(cfg)` — Searches offers via `POST /bundles/`, sorts
  results according to `cfg.bid_strategy`, and annotates each offer with
  `_computed_bid` (the bid price the strategy would produce). Returns the full
  list for display. Used by `beam offers --provider vast.ai`.
- `resolve_offer_id(cfg, exclude_offer_ids?)` — Searches offers via `POST /bundles/`,
  then delegates to `_apply_bid_strategy()` to select an offer and compute a bid price
  according to `cfg.bid_strategy`. Accepts `exclude_offer_ids` to skip
  previously-outbid offers.
- `_apply_bid_strategy(offers, cfg)` — Dispatches to one of five bid strategies
  (see **Bid Strategies** below). Returns `(selected_offer, bid_price)`. For
  on-demand offers the strategy is ignored (pricing is fixed).
- `build_search_body(cfg)` — Builds the search query from `VastAIMachineConfig`
  with filters for GPU name, count (min via `num_gpus`, max via `num_gpus_max`),
  VRAM, machine ID, disk, reliability, dlperf, etc.  Setting `num_gpus_max`
  prevents multi-GPU machines from dominating DLPerf-based strategies (DLPerf
  scales with GPU count, so without a cap, a 4-GPU machine can outrank a
  faster single GPU).
  For bid offers: filters by `min_bid` and sorts by `min_bid` (the minimum bid the
  machine owner accepts). For on-demand: filters and sorts by `dph_total`.
- `build_create_payload(cfg, ghcr_image?)` — Builds PUT `/asks/{id}/` JSON.
- `create_instance(config)` — Supports progressive bidding: starts at
  `bid_price` (set by the strategy via `resolved.bid_price`), increments by
  `price_step` on failure, up to `price_max`.
- Status: Reads `cur_state`, `actual_status`, or `intended_status` from instance info.
- SSH info: Reads `ssh_host` and `ssh_port` from instance info.
- `_is_outbid(info, elapsed)` — Detects outbid condition. Checks for explicit "outbid"
  text in status fields, and uses a heuristic: if `intended_status` is "running" but
  `actual_status` is stuck in "stopped"/"exited" past a 30s grace period, the instance
  is declared outbid. The Vast.ai API does not surface an explicit outbid marker;
  the dashboard computes it client-side.
- `wait_ready()` — Polls `actual_status` until `"running"`. Detects Docker image
  loading states (`"loading"`, `"creating"`, `"connecting"`) and extends timeout from
  300s to 600s (10 min) when observed. Continues checking for outbid during the
  extended wait. The Vast.ai dashboard confirms `"loading"` means the Docker image
  is being downloaded (users are not charged during this state).

### Bid Strategies

When `offer_type` is `"bid"`, `_apply_bid_strategy()` controls which offer is
selected and what price is bid.  The strategy is set via `bid_strategy` in
`VastAIMachineConfig`.  All strategies clamp the computed price between the
offer's `min_bid` (floor) and `price_max` (cap).

| Strategy | Default? | Offer Selection | Bid Price | Config Fields |
|----------|----------|-----------------|-----------|---------------|
| `multiplier` | **yes** | cheapest by `min_bid` | `min_bid * bid_multiplier` | `bid_multiplier` (default 1.1) |
| `cheapest` | | cheapest by `min_bid` | `cfg.price` (progressive increments) | `price`, `price_step`, `price_max` |
| `best_value` | | best `dlperf_per_dphtotal` | `min_bid * bid_multiplier` | `bid_multiplier` |
| `percentile` | | cheapest by `min_bid` | Nth percentile of all `min_bid` values | `bid_percentile` (default 0.5) |
| `score` | | best `dlperf * reliability / min_bid` | `min_bid * bid_multiplier` | `bid_multiplier` |

**`multiplier`** — The default strategy.  Picks the cheapest machine and bids
10% above the host minimum (`min_bid * 1.1`).  The buffer reduces the chance
of being immediately outbid without overpaying.

**`cheapest`** — Legacy behaviour.  Uses the static `price` field as the
starting bid with progressive increments (`price_step`) up to `price_max`.
The bid price is independent of the offer's `min_bid`.

**`best_value`** — Sorts offers by deep-learning performance per dollar
(`dlperf_per_dphtotal`) descending and picks the top result.  Optimises for
training speed per dollar rather than raw cheapness.

**`percentile`** — Sets the bid at the configured percentile of all returned
`min_bid` values.  At `bid_percentile: 0.5` the bid exceeds half of all
available minimum prices, providing market-relative stability.

**`score`** — Computes a composite score `dlperf * reliability / min_bid` for
each offer and picks the highest.  Balances performance, machine reliability,
and cost in a single metric.

After the strategy computes a `bid_price`, progressive bidding (incrementing by
`price_step` on HTTP failure) is still available as a fallback from the
strategy-computed price up to `price_max`.

---

## SSH / rsync (`src/beam/ssh.py`)

### `SSHInfo` (dataclass)

- `host`, `port`, `user`, `identity_file`
- Property `remote_host` → `"{user}@{host}"`

### `SSHClient` (dataclass)

Wraps all SSH/rsync subprocess calls. Uses **ControlMaster** multiplexing for
connection reuse (persists 120s).

**Methods:**
- `run(command, check?, input?)` — Run remote command with retry logic. Retries up to
  `cfg.retry_attempts` times with `cfg.retry_interval` seconds between.
- `check()` — Returns `True` if remote is reachable (batch mode, echo test).
- `run_stream(command)` — Streams stdout/stderr to console.
- `pipe_to_remote(local_proc, remote_command)` — Pipes local process stdout to remote
  via SSH. Used by venv streaming.
- `upload_stdin(data, remote_command)` — Pipes string/bytes to remote command.
- `rsync(source, dest, exclude_file?, delete?, extra_args?)` — rsync with retries,
  uses `--filter=:- .gitignore`.
- `wait_for_ssh(max_wait?, initial_delay?)` — Polls until SSH is reachable. Default
  180s timeout, 15s initial delay, 10s poll interval.
- `remove_from_known_hosts()` — Removes host from `~/.ssh/known_hosts` (handles
  bracketed `[host]:port` format).
- `close_control_master()` — Tears down the ControlMaster connection.
- Property `ssh_display` — Human-readable SSH flags for copy-paste.

### `SSHError`

Exception raised on SSH command failures.

---

## Session State (`src/beam/session.py`)

Sessions are persisted as JSON files at `{project_root}/.beam/logs/sessions/{id}.json`.

### Saved fields

`session_id`, `provider`, `pod_id`, `ip`, `port`, `user`, `identity_file`,
`remote_project_dir`, `remote_home`, `command`, `pod_config_name`,
`recovery_policy`, `ghcr_image`, `remote_state_dir`, `remote_logs_dir`,
`remote_code_dir`, `tmux_session`, `created_at` (UTC ISO),
`runpod_api_key`, `vast_api_key`, `wandb_api_key`, `hf_token`,
`github_username`, plus optional `extra` dict (e.g. `dph_total`).

API keys are saved so that Pulse auto-restart can re-authenticate with providers
without requiring the user's environment.

### Functions

- `save_session(logs_dir, *, session_id, ...) -> Path`
- `load_session(logs_dir, session_id) -> dict`
- `list_sessions(logs_dir) -> list[dict]` — Sorted newest-first.
- `delete_session(logs_dir, session_id)`
- `session_path(logs_dir, session_id) -> Path`

---

## Pulse Monitor (`src/beam/pulse.py`)

Pulse is a cron-based health monitoring system. The cron job calls
`beam pulse check --session-file <path>` at a configured interval.

### Cron Management

- `install_cron(session_id, session_file, interval_secs, ...)` — Creates a wrapper
  shell script at `.beam/logs/.pulse_wrapper_{id}.sh` and installs it in the user's
  crontab. Tags cron lines with `# beam_pulse_{session_id}` for identification.
- `uninstall_cron(session_id?, logs_dir?)` — Removes cron line(s) by tag, cleans up
  state files, deletes session file.
- `list_cron_sessions(logs_dir)` — Lists sessions with `cron_active` flag.

### Health Check Flow (`run_check()`)

1. Load session JSON.
2. Read failure counter (`.pulse_failures_{id}`), idle strike counter
   (`.pulse_idle_{id}`), restart count and timestamp for backoff.
3. Check **instance status** via provider API → normalised status string.
4. If status is unclear and IP exists, fall back to SSH reachability check.
5. Check **job state** via sentinel files on the remote (single SSH call that reads
   all `FINISHED`, `FAILED`, `RUNNING` files and checks tmux).

### Decision Matrix

| Instance Status | Job State | Action |
|----------------|-----------|--------|
| running/reachable | `finished` | Auto-stop if configured |
| running/reachable | `failed` | Soft restart (re-sync + relaunch on same machine) |
| running/reachable | `crashed` | Soft restart |
| running/reachable | `running` | Check GPU → idle strikes → destroy+restart if too many |
| running/reachable | `unknown` | Legacy GPU idle check |
| stopped/exited/unreachable | any | Hard restart (full pipeline re-run) |
| api_error | any | Skip, retry next check |

### Job State Detection

Via sentinel files in `{remote_logs_dir}/.beam_state/`:
- `RUNNING` exists, tmux alive → `running`
- `RUNNING` exists, tmux dead → `crashed`
- `FINISHED` exists → `finished`
- `FAILED` exists → `failed` (contains `exit_code|timestamp`)
- None exist → `unknown`

### Restart Strategies

- **Soft restart** (`do_soft_restart`): Re-sync code, kill old tmux, start new tmux
  session on the same machine. Falls back to hard restart if SSH unreachable.
- **Hard restart** (`do_auto_restart`): Full pipeline re-run with fresh instance.
  Restores env vars from session metadata.
- **Destroy and restart** (`do_destroy_and_restart`): Destroys current instance, then
  hard restart. Used when GPU is idle while job is supposedly running (stuck GPU).
- **Idle stop** (`do_idle_stop`): Stops instance and removes cron. Used when job
  finishes or GPU is idle with no auto-restart.

### Backoff

Exponential backoff: `min(60s * 2^restart_count, 1800s)`. Tracked per-session via
`.pulse_restart_count_{id}` and `.pulse_restart_ts_{id}` files.

### GPU Utilisation Check (`check_gpu_usage`)

1. First checks for active compute processes via
   `nvidia-smi --query-compute-apps=pid`. If any → returns 100%.
2. If no compute processes, takes `gpu_idle_samples` readings of
   `nvidia-smi --query-gpu=utilization.gpu` with `gpu_idle_sample_interval` seconds
   between them, returns peak utilisation.

### Audit Log

Each check appends a JSON line to `pulse_history_{session_id}.jsonl` with timestamp,
instance status, job state, GPU utilisation, action taken, failure/idle counts.

### Cost Estimation (`compute_session_cost`)

Calculates `elapsed_hours * dph_total` from session `created_at` and `dph_total`
(dollars per hour).

---

## Logging (`src/beam/log.py`)

Rich-based logging with themed output. Global verbosity state.

| Function | Behavior |
|----------|----------|
| `info(msg)` | Blue text, suppressed in quiet mode |
| `success(msg)` | Green text, suppressed in quiet mode |
| `warn(msg)` | Yellow text, always shown |
| `error(msg)` | Red "Error:" prefix, always shown |
| `step(msg)` | Yellow text, suppressed in quiet mode |
| `done(msg)` | Green text, suppressed in quiet mode |
| `debug(msg)` | Dim text, only shown in verbose mode |
| `banner(step_num, total, title)` | Step banner with `=====` borders |
| `final_banner(msg?)` | Green completion banner |
| `kv(key, value)` | Indented key-value pair |

`set_verbosity(verbose=, quiet=)` — Sets global `_verbose` / `_quiet` flags.

---

## Environment Variables

### Required (depending on usage)

| Variable | Used By | Purpose |
|----------|---------|---------|
| `RUNPOD_API_KEY` | RunPod provider | API authentication |
| `VAST_API_KEY` | Vast.ai provider | API authentication |
| `GITHUB_USERNAME` | push_image, pipeline | ghcr.io login, registry venv trigger |
| `DOCKER_GHRC_TOKEN` or `GITHUB_TOKEN` | push_image | ghcr.io authentication |

### Optional

| Variable | Used By | Purpose |
|----------|---------|---------|
| `WANDB_API_KEY` | setup_creds, run_remote | Weights & Biases auth |
| `HUGGING_FACE_HUB_TOKEN` | setup_creds, run_remote | HuggingFace auth |
| `BEAM_REMOTE_USER` | config loader | Override remote user |
| `BEAM_REMOTE_PROJECT_DIR` | config loader | Override remote project dir |
| `FORCE_PUSH` | pipeline | Force ghcr.io push even if deps unchanged |

---

## Local File System Artifacts

| Path | Purpose |
|------|---------|
| `beam.yaml` | Project configuration |
| `.deps_hash` | MD5 hash of dep files (skip rebuild) |
| `.heavy-deps-hash` | MD5 of heavy dependency group |
| `.beam/logs/` | Local logs directory root |
| `.beam/logs/sessions/{id}.json` | Session state files |
| `.beam/logs/.pulse_wrapper_{id}.sh` | Cron wrapper scripts |
| `.beam/logs/.pulse_failures_{id}` | Consecutive failure counter |
| `.beam/logs/.pulse_idle_{id}` | GPU idle strike counter |
| `.beam/logs/.pulse_restart_count_{id}` | Restart counter (for backoff) |
| `.beam/logs/.pulse_restart_ts_{id}` | Last restart timestamp |
| `.beam/logs/sessions/pulse_history_{id}.jsonl` | Pulse check audit log |
| `.beam/logs/pulse_cron.log` | Cron stdout/stderr log |

## Remote File System Layout

| Path | Purpose |
|------|---------|
| `{remote.project_dir}/{project.name}/` | Project code root |
| `{remote_code_dir}/.venv/` | Python virtual environment |
| `{remote_code_dir}/.run_session.sh` | Generated run script |
| `{remote.project_dir}/logs/{project.name}/` | Remote logs directory |
| `{remote_logs_dir}/{session_name}.log` | Session output log |
| `{remote_logs_dir}/.beam_state/` | Sentinel file directory |
| `{remote_logs_dir}/.beam_state/RUNNING` | PID and start timestamp |
| `{remote_logs_dir}/.beam_state/FINISHED` | Completion timestamp |
| `{remote_logs_dir}/.beam_state/FAILED` | Exit code and timestamp |

---

## Key Design Patterns

1. **Sequential pipeline with context object** — `RunContext` accumulates state as
   each step runs. Steps are pure functions that take the context + their own args.

2. **Provider abstraction** — `ProviderBase` ABC with `RunPodProvider` and
   `VastAIProvider` implementations. New providers just need to implement 7 methods.

3. **Config layering** — Defaults → YAML → env → CLI, all resolved before the
   pipeline starts. Pydantic models give type safety and validation.

4. **Fallback machine configs** — Multiple named configs per provider with
   `fallback_order`. If one GPU type is unavailable, tries the next.

5. **Sentinel file job tracking** — Instead of parsing logs, the remote run script
   writes `RUNNING`/`FINISHED`/`FAILED` files that Pulse can read in a single SSH call.

6. **Cron-based monitoring** — Pulse installs itself as a cron job. Each invocation
   is stateless (reads counters from files). Exponential backoff prevents restart storms.

7. **SSH ControlMaster** — Reuses SSH connections across multiple commands within a
   session (ControlPersist=120s). Reduces connection overhead.

8. **Bid strategies (Vast.ai)** — Pluggable strategies (`multiplier`, `cheapest`,
   `best_value`, `percentile`, `score`) control offer selection and bid pricing.
   Default `multiplier` bids at `min_bid * 1.1` to buffer against outbidding.
   Progressive bidding (increment by `price_step` on failure, up to `price_max`)
   is still available as a fallback. If outbid after creation, automatically
   destroys the instance and retries with the next offer.

9. **Loading-aware dynamic timeouts** — `wait_ready()` starts with a 300s timeout
   for normal startup, but detects Docker image loading states and extends to 600s
   on first use of a machine. Spot instance interruptions (outbid on Vast.ai,
   reclaimed on RunPod) exit the wait loop immediately.

---

## Common Modification Scenarios

### Adding a new cloud provider

1. Create `src/beam/providers/newprovider.py` implementing `ProviderBase`.
2. Add a new `NewProviderMachineConfig` Pydantic model in `config.py`.
3. Add the provider config to `ProvidersConfig`.
4. Update `_get_provider()` in `ensure_machine.py` to recognise the new name.
5. Update `check_provider_status()` and restart functions in `pulse.py`.
6. Add the provider name to CLI help text.

### Adding a new pipeline step

1. Create `src/beam/steps/new_step.py` with a function.
2. Import in `pipeline.py` and call at the right point in `run_remote()`.
3. Update `total_steps` count and banner numbering.

### Adding a new CLI command

1. Add a new `@app.command()` function in `cli.py`.
2. Use `load_config()` and `get_project_root()` for config access.

### Adding a new config field

1. Add the field to the appropriate Pydantic model in `config.py`.
2. Use it in the relevant step/provider code.
3. Update `beam init` template if it should appear in starter configs.

### Modifying Pulse behavior

1. `run_check()` contains the full decision matrix. Each branch maps
   `(instance_status, job_state)` to an action.
2. New health checks can be added alongside `check_gpu_usage()`.
3. Restart strategies live in `do_soft_restart()`, `do_auto_restart()`,
   `do_destroy_and_restart()`.
