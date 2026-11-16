# `src/beam/` — Implementation Details

Deep reference for all top-level beam modules. This covers classes, method signatures, data flow, and design decisions.

For **features and usage examples**, see [`README.md`](./README.md).

---

## `cli.py`

### App Structure

```python
app = typer.Typer()           # Main app
pulse_app = typer.Typer()     # Sub-app
app.add_typer(pulse_app, name="pulse")
```

### `beam run` — Full Flag Reference

```python
@app.command()
def run(
    command: list[str] = typer.Argument(None),
    ip: str = typer.Option(None, "--ip"),
    port: int = typer.Option(22, "-p", "--port"),
    identity_file: str = typer.Option(None, "-i"),
    user: str = typer.Option(None, "--user"),
    provider: str = typer.Option(None, "--provider"),        # "vastai" | "runpod"
    pod_id: str = typer.Option(None, "--pod-id"),
    pod_config: str = typer.Option(None, "--pod-config"),    # name from beam.yaml
    name: str = typer.Option("", "-n", "--name"),
    pulse: str = typer.Option(None, "--pulse"),                       # cron interval; falls back to pulse.interval
    pulse_idle_stop: bool = typer.Option(None, "--pulse-idle-stop/--no-pulse-idle-stop"),  # tri-state; None → pulse.idle_stop
    auto_stop_on_complete: bool = typer.Option(True, "--no-auto-stop-on-complete"),
    config: str = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "-v"),
    quiet: bool = typer.Option(False, "-q"),
    dry_run: bool = typer.Option(False, "--dry-run"),
)
```

**Routing logic:**
```
if not ip and not provider:
    run_local(command)          # os.execvp — replaces current process
elif ip:
    run_pipeline(cfg, ssh_info=SSHInfo(ip, port, user, identity_file), ...)
else:
    run_pipeline(cfg, provider=provider, pod_config=pod_config, ...)
```

### Other Commands

```python
@app.command()
def status(): ...               # list_sessions() → Rich table; SSHes into each remote by default (--offline to skip)

@app.command()
def init(): ...                 # write beam.yaml template to cwd

@app.command()
def offers(provider: str): ... # provider.list_compatible_offers() → table

@app.command()
def attach(session_id: str): ...# ssh -t ... tmux attach-session

@app.command()
def logs(session_id: str, follow: bool = False): ...  # tail remote log

@app.command()
def ssh(session_id: str, command: list[str] = None): ...  # exec ssh (interactive shell or one-off command)

@app.command()
def scp(session_id: str, source: str, dest: str, recursive: bool = True): ...  # exec scp; "remote:" prefix → instance

@app.command()
def stop(session_id: str): ... # pulse.do_destroy_stop() — provider.destroy_instance() + uninstall_pulse()
```

`attach`/`logs`/`ssh`/`scp` share `_ssh_cmd_prefix(sess, port_flag=...)` for the leading argv (`-p`/`-P`, `-i`, StrictHostKeyChecking).

### Pulse Sub-Commands

```python
@pulse_app.command("list")
def pulse_list(): ...           # crontab -l | grep beam_pulse

@pulse_app.command("install")
def pulse_install(session_id: str): ...

@pulse_app.command("uninstall")
def pulse_uninstall(session_id: str): ...

@pulse_app.command("check")
def pulse_check(session_id: str): ...  # run_check(session_id)
```

### Pulse Agent Sub-Commands (`pulse agent`)

```python
pulse_agent_app = typer.Typer()
pulse_app.add_typer(pulse_agent_app, name="agent")

@pulse_agent_app.command("set-key")
def pulse_agent_set_key(provider: str = "nim"): ...   # secrets.set_secret(env_name, key)

@pulse_agent_app.command("test")
def pulse_agent_test(): ...                            # one live AgentLLM.complete() round-trip

@pulse_agent_app.command("diagnose")
def pulse_agent_diagnose(session_id: str): ...         # diagnose_session() -> print verdict, no action
```

See [`agent/ARCHITECTURE.md`](./agent/ARCHITECTURE.md) for the agent internals.

---

## `config.py`

### Model Hierarchy

```python
class BeamConfig(BaseModel):
    project: ProjectConfig
    registry: RegistryConfig
    remote: RemoteConfig
    ssh: SSHConfig
    credentials: CredentialsConfig
    providers: ProvidersConfig
    pulse: PulseConfig
```

### `ProjectConfig`

