# beam

**Remote GPU training deployment CLI.** Provision cloud GPU instances, sync your code and environment, and launch training jobs — all from one command.

---

## What It Does

`beam` handles the full lifecycle of remote GPU training:

1. **Provisions** a GPU instance on RunPod or Vast.ai (or connects to an existing one)
2. **Builds** a Python virtual environment in Docker (locally or reuses cached)
3. **Syncs** the venv and your code to the remote machine via rsync/SSH
4. **Sets up** credentials (W&B, HuggingFace, Kaggle) on the remote
5. **Launches** your training command in a tmux session with sentinel-file tracking
6. **Monitors** the job via the Pulse subsystem (cron-based health checks, auto-restart, GPU idle detection) — optionally with an **LLM agent** that reads logs/metrics to diagnose failures and stop futile restart loops

---

## Features

- 🚀 **One-command deploy** — `beam run --provider vastai python train.py`
- ☁️ **Multi-provider** — RunPod and Vast.ai, with fallback ordering
- 🔄 **Smart venv sync** — MD5-hashed deps, incremental transfers, Docker-built envs
- 📦 **Registry mode** — Push venv as Docker image to ghcr.io for fast deploys
- 💓 **Pulse monitoring** — Cron-based health checks with auto-restart and GPU idle detection
- 🤖 **Pulse agent mode** — Optional LLM agent (NVIDIA NIM / OpenRouter / OpenAI) that reads logs, history, disk, GPU & wandb metrics to tell transient failures from real bugs and stop crash-loops (`pulse.agent.enabled`; `beam pulse agent set-key`)
- 🧠 **Project memory + unified job log** — A committed `BEAM_AGENT.md` feeds operator knowledge to the agent (and it records durable learnings back); one per-job log captures every `beam run` + `beam pulse` step so the agent sees the whole timeline
- 💸 **Cost tracking** — Log $/hr per session, idle-stop to save money
- 🎯 **Bid strategies** — 5 strategies for Vast.ai spot instances
- 🔁 **Recovery** — Reconnect to existing instances, recover from spot preemption
- 📋 **Session state** — JSON session files for status, attach, logs

---

## Install

```bash
# Requires Python 3.11+, uv, docker (for venv builds)
git clone <repo>
cd beam
uv pip install -e .
```

The entry point is `beam` (defined as `beam-deploy` in pyproject.toml).

---

## Quick Start

### 1. Create `beam.yaml` in your project root

```yaml
project:
  name: my-training-run
  repo_root: .

registry:
  # Optional: set to push venv as Docker image (faster syncs)
  # Requires GITHUB_USERNAME + DOCKER_GHRC_TOKEN env vars

remote:
  user: root
  project_dir: /workspace

ssh:
  identity_file: ~/.ssh/id_rsa

credentials:
  wandb_api_key: ${WANDB_API_KEY}
  hugging_face_hub_token: ${HUGGING_FACE_HUB_TOKEN}

providers:
  vastai:
    machines:
      - name: rtx4090
        gpu_name: RTX_4090
        num_gpus: 1
        disk_gb: 50
        image: pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime
        bid_strategy: multiplier
        bid_multiplier: 1.1

  runpod:
    machines:
      - name: a100
        gpu_type: NVIDIA A100 80GB PCIe
        gpu_count: 1
        cloud_tier: SECURE
        image: runpod/pytorch:2.3.0-py3.11-cuda12.1.1-devel-ubuntu22.04

pulse:
  enabled: true
  interval_minutes: 5
  idle_stop: true
  idle_threshold_minutes: 15
```

### 2. Deploy

```bash
# Provision a new instance and run training
beam run --provider vastai --pod-config rtx4090 python train.py

# Connect to an existing machine
beam run --ip 1.2.3.4 -p 22 python train.py

# Dry run (no provisioning)
beam run --provider vastai --dry-run python train.py

# Run locally (no ip or provider = local passthrough)
beam run python train.py
```

### 3. Monitor

```bash
beam status                    # Show all sessions (live job state by default)
beam status --offline          # Fast offline view, skips SSH
beam attach <session-id>       # Attach to tmux session
beam logs <session-id>         # Tail training logs
beam ssh <session-id>          # Open an SSH shell on the instance
beam scp <session-id> ...      # Copy files to/from the instance
beam stop <session-id>         # Destroy instance + remove pulse cron
```

