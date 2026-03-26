# `src/beam/providers/` — Implementation Details

Deep reference for provider implementations. Covers method signatures, status normalisation, timeouts, outbid detection, and bid strategies.

For **features and usage**, see [`README.md`](./README.md).

---

## `base.py` — ProviderBase ABC

```python
from abc import ABC, abstractmethod
from beam.ssh import SSHInfo

class ProviderBase(ABC):

    @abstractmethod
    def create_instance(self, cfg) -> str:
        """Create instance. Returns pod_id (str)."""

    @abstractmethod
    def start_instance(self, pod_id: str) -> None:
        """Start a stopped instance."""

    @abstractmethod
    def stop_instance(self, pod_id: str) -> None:
        """Stop (pause) a running instance."""

    @abstractmethod
    def destroy_instance(self, pod_id: str) -> None:
        """Permanently destroy an instance."""

    @abstractmethod
    def get_status(self, pod_id: str) -> str:
        """
        Returns normalised status string:
          "running"   — up, SSH accessible
          "stopped"   — paused, can be started
          "exited"    — terminated (spot reclaimed or exited)
          "unknown"   — unrecognised status
          "error"     — instance error state
          "api_error" — API call failed (transient)
        """

    @abstractmethod
    def get_ssh_info(self, pod_id: str) -> SSHInfo:
        """Return SSH connection details for a running instance."""

    @abstractmethod
    def wait_ready(self, pod_id: str, timeout: int = 300) -> SSHInfo:
        """
        Poll until instance is SSH-ready. Returns SSHInfo.
        Raises TimeoutError if not ready within timeout.
        """
```

---

## `runpod.py` — RunPod Client

### Class

```python
class RunPodProvider(ProviderBase):
    BASE_URL = "https://rest.runpod.io/v1"

    def __init__(self, api_key: str) -> None:
        # api_key from RUNPOD_API_KEY
        # httpx.Client with Authorization: Bearer {api_key}
```

### Methods

```python
def create_instance(self, cfg: RunPodMachineConfig) -> str:
    # Calls build_create_payload(cfg) → POST /pods
    # Returns pod["id"]

def start_instance(self, pod_id: str) -> None:
    # POST /pods/{pod_id}/start

def stop_instance(self, pod_id: str) -> None:
    # POST /pods/{pod_id}/stop

def destroy_instance(self, pod_id: str) -> None:
    # DELETE /pods/{pod_id}

def get_status(self, pod_id: str) -> str:
    # GET /pods/{pod_id}
    # Maps runtime.status:
    #   "RUNNING"    → "running"
    #   "PAUSED"     → "stopped"
    #   "EXITED"     → "exited"
    #   "TERMINATED" → "exited"
    #   None/missing → "unknown"
    # API error → "api_error"

def get_ssh_info(self, pod_id: str) -> SSHInfo:
    # GET /pods/{pod_id}
    # Extracts runtime.ports for port 22/tcp → external port
    # Returns SSHInfo(host=pod["machine"]["publicIp"], port=..., ...)

def wait_ready(self, pod_id: str, timeout: int = 300) -> SSHInfo:
    # Polls GET /pods/{pod_id} every 5s
    # When runtime.status == "RUNNING" → return get_ssh_info()
    # Timeout extension: if image pulling detected → extends to 600s
    # Spot reclamation: if desiredStatus == "EXITED"|"TERMINATED" → raises immediately
```

### `build_create_payload()`

```python
def build_create_payload(self, cfg: RunPodMachineConfig) -> dict:
    # Builds RunPod REST API pod creation payload
    # Maps cfg fields → API fields
    # Handles: ssh_public_key_files (reads file contents), env_vars, ports
```

### `list_gpu_types()`

```python
def list_gpu_types(self) -> list[dict]:
    # GET /gpus
    # Returns list of GPU type objects for beam offers
```

### Loading Detection (wait_ready)

RunPod image pull can take several minutes. Detection heuristic:
```python
# If pod has been in non-RUNNING state for > 5 minutes, check if pulling:
if "pulling" in status_message.lower() or runtime.status == "LOADING":
    timeout = max(timeout, 600)  # extend to 10 minutes
```

### Spot Reclamation

```python
# In wait_ready polling loop:
pod_data = get_pod(pod_id)
if pod_data.get("desiredStatus") in ("EXITED", "TERMINATED"):
    raise SpotReclamationError(f"Pod {pod_id} was reclaimed")
```

---

## `vastai.py` — Vast.ai Client

### Types

```python
class OutbidError(Exception):
    """Raised when our bid is too low and we were outbid."""

@dataclass
class ResolvedOffer:
    offer_id: int
    dph_total: float        # $/hr total cost
    gpu_name: str
    num_gpus: int
    bid_price: float        # Our computed bid price
```

### Class

