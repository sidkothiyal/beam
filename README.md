# Beam

Remote GPU training deployment toolkit. Provision machines on RunPod or Vast.ai,
sync your environment and code, and launch training — all from a single command.

## Install

```bash
uv pip install -e .
```

## Quick start

```bash
# Place a beam.yaml in your project root (see beam init)
beam init

# Deploy and run on Vast.ai with pulse monitoring
beam run --provider vast.ai --pulse 5m --pulse-idle-stop -- \
  "uv run python src/trainer.py experiment/autoencoder/compression_ae.yaml"

# Deploy to an existing machine
beam run --ip 1.2.3.4 -p 22222 -- "uv run python src/trainer.py config.yaml"

# Setup-only (no command)
beam run --provider runpod

# Manage sessions
beam status
beam attach <session-id>
beam logs <session-id>
beam stop <session-id>

# Pulse monitoring
beam pulse list
beam pulse uninstall --all
```

## Configuration

Beam reads `beam.yaml` from your project root. All settings can be overridden
via CLI flags or environment variables. Run `beam init` to generate a starter
config.