---

## CLI Reference

### `beam run`

Launch a training job (provision → sync → run).

| Flag | Short | Description |
|------|-------|-------------|
| `--ip` | | Connect to existing machine at this IP |
| `--port` | `-p` | SSH port (default: 22) |
| `-i` | | SSH identity file |
| `--user` | | SSH user (default: root) |
| `--provider` | | Cloud provider: `vastai` or `runpod` |
| `--pod-id` | | Reconnect to existing pod by ID |
| `--pod-config` | | Machine config name from beam.yaml |
| `--name` | `-n` | Session name |
| `--pulse` | | Pulse cron interval (e.g. `5m`); defaults to `pulse.interval` in beam.yaml |
| `--pulse-idle-stop` / `--no-pulse-idle-stop` | | Stop instance when GPU is idle; defaults to `pulse.idle_stop` in beam.yaml |
| `--no-auto-stop-on-complete` | | Don't stop instance when job finishes |
| `--config` | | Path to beam.yaml |
| `--verbose` | `-v` | Verbose output |
| `--quiet` | `-q` | Suppress non-essential output |
| `--dry-run` | | Print plan, don't execute |
| `[command]` | | Training command to run |

**Execution paths:**
- No `--ip` and no `--provider` → runs command locally via `os.execvp`
- `--ip` given → pipeline on existing machine (no provisioning)
- `--provider` given → provision new instance, then pipeline

### `beam status`

Show all sessions — SSHes into each remote by default to report live Job State.

Flags:  to skip SSH checks (fast, offline view)

### `beam init`

Initialize a `beam.yaml` in the current directory.

### `beam offers`

List available GPU offers from a provider.

```bash
beam offers --provider vastai
beam offers --provider runpod
```

### `beam attach`

Attach to the tmux session of a running job.

```bash
beam attach <session-id>
```

### `beam logs`

Tail the log file for a session.

```bash
beam logs <session-id>
beam logs <session-id> --follow
```

### `beam ssh`

Open an interactive SSH shell on a session's instance, or run a one-off command.

```bash
beam ssh <session-id>                 # interactive shell
beam ssh <session-id> -- nvidia-smi   # run a command
```

### `beam scp`

Copy files to/from a session's instance. Prefix either path with `remote:` to
refer to the instance.

```bash
beam scp <session-id> ./data remote:/workspace/data    # upload
beam scp <session-id> remote:/workspace/out ./out       # download
beam scp <session-id> --no-recursive ./f remote:/tmp/f  # single file, no -r
```

### `beam stop`

Destroy a session's instance and remove its Pulse cron job. This permanently
tears the instance down for both RunPod and Vast.ai (no lingering compute or
storage cost) — it does not merely leave it in a stopped/inactive state.

```bash
beam stop <session-id>
```

### `beam pulse`

Manage Pulse monitoring cron jobs.

```bash
beam pulse list                  # List active pulse jobs
beam pulse install <session-id>  # Install cron for session
beam pulse uninstall <session-id># Remove cron for session
beam pulse check <session-id>    # Run one health check now
```

---

## `beam.yaml` Config Reference

All fields are optional unless noted.

### `project`
```yaml
project:
  name: my-project          # Project name (used in remote paths)
  repo_root: .              # Local repo root (default: auto-detected)
```

### `registry`
```yaml
registry:
  ghcr_image_name: loom-env     # ghcr.io/<user>/<name>
  ghcr_tag: latest              # base tag; a per-provider suffix is appended
```
In registry mode the pushed image is tagged **per provider** —
`ghcr.io/<user>/<ghcr_image_name>:<ghcr_tag>-<provider>` (e.g. `loom-env:latest-runpod`,
`loom-env:latest-vastai`) — so each provider gets an image built `FROM` a base it caches on its
hosts (see `base_image` below). A provider-less/manual build uses the plain `<ghcr_tag>` tag.
The identical venv layer is shared across the per-provider images (ghcr dedups it by digest).

### `remote`
```yaml
remote:
  user: root                    # Remote SSH user
  project_dir: /workspace       # Base dir on remote (env: BEAM_REMOTE_PROJECT_DIR)
```

### `ssh`
```yaml
ssh:
  identity_file: ~/.ssh/id_rsa  # SSH key path
  port: 22
```

