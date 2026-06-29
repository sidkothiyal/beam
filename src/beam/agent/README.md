# `src/beam/agent/` — Pulse Agent Mode

LLM-powered diagnosis for Pulse. When enabled, "interesting" health states
(a failed/crashed job, a spot preemption, a down instance) are handed to an
**LLM tool-calling agent** that investigates — reads the remote logs, its own
restart history, instance/spot details, disk usage, GPU status, and wandb
metrics — and emits a recovery decision. The decision maps onto Pulse's
existing recovery actions.

The point: the deterministic monitor never reads logs, so it cannot tell a
transient failure (spot preemption, network blip → just restart) from a
deterministic one (code bug, NaN loss, full disk → stop and alert). Blindly
restarting a deterministically-broken job on billed GPUs burns money. The agent
makes that call, and **stops crash-loops** instead of restarting forever.

## Enabling it

Agent mode is **off by default** — Pulse behaves exactly as before. Turn it on
in `beam.yaml`:

```yaml
pulse:
  agent:
    enabled: true
    llm:
      provider: nim                 # nim (default) | openrouter | openai
      model: qwen/qwen3.5-397b-a17b
      api_key_env: NVIDIA_API_KEY   # names the secret; never put the key in yaml
    use_wandb: true
    wandb_entity: your-entity
    wandb_project: your-project
    allow_server_switch: true
    advisory: false                 # set true to log recommendations without acting
```

Then store the API key once (kept out of the repo — OS keyring if available,
else `~/.beam/agent.env`, chmod 600):

```bash
beam pulse agent set-key --provider nim       # prompts for the key
beam pulse agent test                         # verify the backend round-trips
```

No cron reinstall is needed — `beam pulse check` reads the config each tick.

## CLI

```bash
beam pulse agent set-key --provider nim    # store the LLM API key (keyring or ~/.beam/agent.env)
beam pulse agent test                      # one live LLM call to confirm wiring
beam pulse agent diagnose --session <id>   # one-shot diagnosis, prints verdict + tool trace, NO action
```

`diagnose` is the developer/debugging tool: it runs the full agent loop against a
real session and prints what it would decide and why, without touching anything.

## Backends

All three backends go through one [litellm](https://github.com/BerriAI/litellm)
OpenAI-compatible gateway, so the rest of the code never branches on provider:

| provider | base URL | default secret env |
|---|---|---|
| `nim` (default) | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `BEAM_OPENROUTER_KEY` |
| `openai` | (litellm default) | `BEAM_OPENAI_KEY` |

## Project memory (`BEAM_AGENT.md`)

Each project gets a committed, human-editable `BEAM_AGENT.md` at its repo root
(path configurable via `pulse.agent.project_doc`). Write operator knowledge in it
— expected behavior, known failure signatures and the right response, the wandb
project, normal loss ranges, GPU/cost constraints:

```markdown
# Beam Agent — Project Knowledge

- This job runs on Vast.ai spot and is outbid constantly. On any outbid: just restart.
- NaN loss ⇒ the LR schedule blew up; escalate, don't restart.
- Batch size > 64 OOMs on a single A40.

<!-- BEAM AGENT NOTES (auto-maintained — do not edit below) -->
<!-- END BEAM AGENT NOTES -->
```

The whole doc is fed to the agent at the **start of every pulse call** and it is
told to trust it. After a *successful* pulse call the agent may record a durable,
generalizable learning into its own marker-delimited section (the
`<!-- BEAM AGENT NOTES -->` block) — your hand-written content above the markers is
never touched. Set `project_doc: ""` to disable.

## Unified per-job log

Every job gets one local log, `.beam/logs/sessions/<session_id>.log`, capturing
**all `beam run` provisioning steps and every subsequent `beam pulse` check
(including restarts)** — one continuous timeline since the job started. The agent
reads it via the `read_job_log` tool, so it knows the whole story, not just the
current failure. (The log is carried forward across hard restarts.)

## Safety

- **Graceful fallback:** no key, `litellm` not installed, or any LLM error →
  Pulse silently uses its deterministic decision matrix. The monitor is never
  *less* reliable than before.
- **Caps enforced in code (not trusted to the model):** `pulse.max_retries`,
  `max_agent_restarts`, and a **crash-loop guard** (same `root_cause` repeated
  within `crash_loop_window` → forced `escalate`) bound the agent.
- **Advisory mode** (`advisory: true`): the agent logs its verdict to the
  history JSONL but takes no action — use it to build trust before going
  autonomous.

## Dependencies

Optional extra — base install stays lean:

```bash
uv pip install -e ".[agent]"     # litellm + keyring + wandb
```

## Tests

End-to-end tests live in `tests/` and mock the elemental training run (logs, GPU,
disk, provider/outbid, wandb, pulse history) via the `mock_elemental` fixture:

```bash
uv pip install -e ".[agent,test]"

# Offline: tool harness, decision->action dispatch, safety caps, run_check
# integration. LLM is scripted — no network.
pytest tests/test_pulse_agent_e2e.py -m "not live"

# Live: the real NIM model drives the tools and decides over mocked signals.
# Needs NVIDIA_API_KEY in the environment (auto-skips otherwise).
pytest tests/test_pulse_agent_live.py -m live
```

> If your shell sources an environment that injects pytest plugins (e.g. ROS on
> `PYTHONPATH`), prefix with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` — these tests need
> only core pytest.

## See also

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — module/tool/decision internals.
- [`../pulse.py`](../pulse.py) — where the agent branch slots into `run_check`.