```python
class ProjectConfig(BaseModel):
    name: str = "beam-project"
    repo_root: Path = Path(".")
```

### `RegistryConfig`

```python
class RegistryConfig(BaseModel):
    image: str | None = None    # e.g. "ghcr.io/user/beam-env:latest"
```

### `RemoteConfig`

```python
class RemoteConfig(BaseModel):
    user: str = "root"          # env: BEAM_REMOTE_USER
    project_dir: str = "/workspace"  # env: BEAM_REMOTE_PROJECT_DIR
```

### `SSHConfig`

```python
class SSHConfig(BaseModel):
    identity_file: str | None = None
    port: int = 22
```

### `CredentialsConfig`

```python
class CredentialsConfig(BaseModel):
    wandb_api_key: str = ""
    hugging_face_hub_token: str = ""
    kaggle_json_path: str | None = None
```

### `ProvidersConfig`

```python
class ProvidersConfig(BaseModel):
    vastai: VastAIProviderConfig = VastAIProviderConfig()
    runpod: RunPodProviderConfig = RunPodProviderConfig()
```

### `RunPodProviderConfig` / `RunPodMachineConfig`

```python
class RunPodProviderConfig(BaseModel):
    machines: list[RunPodMachineConfig] = []

class RunPodMachineConfig(BaseModel):
    name: str = "default"
    gpu_type: str = "NVIDIA A100 80GB PCIe"
    gpu_count: int = 1
    cloud_tier: str = "SECURE"          # "SECURE" | "COMMUNITY"
    spot: bool = False
    recovery_policy: str = "restart"    # "restart" | "ignore" | "destroy"
    image: str = "runpod/pytorch:latest"
    template_id: str | None = None
    container_disk_size: int = 20       # GB
    volume_size: int = 50               # GB
    volume_path: str = "/workspace"
    network_volume_id: str | None = None
    min_memory_gb: int = 0
    min_vcpu: int = 0
    max_cost: float = 0.0               # 0 = no limit
    ports: str = "22/tcp"
    ssh_public_key_files: list[str] = []
    env_vars: dict[str, str] = {}
```

### `VastAIProviderConfig` / `VastAIMachineConfig`

```python
class VastAIProviderConfig(BaseModel):
    machines: list[VastAIMachineConfig] = []

class VastAIMachineConfig(BaseModel):
    name: str = "default"
    instance_label: str = ""
    gpu_name: str = "RTX_4090"
    num_gpus: int = 1
    num_gpus_max: int | None = None
    gpu_ram_min: int = 0                # GB
    machine_id: int | None = None       # Pin to specific machine
    offer_type: str = "bid"             # "bid" | "on-demand"
    offer_limit: int = 20               # Max offers to consider
    price: float = 0.30                 # Starting price ($/hr)
    price_max: float = 0.80
    price_step: float = 0.05
    disk_gb: int = 50
    runtype: str = "ssh"                # "ssh" | "jupyter"
    target_state: str = "running"
    recovery_policy: str = "restart"
    image: str = "pytorch/pytorch:latest"
    onstart: str = ""
    env: dict[str, str] = {}
    pick_strategy: str = "cheapest"
    bid_strategy: str = "multiplier"    # see bid strategies
    bid_multiplier: float = 1.1
    bid_percentile: int = 50
    avoid_owned_machines: bool = True   # skip hosts you already occupy (no self-outbid)
    reliability_min: float = 0.0
    dph_max: float = 0.0                # 0 = no limit
    dlperf_min: float = 0.0
    dlperf_per_dphtotal_min: float = 0.0
```

### `PulseConfig`

```python
class PulseConfig(BaseModel):
    interval: str = ""                  # default cron interval (e.g. "5m"); fallback for --pulse
    idle_stop: bool = False             # stop on GPU idle; fallback for --pulse-idle-stop
    gpu_idle_threshold: int = 5         # % utilization = idle
    gpu_idle_max_strikes: int = 3
    gpu_idle_samples: int = 5
    gpu_idle_sample_interval: int = 2
    max_retries: int = 5
    auto_stop_on_complete: bool = True
    agent: PulseAgentConfig             # agent-mode config (see below)
```

`beam run` resolves `--pulse`/`--pulse-idle-stop` against these: an explicit CLI
flag wins, otherwise the config value is used (both flags are tri-state / optional).

### `AgentLLMConfig` / `PulseAgentConfig`

Agent-mode config nested under `pulse.agent`. `enabled=False` (default) keeps
today's deterministic behavior.

