# `src/beam/providers/` — Cloud Providers

This package contains cloud provider implementations for GPU instance provisioning. All providers implement the `ProviderBase` interface.

For **deep implementation details** (method signatures, status normalisation, bid strategy internals), see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## Overview

Beam supports two providers:

| Provider | Module | API Base |
|----------|--------|----------|
| **RunPod** | `runpod.py` | `https://rest.runpod.io/v1` |
| **Vast.ai** | `vastai.py` | `https://console.vast.ai/api/v0` |

Both implement the same `ProviderBase` interface, so pipeline code is provider-agnostic.

---

## `ProviderBase` Interface

Every provider implements these 7 methods:

```python
from beam.providers.base import ProviderBase

class ProviderBase(ABC):
    def create_instance(self, cfg: MachineConfig) -> str:
        """Create a new instance. Returns pod_id."""

    def start_instance(self, pod_id: str) -> None:
        """Start a stopped instance."""

    def stop_instance(self, pod_id: str) -> None:
        """Stop (pause) a running instance."""

    def destroy_instance(self, pod_id: str) -> None:
        """Permanently destroy an instance."""

    def get_status(self, pod_id: str) -> str:
        """Get normalised instance status."""
        # Returns: "running" | "stopped" | "exited" | "unknown" | "error" | "api_error"

    def get_ssh_info(self, pod_id: str) -> SSHInfo:
        """Get SSH connection details for a running instance."""

    def wait_ready(self, pod_id: str, timeout: int = 300) -> SSHInfo:
        """Wait until instance is ready for SSH. Returns SSHInfo."""
```

### Normalised Status Values

All providers map their native status codes to these standard values:

| Value | Meaning |
|-------|---------|
| `running` | Instance is up and accepting SSH |
| `stopped` | Instance is paused/stopped (can be started) |
| `exited` | Instance has terminated/exited (spot reclaimed, etc.) |
| `unknown` | Status not recognised |
| `error` | Instance in error state |
| `api_error` | API call failed (network, auth, rate limit) |

The `api_error` status is important — Pulse skips health-check actions on `api_error` to avoid false positives from transient API failures.

---

## RunPod Provider

**Key features:**
- Uses RunPod's REST API (not GraphQL legacy)
- Auth via `RUNPOD_API_KEY` environment variable
- `wait_ready()` extends timeout 300s→600s when image is being pulled
- Spot reclamation detection: immediately exits `wait_ready()` when `desiredStatus` → `EXITED`/`TERMINATED`
- `list_gpu_types()` for `beam offers --provider runpod`

**When to use:** Reliable, stable pricing, good for SECURE cloud tier instances. Better for longer training runs.

**Config:** See `RunPodMachineConfig` in [`../../README.md`](../../README.md#providersrunpodmachines).

---

## Vast.ai Provider

**Key features:**
- Spot/bid marketplace — typically cheaper than RunPod
- 5 bid strategies for cost/performance optimisation
- Outbid detection with retry (up to 3 attempts, `_MAX_OUTBID_RETRIES`)
- Progressive bidding on `create_instance()` (increments until accepted)
- `wait_ready()` polls `actual_status`, extends timeout for loading states
- `list_compatible_offers()` for `beam offers --provider vastai`

**When to use:** Cost-sensitive training, short experiments, or when you need specific GPU models at lower prices.

**Config:** See `VastAIMachineConfig` in [`../../README.md`](../../README.md#providersvastaimachines).

### 5 Bid Strategies

Configure via `bid_strategy` in your machine config:

| Strategy | When to Use |
|----------|-------------|
| `multiplier` (default) | General use — cheapest offer, bid slightly above min |
| `cheapest` | Legacy — manual price control with incremental bidding |
| `best_value` | Best performance per dollar (dlperf/dph ratio) |
| `percentile` | Control bid at Nth percentile of market min bids |
| `score` | Best reliability-weighted performance score |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for full strategy details.

---

## Adding a New Provider

1. Create `src/beam/providers/<name>.py`
2. Implement all 7 `ProviderBase` methods
3. Map native statuses to normalised values
4. Add config model to `config.py` (`<Name>MachineConfig`, `<Name>ProviderConfig`)
5. Register in `ProvidersConfig.providers` in `config.py`
6. Add provider selection in `cli.py` and `pipeline.py`
7. Update this README and `ARCHITECTURE.md`

---

## See Also

- **[`ARCHITECTURE.md`](./ARCHITECTURE.md)** — Deep implementation details
- **[`../../README.md`](../../README.md)** — Project README with full config reference
- **[`../steps/ensure_machine.py`](../steps/ensure_machine.py)** — Where providers are called
