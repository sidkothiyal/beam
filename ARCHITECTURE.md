# beam — Architecture Overview

**Navigation guide: find which file to edit for any task.**

---

## What It Is

Beam is a CLI tool that provisions cloud GPU instances (RunPod, Vast.ai), builds and transfers Python virtual environments, syncs code via rsync, sets up credentials, and launches training jobs in tmux sessions. A Pulse subsystem provides cron-based health monitoring with auto-restart, GPU idle detection, and cost tracking. The codebase is organized as a Typer CLI over a 7-step pipeline, with Pydantic v2 config, provider abstractions, and per-step modules.

---

## Directory Tree

```
beam/
├── pyproject.toml              # Package metadata, deps, entry point (beam-deploy 0.1.0)
├── beam.yaml                   # Config file (project-local, found by walking up dirs)
├── examples/
│   └── elemental-beam.yaml     # Full annotated example config
├── README.md                   # GitHub project overview (you are here: ../README.md)
├── ARCHITECTURE.md             # This file — module navigation map
├── CLAUDE.md                   # AI assistant context
├── .cursor/rules/              # Cursor editor rules
│   ├── main.mdc                # Always-on core patterns
│   ├── providers.mdc           # Provider-specific rules
│   └── steps.mdc               # Pipeline step rules
└── src/beam/
    ├── __init__.py             # Package version (__version__)
    ├── cli.py                  # Typer CLI: all commands + pulse sub-app
    ├── config.py               # Pydantic v2 config hierarchy + YAML loader
    ├── log.py                  # Rich terminal logging utilities
    ├── pipeline.py             # 7-step deploy pipeline orchestrator + RunContext
    ├── pulse.py                # Cron-based health monitoring + auto-restart
    ├── session.py              # Session state: read/write JSON session files
    ├── ssh.py                  # SSH/rsync subprocess wrappers + ControlMaster
    ├── providers/
    │   ├── __init__.py
    │   ├── base.py             # ProviderBase ABC (7-method interface)
    │   ├── runpod.py           # RunPod REST API client
    │   └── vastai.py           # Vast.ai REST API client + 5 bid strategies
    └── steps/
        ├── __init__.py
        ├── build_venv.py       # Build Python venv in Docker, MD5-hash dep files
        ├── push_image.py       # Push Docker image to ghcr.io
        ├── ensure_machine.py   # Provision or recover remote machine
        ├── sync_venv.py        # Transfer/link .venv on remote
        ├── sync_code.py        # rsync code to remote (respects .gitignore)
        ├── setup_creds.py      # Copy credentials to remote .bashrc
        ├── setup_uv.py         # Install uv on remote if missing
        └── run_remote.py       # Write launch script + start tmux session
```

---

## Module → Feature Lookup

### `cli.py`
Entry point for all user-facing commands. Defines the Typer app with commands `run`, `status`, `init`, `offers`, `attach`, `logs`, `stop`, and the `pulse` sub-app (`list`, `install`, `uninstall`, `check`). Routes `beam run` to local execution, existing-machine pipeline, or provision+pipeline based on flags.

**Edit when:** Adding a new CLI command, changing flag names, modifying execution routing logic.

### `config.py`
Pydantic v2 model hierarchy for all beam configuration. Loads `beam.yaml` by walking up from cwd, applies env var overrides (`BEAM_REMOTE_USER`, `BEAM_REMOTE_PROJECT_DIR`), then CLI overrides (flat dotted-path dict). Contains all machine config fields for both RunPod and Vast.ai.

**Edit when:** Adding new config fields, changing defaults, adding a new provider's config model, modifying config resolution order.

### `log.py`
Rich-based terminal logging. Provides `info`, `success`, `warn`, `error`, `step`, `done`, `debug`, `banner`, `final_banner`, `kv`. Respects `set_verbosity(verbose, quiet)` — debug only shows in verbose, info/success/step/done suppressed in quiet mode.

**Edit when:** Changing log formatting, adding new log levels, modifying verbosity behavior.

### `pipeline.py`
Orchestrates the 7-step deploy pipeline. `RunContext` is the central state object passed to all steps. Derives remote paths, decides pipeline variant (registry venv vs. direct sync), and calls steps in order. Contains the `run_pipeline()` entry point called by `cli.py`.

**Edit when:** Changing pipeline ordering, adding a new step to the pipeline, modifying RunContext fields or derived paths.

### `pulse.py`
Cron-based health monitoring subsystem. Installs/uninstalls cron jobs, runs health checks (`run_check()`), reads sentinel files from remote, implements the decision matrix, and executes restart strategies. Manages backoff files, idle-strike counters, and the JSONL audit log.

**Edit when:** Changing health-check logic, adding restart strategies, modifying backoff behavior, changing GPU idle detection.

### `session.py`
Reads and writes JSON session files at `.beam/logs/sessions/{id}.json`. A session captures everything needed to reconnect, restart, or monitor a job: SSH info, provider details, remote paths, API keys, tmux session name.

**Edit when:** Adding new fields to session state, changing session file format, adding session query functions.

### `ssh.py`
Low-level SSH/rsync subprocess wrappers. `SSHInfo` holds connection details. `SSHClient` wraps all remote operations with ControlMaster multiplexing (ControlPersist=120s) for connection reuse. All remote commands go through this module.

**Edit when:** Changing SSH options, adding new remote operation helpers, modifying rsync behavior.

### `providers/base.py`
`ProviderBase` abstract base class defining the 7-method provider interface: `create_instance`, `start_instance`, `stop_instance`, `destroy_instance`, `get_status`, `get_ssh_info`, `wait_ready`. Normalised status values: `running`, `stopped`, `exited`, `unknown`, `error`, `api_error`.

**Edit when:** Adding a method to the provider interface (requires updating all providers).