```python
class AgentLLMConfig(BaseModel):
    provider: str = "nim"               # nim | openrouter | openai
    model: str = "qwen/qwen3.5-397b-a17b"
    base_url: str | None = None
    api_key_env: str = "NVIDIA_API_KEY"  # names the secret, never the key
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: int = 90
    retries: int = 3
    max_tool_iterations: int = 8

class PulseAgentConfig(BaseModel):
    enabled: bool = False
    llm: AgentLLMConfig
    trigger_on: list[str] = ["failed", "crashed", "outbid", "provider_down"]
    read_logs: bool = True; log_tail_lines: int = 400; log_max_bytes: int = 65536
    disk_check: bool = True; history_lookback: int = 50
    use_wandb: bool = True; wandb_entity: str = ""; wandb_project: str = ""
    allow_server_switch: bool = True
    read_job_log: bool = True; job_log_tail_lines: int = 300   # unified per-job log tool
    project_doc: str = "BEAM_AGENT.md"; project_doc_max_bytes: int = 8192
    advisory: bool = False              # dry-run: log verdict, take no action
    max_agent_restarts: int = 8
    crash_loop_window: int = 3600; crash_loop_threshold: int = 4
    escalate_command: str = ""          # optional notify hook on escalate
```

### `load_config()`

```python
def load_config(
    config_path: str | None = None,
    overrides: dict[str, str] | None = None,
) -> BeamConfig
```

1. Find `beam.yaml`: use `config_path` if given, else walk up from cwd
2. Parse YAML → dict
3. Apply env vars: overwrite `remote.user` from `BEAM_REMOTE_USER`, `remote.project_dir` from `BEAM_REMOTE_PROJECT_DIR`
4. Apply `overrides` (flat dotted-path dict: `{"remote.user": "ubuntu"}`)
5. Return `BeamConfig(**merged_dict)`

---

## `log.py`

### Functions

```python
def set_verbosity(verbose: bool, quiet: bool) -> None

def info(msg: str) -> None          # Blue, suppressed in quiet
def success(msg: str) -> None       # Green, suppressed in quiet
def warn(msg: str) -> None          # Yellow, always shown
def error(msg: str) -> None         # Red "Error: {msg}", always shown
def step(msg: str) -> None          # Yellow step indicator, suppressed in quiet
def done(msg: str) -> None          # Green checkmark, suppressed in quiet
def debug(msg: str) -> None         # Dim, only in verbose mode

def banner(step_num: int, total: int, title: str) -> None
# Prints: ━━━ [3/7] Sync Code ━━━

def final_banner(msg: str | None = None) -> None
# Prints success banner, optionally with message

def kv(key: str, value: str) -> None
# Prints: "  key: value" (indented, key in bold)

# Per-job unified log sink (one timeline for beam run + every beam pulse check):
def attach_job_log(path: Path | str) -> None   # mirror all emitters to a file (append)
def detach_job_log() -> None                    # stop mirroring, close handle
```

### Design

Uses `rich.console.Console` with markup. Verbosity is global module-level state set once via `set_verbosity()` at CLI startup. When a job log is attached, every emitter ALSO appends a timestamped line to that file (regardless of quiet/verbose) so the file is a complete record. `cli.run` attaches it for `beam run`; `pulse.run_check` re-attaches `<id>.log` for each check.

---

## `pipeline.py`

### `RunContext` — Pipeline State

```python
@dataclass
class RunContext:
    # Config
    cfg: BeamConfig
    repo_root: Path
    command: list[str]
    name: str
    dry_run: bool
    verbose: bool
    quiet: bool

    # SSH (set after ensure_machine)
    ssh_info: SSHInfo | None = None
    ssh_client: SSHClient | None = None

    # Provider (set after ensure_machine)
    pod_id: str | None = None
    provider_name: str | None = None
    dph_total: float | None = None

    # Build (set after build_venv)
    build_result: BuildResult | None = None
    ghcr_image: str | None = None

    # Session (set after run_remote)
    session: Session | None = None

    # Derived paths (properties)
    @property
    def remote_code_dir(self) -> str:
        return f"{self.cfg.remote.project_dir}/{self.cfg.project.name}"

    @property
    def remote_venv_dir(self) -> str:
        return f"{self.remote_code_dir}/.venv"

    @property
    def remote_logs_dir(self) -> str:
        return f"{self.cfg.remote.project_dir}/logs/{self.cfg.project.name}"

    @property
    def logs_dir(self) -> Path:
        return self.repo_root / ".beam" / "logs"
```

