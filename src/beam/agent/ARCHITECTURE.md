# `src/beam/agent/` — Implementation Details

The agentic diagnosis layer for Pulse. Pure decision-making — it performs **no**
side effects; Pulse (`pulse.py`) executes the chosen action.

For **features and usage**, see [`README.md`](./README.md).

---

## Module map

| File | Responsibility |
|---|---|
| `__init__.py` | Lazy re-export of `diagnose_session` / `AgentVerdict` (no litellm import at load). |
| `secrets.py` | 3-tier secret resolver: `os.environ` → `~/.beam/agent.env` (chmod 600) → keyring (timeout-guarded). `BEAM_NO_KEYRING=1` disables the keyring tier. |
| `llm.py` | `AgentLLM` litellm gateway + `run_tool_loop` OpenAI tool-calling loop. |
| `prompts.py` | `SYSTEM_PROMPT` (cost-aware SRE role) + `build_observation_summary`. |
| `tools.py` | `ToolContext`, tool functions, `build_tool_schemas`, `make_dispatch`. |
| `diagnose.py` | `diagnose_session` orchestrator → `AgentVerdict`. Never raises. |

---

## Flow

```
pulse.run_check()  (state is an agent trigger and pulse.agent.enabled)
   └─ diagnose_session(sess, observations, logs_dir, cfg) -> AgentVerdict
        ├─ AgentLLM(cfg.pulse.agent.llm)             # resolve key; available()?
        ├─ ToolContext(sess, cfg, logs_dir)          # one ControlMaster SSH client
        ├─ build_tool_schemas(agent_cfg)             # honors tool toggles
        ├─ llm.run_tool_loop(system, user, tools, dispatch)
        │     repeat: completion(tools=...) → dispatch each tool_call → append result
        │     until the model calls submit_decision (terminal) or max_tool_iterations
        └─ _coerce_verdict(...) -> AgentVerdict(used_agent=True, ...)
   └─ pulse._dispatch_agent_decision(verdict, ...)   # maps decision -> do_* with caps
```

On **any** failure (no key, litellm missing, LLM/tool error, no decision in
budget) `diagnose_session` returns `AgentVerdict(used_agent=False, error=...)`
and `run_check` falls through to the deterministic matrix.

---

## `AgentLLM` (llm.py)

- `PROVIDER_BASE`: `{openrouter, nim}` → OpenAI-compatible base URLs.
- `_model_str()`: prepends `openai/` for `nim`/`openrouter`/`local` (litellm needs
  it with a custom `api_base`).
- `available()`: True if a key resolved (or provider is keyless `local`).
- `complete(system, user)`: single chat completion.
- `run_tool_loop(system, user, tools, dispatch, *, max_iterations, terminal_tool="submit_decision")`:
  drives `litellm.completion(tools=..., tool_choice="auto")`; echoes assistant
  tool-call turns back, appends `role:"tool"` results, accumulates token usage and
  a `tool_trace`. Returns `ToolLoopResult{decision, tool_trace, prompt_tokens,
  completion_tokens, iterations}`. Raises `LLMUnavailable` if litellm is missing
  or the loop ends without a decision.

Env knobs: `BEAM_LLM_TIMEOUT`, `BEAM_LLM_RETRIES`.

---

## Toolset (tools.py)

`ToolContext` carries `sess`, `cfg`, `logs_dir`, and a lazily-built, ControlMaster-
multiplexed `SSHClient` (so a whole investigation is ~one SSH handshake).
`make_dispatch(ctx)` returns `dispatch(name, args)` that isolates each tool in
try/except — a tool error becomes an `"ERROR: ..."` string the model reacts to,
never a crashed loop.

| tool | reads | failure mode it surfaces |
|---|---|---|
| `read_remote_log(tail_lines)` | `tail` of `{remote_logs_dir}/{tmux}.log` (capped at `log_max_bytes`) | any |
| `grep_remote_log(pattern, context, max_matches)` | `grep -nEi -C` (pattern shell-escaped) | OOM, traceback, NaN, disk-full |
| `read_job_log(tail_lines)` | unified `<id>.log` (all `beam run` + `beam pulse` steps since start) | full job timeline |
| `read_pulse_history(limit)` | `pulse_history_<id>.jsonl` + action/root_cause counts | **crash-loop** |
| `get_instance_details()` | machine_config spot/offer_type, dph, live `get_status`/`is_outbid` | spot preemption |
| `check_remote_disk(path)` | `df -h` + `du` of logs dir | disk-full |
| `get_gpu_status()` | `pulse.check_gpu_usage` | idle/hung |
| `query_wandb_run(metric, last_n)` | wandb Public API: state + recent values + NaN/Inf flag | training divergence |
| `list_provider_offers(max_results)` | `VastAIProvider.list_compatible_offers` | bad node → switch server |
| `submit_decision(...)` | **terminal** — ends the loop; not dispatched | — |