### `providers/runpod.py`
RunPod REST API client (`https://rest.runpod.io/v1`). Implements `ProviderBase`. Handles pod creation, GPU type listing, spot reclamation detection, and loading-aware `wait_ready` (300s → 600s when pulling image).

**Edit when:** Fixing RunPod API issues, changing pod creation parameters, updating status detection.

### `providers/vastai.py`
Vast.ai REST API client (`https://console.vast.ai/api/v0`). Implements `ProviderBase` with 5 bid strategies, outbid detection, progressive bidding, and loading-aware `wait_ready`. Contains `OutbidError` and `ResolvedOffer` types.

**Edit when:** Fixing Vast.ai API issues, adding/changing bid strategies, modifying offer filtering.

### `steps/build_venv.py`
Builds the Python venv in Docker locally. MD5-hashes dependency files against cached `.deps_hash`; only rebuilds if changed. Returns `BuildResult(deps_changed, current_hash)`.

**Edit when:** Changing venv build process, modifying dep-file hashing, updating Docker build args.

### `steps/push_image.py`
Logs in to ghcr.io and pushes the built Docker image. Used in registry-venv mode when `GITHUB_USERNAME` is set.

**Edit when:** Changing registry target, modifying push logic.

### `steps/ensure_machine.py`
Provisions or recovers a remote machine. Returns `MachineResult(ssh_info, pod_id, provider_name, dph_total)`. Handles IP path (existing machine check + recovery) and provider path (create with fallback ordering, 15s between attempts, Vast.ai outbid retry up to 3 times).

**Edit when:** Changing provisioning logic, modifying fallback behavior, adding provider fallback ordering.

### `steps/sync_venv.py`
Transfers the venv to the remote. Registry mode: symlinks `/app/.venv`. Stream mode: `docker cp` → gzip → SSH pipe, checks `.deps_hash` before transfer, repairs interpreter symlinks after.

**Edit when:** Changing venv transfer method, fixing symlink repair, modifying registry-mode behavior.

### `steps/sync_code.py`
rsyncs local code to remote `{remote_code_dir}`. Builds exclude list from hardcoded patterns + `git ls-files --others --ignored`. Uses `--delete` to remove stale files.

**Edit when:** Changing rsync options, modifying exclusion patterns.

### `steps/setup_creds.py`
Copies `kaggle.json` to remote and writes API key env vars (`WANDB_API_KEY`, `HUGGING_FACE_HUB_TOKEN`) to remote `.bashrc`.

**Edit when:** Adding new credential types, changing how env vars are written.

### `steps/setup_uv.py`
Installs `uv` on the remote if not already present. Adds `UV_LINK_MODE=copy` to remote `.bashrc`.

**Edit when:** Changing uv installation method or version.

### `steps/run_remote.py`
Writes `.run_session.sh` to the remote (sets PATH, env vars, torch headers, sentinel file logic, tee to log file) and launches it in a new tmux session. Returns `RemoteSession` or `None` on dry-run.

**Edit when:** Changing launch script contents, modifying sentinel file behavior, changing tmux session options.

---

## Key Design Patterns

1. **Pydantic config hierarchy** — All config flows through `BeamConfig`. Resolution order: defaults → YAML → env vars → CLI overrides. No global mutable state for config.

2. **RunContext as pipeline state** — A single `RunContext` dataclass is built once and passed to every step. Derived paths (remote_code_dir, remote_logs_dir, etc.) are computed as properties.

3. **Provider abstraction** — All cloud operations go through `ProviderBase`. Adding a provider means implementing 7 methods and registering in `cli.py`/`pipeline.py`.

4. **Step functions, not classes** — Pipeline steps are plain functions (not classes), called by `pipeline.py` in sequence. Each step takes `RunContext` or specific args from it.

5. **Session files as truth** — Running job state lives in `.beam/logs/sessions/{id}.json`. Pulse, `beam status`, `beam attach`, and `beam stop` all read from these files.

6. **ControlMaster SSH multiplexing** — All SSH operations reuse a single connection per host (ControlPersist=120s), avoiding repeated authentication overhead.

7. **Sentinel files for job state** — Remote job state is communicated via files (`RUNNING`, `FINISHED`, `FAILED`) rather than polling process state, making it robust to reconnection.

---

## Common Modification Scenarios

| Task | Files to Edit |
|------|--------------|
| Add a CLI flag to `beam run` | `cli.py`, `config.py` (if config-backed) |
| Add a new config field | `config.py` |
| Add a new pipeline step | `steps/<new_step>.py`, `pipeline.py` |
| Add a new cloud provider | `providers/<name>.py`, `providers/base.py` (if interface changes), `cli.py`, `pipeline.py` |
| Change how the venv is built | `steps/build_venv.py` |
| Change Pulse restart logic | `pulse.py` |
| Add a new Vast.ai bid strategy | `providers/vastai.py` |
| Change session file schema | `session.py` |
| Change SSH options | `ssh.py` |
| Change log formatting | `log.py` |

---

## Further Reading

- **[`src/beam/README.md`](./src/beam/README.md)** — Features & usage for all core modules
- **[`src/beam/ARCHITECTURE.md`](./src/beam/ARCHITECTURE.md)** — Deep implementation details (classes, methods, data flow)
- **[`src/beam/providers/README.md`](./src/beam/providers/README.md)** — Provider features & usage
- **[`src/beam/providers/ARCHITECTURE.md`](./src/beam/providers/ARCHITECTURE.md)** — Provider implementation details
- **[`src/beam/steps/README.md`](./src/beam/steps/README.md)** — Step features & usage
- **[`src/beam/steps/ARCHITECTURE.md`](./src/beam/steps/ARCHITECTURE.md)** — Step implementation details