### `run_pipeline()`

```python
def run_pipeline(
    cfg: BeamConfig,
    command: list[str],
    name: str = "",
    ssh_info: SSHInfo | None = None,    # Existing machine
    provider: str | None = None,         # Provider name for provisioning
    pod_id: str | None = None,           # Reconnect to existing pod
    pod_config: str | None = None,       # Machine config name
    pulse: bool = False,
    pulse_idle_stop: bool = False,
    auto_stop_on_complete: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
) -> None
```

**Pipeline variant selection:**
```python
use_registry = bool(cfg.registry.image and os.environ.get("GITHUB_USERNAME"))
```

**With registry** (7 steps):
```
build_venv → push_image → ensure_machine → sync_venv → sync_code → setup_creds → run_remote
```

**Without registry** (7 steps):
```
ensure_machine → build_venv → sync_venv → sync_code → setup_creds → setup_uv → run_remote
```

---

## `pulse.py`

### Public Functions

```python
def install_pulse(session_id: str, interval_minutes: int = 5) -> None
# Writes .pulse_wrapper_{id}.sh, installs cron job

def uninstall_pulse(session_id: str) -> None
# Removes cron entry with tag # beam_pulse_{session_id}

def run_check(session_id: str) -> None
# Main health-check entry point

def list_pulse_sessions() -> list[str]
# Returns session IDs with active cron jobs
```

### `run_check()` Decision Matrix

```python
# Pseudo-code of decision logic:
machine_status = provider.get_status(pod_id)
job_state = read_sentinel_files(ssh_client, remote_state_dir)
# job_state: "running" | "finished" | "failed" | "crashed" | "unknown"

if machine_status == "api_error":
    log_and_skip()

elif machine_status in ("stopped", "exited"):
    do_auto_restart()           # full pipeline on fresh instance

elif machine_status == "running":
    if job_state == "finished":
        do_auto_stop()          # stop instance, remove cron

    elif job_state in ("failed", "crashed"):
        do_soft_restart()       # re-sync + relaunch same machine

    elif job_state == "running":
        idle_strikes = check_gpu_usage(ssh_client)
        if idle_strikes >= threshold:
            do_destroy_and_restart()
        # else: healthy, do nothing
```

### Restart Strategies

```python
def do_soft_restart(ctx: PulseContext) -> None:
    # sync_code() + run_remote() on existing SSH connection
    # Fast: no reprovisioning

def do_auto_restart(ctx: PulseContext) -> None:
    # Full run_pipeline() on a fresh instance
    # Used when machine is stopped/exited

def do_destroy_and_restart(ctx: PulseContext) -> None:
    # provider.destroy_instance() + full run_pipeline()
    # Used after GPU idle strikes

def do_idle_stop(ctx: PulseContext) -> None:
    # _teardown_instance(destroy=False): provider.stop_instance() + uninstall_pulse()
    # Used when job finished / GPU idle / escalate teardown

def do_destroy_stop(ctx: PulseContext) -> None:
    # _teardown_instance(destroy=True): provider.destroy_instance() + uninstall_pulse()
    # Used by `beam stop` — permanently tears down (no lingering storage cost)
```

### Backoff

```python
wait_seconds = min(60 * (2 ** restart_count), 1800)
# Stored in: .beam/logs/.pulse_restart_count_{id}
# Timestamp:  .beam/logs/.pulse_restart_ts_{id}
```

Files managed by Pulse:
- `.beam/logs/.pulse_failures_{id}` — consecutive failure count
- `.beam/logs/.pulse_idle_{id}` — GPU idle strike count
- `.beam/logs/.pulse_restart_count_{id}` — restart count (for backoff)
- `.beam/logs/.pulse_restart_ts_{id}` — last restart timestamp

### GPU Idle Check

```python
def check_gpu_usage(ssh_client: SSHClient) -> float:
    # Step 1: nvidia-smi --query-compute-apps=pid
    #   If any PIDs → return 100.0 (GPU in use)
    # Step 2: N samples of nvidia-smi --query-gpu=utilization.gpu
    #   Return average utilization
```

### Sentinel Files

Read from `{remote_logs_dir}/.beam_state/`:
- `RUNNING` → content: `{pid}|{timestamp}`
- `FINISHED` → content: `{timestamp}`
- `FAILED` → content: `{exit_code}|{timestamp}`