Schemas are gated by config: `read_logs`, `disk_check`, `use_wandb`,
`allow_server_switch`, `read_job_log`. `submit_decision` is always present.

## Project doc (`project_doc.py`)

`read_project_doc(repo_root, cfg)` loads `BEAM_AGENT.md` (path `pulse.agent.project_doc`,
capped at `project_doc_max_bytes`); `diagnose_session` injects it into the initial
user message via `build_observation_summary(obs, project_doc=...)`. The doc is
operator knowledge the agent is told to trust.

`submit_decision` carries an optional `project_doc_update` (markdown). It rides on
`AgentVerdict.project_doc_update` but **diagnose writes nothing** — `pulse.run_check`
calls `update_agent_section(repo_root, cfg, notes)` **only after the action
dispatched without error and not in advisory mode** (the success gate), setting
`AgentVerdict.project_doc_updated`. `update_agent_section` splices `notes` only
between the `AGENT_NOTES_START/END` markers (creating the file from a template, or
appending markers, as needed), so human-authored content is never overwritten.

## Unified per-job log

`log.py` gains `attach_job_log(path)`/`detach_job_log()` — a file sink mirroring
every emitter. `cli.run` attaches it for the whole `beam run` (temp → renamed to
`<id>.log` once the session id is minted), and `pulse.run_check` re-attaches
`sess["log_file"]` for each check (and the restart steps it triggers), so one file
holds the entire job timeline. `do_auto_restart` copies the old log into the new
session's log so a hard restart stays continuous. The agent reads it via
`read_job_log`.

---

## Decision → action mapping (executed in `pulse._dispatch_agent_decision`)

| `decision` | Pulse action |
|---|---|
| `soft_restart` | `do_soft_restart` |
| `hard_restart` | `_destroy_old_instance` + `do_auto_restart` |
| `outbid_restart` | `do_auto_restart` |
| `destroy_restart` | `do_destroy_and_restart` |
| `idle_stop` | `do_idle_stop` |
| `wait` | no-op (recorded only) |
| `escalate` | `do_escalate` (stop + marker + `escalate_command`) |
| `give_up` | `do_idle_stop` |
| `defer`/unknown | hand back to deterministic logic |

**Caps that override the model** (enforced in `_dispatch_agent_decision`):
1. `consecutive_failures >= pulse.max_retries` → `give_up`.
2. agent restarts (`.pulse_agent_restarts_<id>`) `>= max_agent_restarts` → `escalate`.
3. crash-loop guard: `_count_recent_root_cause` over `crash_loop_window` ≥
   `crash_loop_threshold` for the same `root_cause` → `escalate`.
4. `advisory=True` → log only, never act.

Restart-class actions run under `run_check`'s `_with_lock` to avoid concurrent
restarts.

---

## AgentVerdict (diagnose.py)

```python
@dataclass
class AgentVerdict:
    decision: str = "defer"     # one of the decision enum (validated/coerced)
    root_cause: str = ""        # short tag: spot_preempted, cuda_oom, nan_loss, ...
    reason: str = ""            # human explanation
    confidence: float = 0.0     # clamped to [0,1]
    used_agent: bool = False    # False => caller uses deterministic fallback
    tool_trace: list[dict]      # [{tool, args, summary}]
    prompt_tokens / completion_tokens: int
    error: str = ""             # set when used_agent is False
    project_doc_update: str = ""    # proposed BEAM_AGENT.md notes (written by pulse on success)
    project_doc_updated: bool = False
```

These fields are appended (additively) to each `pulse_history_<id>.jsonl` record
(`agent_used`, `decision`, `root_cause`, `reason`, `confidence`, `tool_calls`,
`tokens`, `agent_error`), so a later `read_pulse_history` feeds them back — the
loop that powers crash-loop detection.

---

## Risks / notes

- **Tool-calling support:** the NIM default `qwen/qwen3.5-397b-a17b` supports
  OpenAI tool-calling via litellm's `openai/` shim. If a backend rejects tools,
  the loop raises and Pulse falls back to deterministic. Validate with
  `beam pulse agent test`.
- **Prompt injection:** the agent reads job-controlled stdout/stderr. Defenses:
  the decision is a closed enum validated in code; all caps are enforced in the
  dispatcher; `escalate`/`give_up` only ever *stop* (never provision); the only
  shell hook is the operator-set `escalate_command`.
- **Cost:** healthy `running` and transient `api_error` never invoke the LLM.
  `log_max_bytes` / `max_tool_iterations` bound per-call spend; the restart caps
  bound total spend on a flapping instance.

---

## v1 scope

GPU-idle handling stays deterministic (the existing strikes mechanism). The agent
triggers on `failed`, `crashed`, `outbid`, `provider_down` (`pulse.agent.trigger_on`).