### `credentials`
```yaml
credentials:
  wandb_api_key: ""
  hugging_face_hub_token: ""
  kaggle_json_path: ~/.kaggle/kaggle.json
```

### `providers.vastai.machines[]`
```yaml
- name: rtx4090                  # Config name (use with --pod-config)
  gpu_name: "RTX 4090"           # GPU model filter; whole-token prefix ("RTX PRO 6000" matches "... S"/"... WS")
  num_gpus: 1
  num_gpus_max: 1                # Max GPUs (for multi-GPU offers)
  gpu_ram_min: 20                # Minimum GPU RAM (GiB; ~3% tolerance so "80GB" cards that report 79.6 GiB match)
  machine_id: null               # Pin to specific machine ID
  offer_type: bid                # "bid" or "on-demand"
  offer_limit: 20                # Max offers to consider
  price: 0.30                    # Starting bid price ($/hr)
  price_max: 0.80                # Max bid price
  price_step: 0.05               # Bid increment
  disk_gb: 50
  runtype: ssh                   # "ssh" or "jupyter"
  target_state: running
  recovery_policy: restart       # "restart", "ignore", or "destroy"
  image: pytorch/pytorch:latest  # pod image (fallback / stream-path base)
  base_image: ""                 # registry-mode: Dockerfile RUNTIME_BASE_IMAGE to build FROM;
                                 # pick a base cached on this provider's hosts for faster pulls.
                                 # Empty -> per-provider default (nvidia/cuda for vast).
  onstart: ""                    # Script to run on instance start
  env: {}                        # Environment variables
  pick_strategy: cheapest        # How to pick from offers
  bid_strategy: multiplier       # Bid strategy (see below)
  bid_multiplier: 1.1
  bid_percentile: 50
  reliability_min: 0.9
  dph_max: 1.0                   # Max $/hr
  dlperf_min: 0.0
  dlperf_per_dphtotal_min: 0.0
```

### `providers.runpod.machines[]`
```yaml
- name: a100
  gpu_type: "NVIDIA A100 80GB PCIe"
  gpu_count: 1
  cloud_tier: SECURE             # "SECURE" or "COMMUNITY"
  spot: false
  recovery_policy: restart
  image: runpod/pytorch:latest   # pod image (fallback / stream-path base)
  base_image: ""                 # registry-mode: Dockerfile RUNTIME_BASE_IMAGE to build FROM;
                                 # empty -> per-provider default (runpod/pytorch for runpod).
  template_id: null
  container_disk_size: 20        # GB
  volume_size: 50                # GB
  volume_path: /workspace
  network_volume_id: null
  min_memory_gb: 0
  min_vcpu: 0
  max_cost: 0.0                  # 0 = no limit
  ports: "22/tcp"
  ssh_public_key_files: []
  env_vars: {}
```

### `pulse`
```yaml
pulse:
  enabled: false
  interval_minutes: 5
  idle_stop: false
  idle_threshold_minutes: 15
  idle_gpu_threshold: 10         # % GPU utilization = idle
  max_restarts: 5
```

---

## Pulse Monitoring

Pulse is a cron-based health monitoring subsystem. When enabled (`--pulse` flag or `pulse.enabled: true`), it installs a cron job that runs every N minutes and checks job health.

### Decision Matrix

| Machine State | Job State | Action |
|---------------|-----------|--------|
| running | FINISHED | Auto-stop instance |
| running | FAILED/crashed | Soft restart (re-sync + relaunch) |
| running | RUNNING | GPU idle check → destroy+restart if too many strikes |
| stopped/exited | any | Hard restart (full pipeline, fresh instance) |
| api_error | any | Skip (try again next cycle) |

### Restart Strategies

- **Soft restart** — Re-sync code + relaunch on the same machine (fast)
- **Auto restart** — Full pipeline on a fresh new instance
- **Destroy and restart** — Destroy current instance + full pipeline (used after GPU idle strikes)
- **Idle stop** — Stop instance + remove cron job (job done, save money)

### Backoff

Restart backoff: `min(60 × 2^restart_count, 1800)` seconds (max 30 minutes).

### Audit Log

All Pulse decisions are logged to `.beam/logs/sessions/pulse_history_{session_id}.jsonl`.

---

## Bid Strategies (Vast.ai)