### Audit Log

```python
# Appended to: .beam/logs/sessions/pulse_history_{session_id}.jsonl
{
    "timestamp": "2024-01-01T12:00:00Z",
    "machine_status": "running",
    "job_state": "running",
    "action": "none",
    "restart_count": 0,
    "idle_strikes": 0,
    "notes": "..."
}
```

### Cron Job

```bash
# Cron entry format:
*/5 * * * * /path/to/.beam/logs/.pulse_wrapper_{id}.sh >> /path/to/.beam/logs/pulse_cron.log 2>&1  # beam_pulse_{session_id}

# Wrapper script sets up PATH and calls:
beam pulse check {session_id}
```

### Agent Mode (optional)

When `pulse.agent.enabled`, `run_check()` routes "interesting" states to an LLM
tool-calling agent *before* the deterministic matrix; on any agent failure it
falls through to the matrix (never less reliable).

```python
trigger = _classify_trigger(instance_status, job_state)
# "" (skip) | "failed" | "crashed" | "outbid" | "provider_down"
if agent_cfg.enabled and auto_restart and trigger in agent_cfg.trigger_on:
    verdict = beam.agent.diagnose.diagnose_session(sess, observations, logs_dir, cfg)
if verdict and verdict.used_agent:
    action = _dispatch_agent_decision(verdict, ...)   # maps decision -> do_*
else:
    ... deterministic matrix ...
```

Key functions added to `pulse.py`:
- `_classify_trigger(instance_status, job_state)` — trigger token or "".
- `_agent_observations(...)` — cheap context handed to the agent.
- `_dispatch_agent_decision(...)` — maps the verdict's `decision` to `do_*`,
  enforcing `max_retries`, `max_agent_restarts` (`.pulse_agent_restarts_<id>`),
  and the crash-loop guard (`_count_recent_root_cause`).
- `do_escalate(sess, session_id, logs_dir, verdict, cfg)` — stop + write
  `pulse_escalation_<id>.txt` + run `escalate_command`. The crash-loop fix.

History records gain `agent_used`, `decision`, `root_cause`, `reason`,
`confidence`, `tool_calls`, `tokens`, `project_doc_updated` when the agent ran.

`run_check` also attaches the **unified per-job log** (`sess["log_file"]` =
`.beam/logs/sessions/<id>.log`) so every check + restart appends to the same
timeline started by `beam run`; and after a *successful* (non-advisory) agent
dispatch it writes `verdict.project_doc_update` into `BEAM_AGENT.md`'s
agent-managed section via `beam.agent.project_doc.update_agent_section`. See
[`agent/ARCHITECTURE.md`](./agent/ARCHITECTURE.md).

---

## `session.py`

### Session Fields

```python
@dataclass
class Session:
    session_id: str                 # UUID
    provider: str | None            # "vastai" | "runpod" | None
    pod_id: str | None
    ip: str
    port: int
    user: str
    identity_file: str | None
    remote_project_dir: str
    remote_home: str
    command: list[str]
    pod_config_name: str | None
    recovery_policy: str            # "restart" | "ignore" | "destroy"
    ghcr_image: str | None
    remote_state_dir: str           # {remote_logs_dir}/.beam_state
    remote_logs_dir: str
    remote_code_dir: str
    tmux_session: str               # tmux session name
    created_at: str                 # ISO 8601
    # API keys (stored for pulse reconnection)
    runpod_api_key: str | None
    vast_api_key: str | None
    wandb_api_key: str | None
    hf_token: str | None
    github_username: str | None
    extra: dict                     # Arbitrary additional data
```

### Functions

```python
def save_session(session: Session, project_root: Path) -> Path
# Writes to {project_root}/.beam/logs/sessions/{session_id}.json

def load_session(session_id: str, project_root: Path | None = None) -> Session
# Reads from sessions dir, raises if not found

def list_sessions(project_root: Path | None = None) -> list[Session]
# Returns all sessions sorted by created_at descending

def find_session(query: str, project_root: Path | None = None) -> Session
# Match by full ID, prefix, or session name. Raises if not found or ambiguous.

def delete_session(session_id: str, project_root: Path | None = None) -> None
# Removes session JSON file
```

---

## `ssh.py`

### `SSHInfo`

```python
@dataclass
class SSHInfo:
    host: str
    port: int = 22
    user: str = "root"
    identity_file: str | None = None

    @property
    def remote_host(self) -> str:
        return f"{self.user}@{self.host}"
```