```python
class VastAIProvider(ProviderBase):
    BASE_URL = "https://console.vast.ai/api/v0"
    _MAX_OUTBID_RETRIES = 3

    def __init__(self, api_key: str) -> None:
        # api_key from VAST_API_KEY
        # httpx.Client with Authorization: Bearer {api_key}
```

### Methods

```python
def create_instance(self, cfg: VastAIMachineConfig) -> str:
    # resolve_offer_id(cfg) → ResolvedOffer
    # POST /asks/{offer_id}/ with bid_price, image, disk_gb, etc.
    # Progressive bidding: if initial bid fails, increment up to price_max
    # Catches OutbidError (raised if _is_outbid() during wait_ready)
    # Returns contract_id (str)

def start_instance(self, pod_id: str) -> None:
    # PUT /instances/{pod_id}/ {"state": "running"}

def stop_instance(self, pod_id: str) -> None:
    # PUT /instances/{pod_id}/ {"state": "stopped"}

def destroy_instance(self, pod_id: str) -> None:
    # DELETE /instances/{pod_id}/

def get_status(self, pod_id: str) -> str:
    # GET /instances/{pod_id}/
    # Maps actual_status:
    #   "running"  → "running"
    #   "stopped"  → "stopped"
    #   "exited"   → "exited"
    #   other      → "unknown"
    # API error → "api_error"

def get_ssh_info(self, pod_id: str) -> SSHInfo:
    # GET /instances/{pod_id}/
    # Returns SSHInfo(host=instance["ssh_host"], port=instance["ssh_port"], ...)

def wait_ready(self, pod_id: str, timeout: int = 300) -> SSHInfo:
    # Polls GET /instances/{pod_id}/ every 5s
    # When actual_status == "running" → return get_ssh_info()
    # Loading states ("loading", "provisioning") → extend timeout
    # _is_outbid() heuristic check during polling → raises OutbidError
```

### Offer Resolution

```python
def list_compatible_offers(self, cfg: VastAIMachineConfig) -> list[dict]:
    # GET /bundles/ with filter params:
    #   gpu_name, num_gpus, reliability, dph_max, dlperf_min, etc.
    # Annotates each offer with _computed_bid (from _apply_bid_strategy)
    # Returns filtered list (up to offer_limit)

def resolve_offer_id(self, cfg: VastAIMachineConfig) -> ResolvedOffer:
    # list_compatible_offers() → pick by pick_strategy
    # _apply_bid_strategy(offer, cfg) → bid_price
    # Returns ResolvedOffer

def _apply_bid_strategy(
    self,
    offer: dict,
    cfg: VastAIMachineConfig,
    all_offers: list[dict],
) -> float:
    # Dispatches to strategy implementation
    # Returns bid_price (float, $/hr)
```

### 5 Bid Strategies — Full Table

| Strategy | `bid_strategy` value | Offer Selection | Bid Computation |
|----------|---------------------|-----------------|-----------------|
| **Multiplier** (default) | `"multiplier"` | Cheapest by `dph_total` | `offer["min_bid"] * cfg.bid_multiplier` |
| **Cheapest** (legacy) | `"cheapest"` | Cheapest by `dph_total` | `cfg.price + N * cfg.price_step` (incremented until accepted) |
| **Best Value** | `"best_value"` | Best `dlperf_per_dphtotal` | `offer["min_bid"] * cfg.bid_multiplier` |
| **Percentile** | `"percentile"` | Cheapest by `dph_total` | `percentile(all_min_bids, cfg.bid_percentile)` |
| **Score** | `"score"` | Best `dlperf * reliability / min_bid` | `offer["min_bid"] * cfg.bid_multiplier` |

**Notes:**
- `min_bid` = Vast.ai's minimum accepted bid for the offer
- `dlperf` = deep learning performance benchmark score
- `dlperf_per_dphtotal` = performance per dollar
- `bid_multiplier` default: `1.1` (10% above minimum)
- `bid_percentile` default: `50` (median of market)

### Outbid Detection

Vast.ai has no explicit "outbid" API status. Detection heuristic:
```python
def _is_outbid(self, instance: dict) -> bool:
    # Heuristic: instance is in "exited" state shortly after creation
    # with no log output and age < 5 minutes
    actual_status = instance.get("actual_status")
    age_seconds = time.time() - instance.get("start_date", time.time())
    return actual_status == "exited" and age_seconds < 300
```

When detected: `create_instance()` increments bid and retries (max `_MAX_OUTBID_RETRIES = 3`).

### Loading-Aware wait_ready

```python
LOADING_STATES = {"loading", "provisioning", "created"}
# If actual_status in LOADING_STATES for > initial timeout:
#   extend timeout by 300s (up to 2x original)
```

---

## See Also

- **[`README.md`](./README.md)** — Features & usage
- **[`../steps/ensure_machine.py`](../steps/ensure_machine.py)** — Provider usage in pipeline
- **[`../../README.md`](../../README.md)** — Bid strategy config reference
