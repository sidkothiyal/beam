# CLAUDE.md — beam

**beam**: CLI for remote GPU training deployment. Provisions RunPod/Vast.ai instances, syncs venv+code, launches tmux jobs with Pulse monitoring.

## Tooling

- **Python env**: `uv` (defined in `pyproject.toml`). Entry point: `beam` CLI (Typer).
- **No pre-commit hooks** — manually lint before committing.

## Commands

```bash
uv pip install -e .                                    # install
beam run --provider vastai python train.py             # deploy
beam run --ip 1.2.3.4 python train.py                 # existing machine
beam status                                            # show sessions
beam attach <id>                                       # attach tmux
beam stop <id>                                         # stop instance
```

## Non-Obvious Patterns

### Config Resolution Order
YAML (`beam.yaml`) → env vars → CLI overrides → `BeamConfig` (Pydantic v2). All fields validated via Pydantic models in `config.py`.

### RunContext
Single state dataclass passed through all pipeline steps. Steps mutate it to carry state forward (e.g. `ssh_info` set after `ensure_machine`). Derived paths are `@property`.

### Two Pipeline Modes
- **Registry mode** (`GITHUB_USERNAME` set): build_venv → push_image → ensure_machine → sync_venv → sync_code → setup_creds → run_remote
- **Stream mode** (no registry): ensure_machine → build_venv → sync_venv → sync_code → setup_creds → setup_uv → run_remote

### Sentinel Files
Remote job state tracked via files (`RUNNING`/`FINISHED`/`FAILED`) in `.beam_state/`. Pulse reads these for health checks.

### Unified Per-Job Log
`log.py`'s `attach_job_log`/`detach_job_log` mirror all output to `.beam/logs/sessions/<id>.log`. `beam run` and every `beam pulse check` (incl. restarts) append to the same file — one timeline the pulse agent reads via the `read_job_log` tool.

### Project Doc (`BEAM_AGENT.md`)
Committed, human-editable operator knowledge fed to the pulse agent every check (`pulse.agent.project_doc`). The agent maintains only its own marker-delimited section (`beam.agent.project_doc.update_agent_section`), written after a successful check; human content is never overwritten.

### Provider Status Normalisation
All providers must map native statuses to: `running`, `stopped`, `exited`, `unknown`, `error`, `api_error`.

## Adding New Components

| Task | Where |
|------|-------|
| New CLI flag | `cli.py` + `config.py` |
| New pipeline step | `steps/<name>.py` + register in `pipeline.py` |
| New provider | `providers/<name>.py` (impl `ProviderBase`) + `cli.py` + `pipeline.py` + `config.py` |
| New pulse agent tool | `agent/tools.py` (fn + schema + register in `dispatch`) |
| New pulse agent LLM backend | `agent/llm.py` (`PROVIDER_BASE`) + `agent/secrets.py` (`PROVIDER_ENV`) |

## Documentation

Hierarchical docs in every `src/` subdirectory:
- `README.md` — features and usage (for users)
- `ARCHITECTURE.md` — implementation details (for developers)

Outermost `README.md` is a GitHub project overview. Outermost `ARCHITECTURE.md` is a directory-to-feature lookup map.

**Update both files in every directory you modify.** Also update the outermost `README.md` and `ARCHITECTURE.md` if changes affect project-level docs. Outdated docs are worse than no docs.

## Planning

If a task looks like it will have more than 3 steps, create a plan first and get alignment before executing. Break complex work into discrete steps and execute them one by one.