| Strategy | Selection | Bid Price | Best For |
|----------|-----------|-----------|----------|
| `multiplier` (default) | Cheapest offer | `min_bid × bid_multiplier` | General use |
| `cheapest` | Cheapest offer | `cfg.price` + increments | Legacy/manual |
| `best_value` | Best `dlperf/dph` | `min_bid × bid_multiplier` | Performance per $ |
| `percentile` | Cheapest offer | Nth percentile of min_bids | Percentile control |
| `score` | Best `dlperf×reliability/min_bid` | `min_bid × bid_multiplier` | Reliability focus |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `RUNPOD_API_KEY` | For RunPod | RunPod REST API key |
| `VAST_API_KEY` | For Vast.ai | Vast.ai API key |
| `GITHUB_USERNAME` | For registry mode | GitHub username for ghcr.io |
| `DOCKER_GHRC_TOKEN` or `GITHUB_TOKEN` | For registry mode | Token for ghcr.io push |
| `WANDB_API_KEY` | Optional | Weights & Biases API key |
| `HUGGING_FACE_HUB_TOKEN` | Optional | HuggingFace token |
| `BEAM_REMOTE_USER` | Optional | Override remote SSH user |
| `BEAM_REMOTE_PROJECT_DIR` | Optional | Override remote project directory |
| `FORCE_PUSH` | Optional | Force Docker image push even if unchanged |

---

## Filesystem Layout

### Local
```
./beam.yaml                                  # Config file
./.deps_hash                                 # MD5 hash of dep files
./.heavy-deps-hash                           # Hash for heavy deps
./.beam/logs/                                # All local beam logs
./.beam/logs/sessions/{id}.json              # Session state
./.beam/logs/.pulse_wrapper_{id}.sh          # Pulse cron wrapper script
./.beam/logs/pulse_cron.log                  # Pulse cron output log
./.beam/logs/sessions/pulse_history_{id}.jsonl  # Pulse audit log
```

### Remote
```
{remote.project_dir}/{project.name}/         # Code root
{remote_code_dir}/.venv/                     # Python venv
{remote_code_dir}/.run_session.sh            # Launch script
{remote.project_dir}/logs/{project.name}/    # Job logs dir
{remote_logs_dir}/{session_name}.log         # Training log
{remote_logs_dir}/.beam_state/RUNNING        # Sentinel: PID|timestamp
{remote_logs_dir}/.beam_state/FINISHED       # Sentinel: timestamp
{remote_logs_dir}/.beam_state/FAILED         # Sentinel: exit_code|timestamp
```

---

## See Also

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — Module map: which file to edit for any task
- [`src/beam/README.md`](./src/beam/README.md) — Core modules docs
- [`src/beam/providers/README.md`](./src/beam/providers/README.md) — Provider docs
- [`src/beam/steps/README.md`](./src/beam/steps/README.md) — Pipeline step docs
- [`examples/elemental-beam.yaml`](./examples/elemental-beam.yaml) — Full example config

---

## Shell Completion

Beam supports Tab completion for session IDs, provider names, and config names in bash, zsh, and fish.

**Setup (one-time):**

```bash
# Detect your shell automatically
beam --install-completion

# Or specify explicitly
beam --install-completion zsh
beam --install-completion bash
beam --install-completion fish
```

Then restart your shell (or `source ~/.zshrc` / `source ~/.bashrc`).

**What completes:**

| Command | Completes |
|---------|-----------|
| `beam attach <TAB>` | Session IDs |
| `beam logs <TAB>` | Session IDs |
| `beam ssh <TAB>` | Session IDs |
| `beam scp <TAB>` | Session IDs |
| `beam stop <TAB>` | Session IDs |
| `beam pulse install --session <TAB>` | Session IDs |
| `beam pulse uninstall --session <TAB>` | Session IDs |
| `beam pulse check --session <TAB>` | Session IDs |
| `beam run --provider <TAB>` | `runpod`, `vast.ai` |
| `beam offers --provider <TAB>` | `runpod`, `vast.ai` |
| `beam run --pod-config <TAB>` | Config names from `beam.yaml` |
| `beam offers --pod-config <TAB>` | Config names from `beam.yaml` |

Session IDs are read from `.beam/logs/sessions/` (filename-only, instant). No network calls during completion.