### `SSHClient`

```python
class SSHClient:
    def __init__(self, info: SSHInfo) -> None

    def run(
        self,
        command: str,
        check: bool = True,         # Raise on non-zero exit
        env: dict | None = None,
    ) -> subprocess.CompletedProcess

    def check(self, command: str) -> bool
    # Returns True if command exits 0, False otherwise

    def run_stream(
        self,
        command: str,
        env: dict | None = None,
    ) -> None
    # Streams stdout/stderr in real-time (for long-running commands)

    def pipe_to_remote(
        self,
        local_command: str,         # Runs locally, pipes stdout to remote
        remote_command: str,
    ) -> None
    # e.g. pipe_to_remote("docker cp ... | gzip", "gunzip | tar -x ...")

    def upload_stdin(
        self,
        content: str,
        remote_path: str,
    ) -> None
    # Write string content to remote file via stdin

    def rsync(
        self,
        local_path: str,
        remote_path: str,
        extra_args: list[str] | None = None,
        exclude: list[str] | None = None,
        delete: bool = False,
    ) -> None

    def wait_for_ssh(
        self,
        timeout: int = 120,
        interval: int = 5,
    ) -> None
    # Polls until SSH accepts connections or timeout

    def remove_from_known_hosts(self) -> None
    # Removes host from ~/.ssh/known_hosts (for fresh instances)

    def close_control_master(self) -> None
    # Sends -O exit to ControlMaster socket
```

### ControlMaster Configuration

SSH args used for all connections:
```
-o ControlMaster=auto
-o ControlPath=/tmp/beam_cm_{host}_{port}
-o ControlPersist=120
-o StrictHostKeyChecking=no
-o UserKnownHostsFile=/dev/null
-o ConnectTimeout=30
```

The ControlMaster socket is created on first connection and reused for 120 seconds after the last connection closes. `close_control_master()` tears it down immediately (called at end of pipeline).

---

## See Also

- **[`README.md`](./README.md)** — Features & usage examples
- **[`agent/ARCHITECTURE.md`](./agent/ARCHITECTURE.md)** — Pulse agent-mode internals
- **[`providers/ARCHITECTURE.md`](./providers/ARCHITECTURE.md)** — Provider implementation details
- **[`steps/ARCHITECTURE.md`](./steps/ARCHITECTURE.md)** — Step implementation details

---

## `complete.py` — Shell Completion Callbacks

### Purpose
Provides lightweight Tab-completion callbacks consumed by Typer's `autocompletion=` parameter. Runs inside the shell process on Tab-press — must be fast, no network calls.

### Callback Signature Convention (Typer 0.24+)
Typer inspects callback signatures and matches parameters by type annotation or name:
- `incomplete: str` — the partial string typed so far (required)
- `ctx: click.Context` — optional, injected if annotated as `click.Context`
- `args: list[str]` — optional, injected if annotated as a list

Our callbacks only need `incomplete: str`.

### Functions

```python
def complete_session_id(incomplete: str) -> list[str]
```
Walks up from `cwd` to find `.beam/logs/`, then globs `sessions/*.json`.
Returns `p.stem` values sorted by `mtime` (newest first), filtered by prefix.
Excludes `pulse_history_*` files. No JSON parsing — instant.

```python
def complete_provider(incomplete: str) -> list[str]
```
Static list `["runpod", "vast.ai"]` filtered by prefix.

```python
def complete_pod_config(incomplete: str) -> list[str]
```
Calls `load_config()` (reads beam.yaml). Returns all `name` fields from
`providers.runpod.configs` and `providers.vastai.configs` filtered by prefix.
Wrapped in `try/except` — returns `[]` on any error.

### `_find_logs_dir() -> Path | None`
Internal helper. First tries `find_config_file()` to locate beam.yaml and
derives `.beam/logs/` from its parent. Falls back to walking up from `cwd`
looking for `.beam/logs/` directly (up to 10 levels).

### Wired into cli.py
```python
# Arguments
session_id: str = typer.Argument(..., autocompletion=complete.complete_session_id)

# Options
provider: str = typer.Option(..., autocompletion=complete.complete_provider)
pod_config: str = typer.Option(..., autocompletion=complete.complete_pod_config)
```
Applied to: `attach`, `logs`, `ssh`, `scp`, `stop` (session_id argument); `pulse install/uninstall/check` (--session option); `run`, `offers` (--provider, --pod-config options).
