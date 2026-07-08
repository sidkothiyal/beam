"""Vast.ai REST API provider (httpx)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from beam import log
from beam.config import VastAIMachineConfig
from beam.providers.base import ProviderBase, ProviderError
from beam.ssh import SSHInfo


class OutbidError(ProviderError):
    """Raised when an interruptible instance is outbid."""

    def __init__(self, instance_id: str, msg: str = ""):
        self.instance_id = instance_id
        super().__init__(msg or f"Instance {instance_id} was outbid")

VAST_API_BASE = "https://console.vast.ai/api/v0"

# Vast reports ``gpu_ram`` in MiB. GPUs marketed as "NN GB" frequently report
# slightly below the clean binary boundary (e.g. H100 SXM 80GB = 81559 MiB =
# 79.6 GiB), so a strict ``gpu_ram_min * 1024`` threshold silently excludes
# them and the offer search returns zero. Admit cards within this tolerance of
# the requested GiB. VRAM tiers (40/48/80/96/141/192 GB) are spaced far wider
# than this margin, so it never blurs adjacent tiers.
GPU_RAM_TOLERANCE = 0.97

# Vast applies a small default page size to /bundles/ searches. When expanding a
# partial ``gpu_name`` (e.g. "RTX PRO 6000") to the concrete labels Vast offers
# ("RTX PRO 6000 S", "RTX PRO 6000 WS"), request a large page so we observe every
# available label before prefix-filtering.
GPU_NAME_DISCOVERY_LIMIT = 1000


@dataclass
class ResolvedOffer:
    offer_id: str
    dph_total: float = 0.0
    gpu_name: str = ""
    num_gpus: int = 0
    bid_price: float = 0.0


class VastAIProvider(ProviderBase):

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("VAST_API_KEY", "")
        if not self.api_key:
            raise ProviderError(
                "VAST_API_KEY is not set. "
                "Get one at https://cloud.vast.ai/account/"
            )
        self._client = httpx.Client(
            base_url=VAST_API_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    # -- low-level API ---------------------------------------------------------

    def _request(self, method: str, path: str, json: dict | None = None) -> tuple[int, dict | list]:
        resp = self._client.request(method, path, json=json)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return resp.status_code, body

    # -- queries ---------------------------------------------------------------

    def get_instance(self, instance_id: str) -> dict:
        code, body = self._request("GET", f"/instances/{instance_id}/")
        if code != 200:
            raise ProviderError(f"GET /instances/{instance_id}/ returned HTTP {code}")
        if isinstance(body, dict) and "instances" in body:
            return body["instances"] or {}
        return body if isinstance(body, dict) else {}

    def search_offers(self, body: dict) -> list[dict]:
        code, resp = self._request("POST", "/bundles/", json=body)
        if code != 200:
            raise ProviderError(f"Search offers failed (HTTP {code})")
        if isinstance(resp, dict):
            return resp.get("offers", [])
        return resp if isinstance(resp, list) else []

    # -- lifecycle -------------------------------------------------------------

    def _create_raw(self, offer_id: str | int, payload: dict) -> tuple[int, dict]:
        code, body = self._request("PUT", f"/asks/{offer_id}/", json=payload)
        return code, body if isinstance(body, dict) else {}

    def create_instance(self, config: dict) -> str:
        """Create an instance. *config* must contain ``offer_id`` and ``payload``."""
        offer_id = config["offer_id"]
        payload = config["payload"]
        price = payload.get("price")
        price_max = config.get("price_max")
        price_step = config.get("price_step", 0.005)

        if price is not None and price_max is not None:
            current = price
            while True:
                payload["price"] = round(current, 4)
                log.step(f"Creating Vast.ai instance (bid ${current:.4f}/hr) ...")
                code, body = self._create_raw(offer_id, payload)
                if code == 200:
                    break
                log.warn(f"Create failed (HTTP {code}). Trying higher bid ...")
                if current >= price_max:
                    msg = body.get("msg") or body.get("error") or str(body)
                    raise ProviderError(f"Failed to create instance at max price (HTTP {code}): {msg}")
                current = min(current + price_step, price_max)
        else:
            log.step("Creating Vast.ai instance ...")
            code, body = self._create_raw(offer_id, payload)

        if code != 200:
            msg = body.get("msg") or body.get("error") or str(body)
            raise ProviderError(f"Failed to create instance (HTTP {code}): {msg}")

        instance_id = str(body.get("new_contract", ""))
        if not instance_id:
            raise ProviderError(f"No instance ID in response: {body}")
        log.done(f"Instance created: {instance_id}")
        return instance_id

    def start_instance(self, instance_id: str) -> None:
        log.step(f"Starting Vast.ai instance {instance_id} ...")
        code, body = self._request("PUT", f"/instances/{instance_id}/", json={"state": "running"})
        if code != 200:
            msg = (body if isinstance(body, dict) else {}).get("msg", str(body))
            raise ProviderError(f"Failed to start instance (HTTP {code}): {msg}")

    def stop_instance(self, instance_id: str) -> None:
        log.step(f"Stopping Vast.ai instance {instance_id} ...")
        code, body = self._request("PUT", f"/instances/{instance_id}/", json={"state": "stopped"})
        if code != 200:
            log.warn(f"Stop instance returned HTTP {code}")
        else:
            log.done(f"Instance {instance_id} stopped.")

    def destroy_instance(self, instance_id: str) -> None:
        log.step(f"Destroying Vast.ai instance {instance_id} ...")
        code, body = self._request("DELETE", f"/instances/{instance_id}/")
        if code in (200, 204):
            log.done(f"Instance {instance_id} destroyed.")
        elif code == 404:
            log.info(f"Instance {instance_id} already gone (HTTP 404)")
        else:
            log.warn(f"Destroy instance returned HTTP {code}: {body}")

    # -- status ----------------------------------------------------------------

    def get_status(self, instance_id: str) -> str:
        try:
            info = self.get_instance(instance_id)
        except ProviderError:
            return "api_error"
        for field in ("cur_state", "actual_status", "intended_status"):
            val = info.get(field, "")
            if val:
                return str(val).lower()
        return "unknown"

    def is_outbid(self, instance_id: str) -> bool:
        """Check if an instance was outbid. Safe to call from pulse."""
        try:
            info = self.get_instance(instance_id)
        except ProviderError:
            return False
        # Use a generous elapsed value so the heuristic fires
        return self._is_outbid(info, elapsed=60)

    def get_ssh_info(self, instance_id: str) -> SSHInfo | None:
        info = self.get_instance(instance_id)
        host = info.get("ssh_host", "")
        port = info.get("ssh_port", "")
        if host and port:
            return SSHInfo(host=str(host), port=str(port))
        return None

    # -- polling ---------------------------------------------------------------

    _OUTBID_GRACE_SECONDS = 10

    _TERMINAL_STATES = frozenset({"stopped", "exited"})
    _HEALTHY_STATES = frozenset({"running", "loading", "creating", "connecting"})

    @staticmethod
    def _is_outbid(info: dict, elapsed: int = 0) -> bool:
        """Return True if instance info indicates an outbid condition.

        The Vast.ai API does not surface an explicit "outbid" status; the
        dashboard derives it client-side.  We detect it three ways:

        1. Explicit text -- any status field containing "outbid" (future-proof).
        2. Heuristic -- after the grace period, if the resolved status (first
           non-empty of cur_state / actual_status / intended_status) is stuck
           in a terminal state ("stopped" / "exited") and no field shows a
           healthy state, the instance is dead-on-arrival.
        3. Empty info -- API returned nothing after the grace period.
        """
        if not info:
            return elapsed >= VastAIProvider._OUTBID_GRACE_SECONDS

        # 1. Explicit outbid text in any field
        for field in ("status_msg", "cur_state", "actual_status",
                       "intended_status", "next_state"):
            if "outbid" in str(info.get(field, "")).lower():
                return True

        # 2. Both cur_state and intended_status are "stopped" — the provider
        #    never intended to run this instance, i.e. it was outbid immediately.
        cur = str(info.get("cur_state", "")).lower()
        intended = str(info.get("intended_status", "")).lower()
        if cur == "stopped" and intended == "stopped":
            return True

        if elapsed < VastAIProvider._OUTBID_GRACE_SECONDS:
            return False

        # 2. Check actual state fields (not intended_status, which is
        #    aspirational — it stays "running" even when the instance is dead)
        actual_statuses = set()
        for field in ("cur_state", "actual_status"):
            val = str(info.get(field, "")).lower()
            if val:
                actual_statuses.add(val)

        # If any actual field shows a healthy state, the instance is progressing
        if actual_statuses & VastAIProvider._HEALTHY_STATES:
            return False

        # If the actual status is terminal, the instance is dead
        if actual_statuses & VastAIProvider._TERMINAL_STATES:
            return True

        return False

    def check_outbid(self, instance_id: str) -> bool:
        """Check if an instance is currently in an outbid state."""
        try:
            info = self.get_instance(instance_id)
        except ProviderError:
            info = {}
        return self._is_outbid(info, elapsed=self._OUTBID_GRACE_SECONDS)

    def wait_ready(self, instance_id: str, timeout: int = 600) -> None:
        log.step(f"Waiting for instance {instance_id} to become ready ...")
        elapsed = 0
        interval = 10

        while elapsed < timeout:
            try:
                info = self.get_instance(instance_id)
            except ProviderError:
                info = {}

            log.debug(f"  Instance info: {info}")

            if self._is_outbid(info, elapsed):
                raise OutbidError(instance_id)

            status = ""
            for field in ("cur_state", "actual_status", "intended_status"):
                val = info.get(field, "")
                if val:
                    status = str(val).lower()
                    break

            if status == "running":
                log.done("Instance is running.")
                return

            cur = str(info.get("cur_state", "")).lower()
            actual = str(info.get("actual_status", "")).lower()
            intended = str(info.get("intended_status", "")).lower()
            log.info(f"  Waiting ... cur={cur} actual={actual} intended={intended} ({elapsed}s / {timeout}s)")
            time.sleep(interval)
            elapsed += interval
        raise ProviderError(f"Instance did not become ready within {timeout}s")

    def wait_ssh_info(self, instance_id: str, timeout: int = 120) -> SSHInfo:
        log.step(f"Retrieving SSH info for instance {instance_id} ...")
        elapsed = 0
        interval = 10
        while elapsed < timeout:
            try:
                info = self.get_instance(instance_id)
            except ProviderError:
                info = {}

            if self._is_outbid(info, elapsed=self._OUTBID_GRACE_SECONDS):
                raise OutbidError(instance_id)

            host = info.get("ssh_host", "")
            port = info.get("ssh_port", "")
            if host and port:
                ssh = SSHInfo(host=str(host), port=str(port))
                log.done(f"SSH ready: {ssh.host}:{ssh.port}")
                return ssh
            status = str(info.get("cur_state", "") or info.get("actual_status", "")).lower()
            log.info(f"  Waiting for SSH info ... status={status} ({elapsed}s / {timeout}s)")
            time.sleep(interval)
            elapsed += interval
        raise ProviderError(f"SSH info not available after {timeout}s")

    # -- JSON builders ---------------------------------------------------------

    @staticmethod
    def build_create_payload(cfg: VastAIMachineConfig, ghcr_image: str = "") -> dict:
        """Build the JSON payload for PUT /asks/{id}/."""
        image = ghcr_image or cfg.image or "vastai/base-image:@vastai-automatic-tag"
        payload: dict = {
            "image": image,
            "disk": cfg.disk_gb,
            "label": cfg.instance_label or cfg.name or "beam-training",
            "runtype": cfg.runtype,
            "target_state": cfg.target_state,
        }
        if cfg.price:
            payload["price"] = cfg.price
        if cfg.onstart:
            payload["onstart"] = cfg.onstart
        if cfg.env:
            payload["env"] = cfg.env
        return payload

    @staticmethod
    def _gpu_name_matches(candidate: str, query: str) -> bool:
        """Whole-token prefix match of a Vast ``gpu_name`` label against a query.

        A candidate matches when it equals the query exactly, or when the query
        is a whole-token prefix of it (i.e. the query followed by a space). So
        "RTX PRO 6000" matches both "RTX PRO 6000 S" and "RTX PRO 6000 WS", while
        the fully-qualified "RTX PRO 6000 S" matches only itself. The trailing
        space guard prevents partial-token matches like "RTX 4080" -> "RTX 4080S".
        """
        return candidate == query or candidate.startswith(query + " ")

    def _resolve_gpu_names(self, cfg: VastAIMachineConfig) -> list[str] | None:
        """Expand ``cfg.gpu_name`` to the concrete Vast labels it matches.

        Vast's API has no server-side substring/prefix filter, so we fetch the
        labels available under the config's *other* constraints and filter them
        with :meth:`_gpu_name_matches`. Returns ``None`` when no ``gpu_name`` is
        set (no filter), or the (possibly empty) list of matching labels, which
        the caller feeds to the search as an ``in`` filter.
        """
        if not cfg.gpu_name:
            return None
        probe = self.build_search_body(cfg)
        probe.pop("gpu_name", None)  # discover across all labels, then prefix-filter
        probe["limit"] = max(cfg.offer_limit, GPU_NAME_DISCOVERY_LIMIT)
        available = {o.get("gpu_name", "") for o in self.search_offers(probe)}
        matched = sorted(
            n for n in available if n and self._gpu_name_matches(n, cfg.gpu_name)
        )
        if matched and matched != [cfg.gpu_name]:
            log.step(f"gpu_name '{cfg.gpu_name}' matched: {', '.join(matched)}")
        return matched

    @staticmethod
    def build_search_body(
        cfg: VastAIMachineConfig, gpu_names: list[str] | None = None
    ) -> dict:
        """Build the search-offers body.

        When *gpu_names* is provided (from :meth:`_resolve_gpu_names`), it filters
        by that resolved label set via ``in``. When it is ``None``, a configured
        ``cfg.gpu_name`` falls back to an exact ``eq`` match.
        """
        is_bid = cfg.offer_type == "bid"
        body: dict = {
            "limit": cfg.offer_limit,
            "type": cfg.offer_type,
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "order": [["min_bid" if is_bid else "dph_total", "asc"]],
        }
        if gpu_names is not None:
            body["gpu_name"] = {"in": gpu_names}
        elif cfg.gpu_name:
            body["gpu_name"] = {"eq": cfg.gpu_name}
        if cfg.num_gpus or cfg.num_gpus_max is not None:
            gpu_filter: dict = {}
            if cfg.num_gpus:
                gpu_filter["gte"] = cfg.num_gpus
            if cfg.num_gpus_max is not None:
                gpu_filter["lte"] = cfg.num_gpus_max
            body["num_gpus"] = gpu_filter
        if cfg.gpu_ram_min is not None:
            # See GPU_RAM_TOLERANCE: marketed "80GB" cards report ~79.6 GiB, so
            # relax the GiB floor slightly rather than dropping the offer.
            body["gpu_ram"] = {"gte": int(cfg.gpu_ram_min * 1024 * GPU_RAM_TOLERANCE)}
        if cfg.machine_id:
            body["machine_id"] = {"eq": cfg.machine_id}
        if cfg.disk_gb:
            body["disk_space"] = {"gte": cfg.disk_gb}
        if cfg.reliability_min is not None:
            body["reliability"] = {"gte": cfg.reliability_min}
        price_cap = cfg.dph_max or cfg.price_max
        if price_cap is not None:
            if is_bid:
                body["min_bid"] = {"lte": price_cap}
            else:
                body["dph_total"] = {"lte": price_cap}
        if cfg.dlperf_min is not None:
            body["dlperf"] = {"gte": cfg.dlperf_min}
        if cfg.dlperf_per_dphtotal_min is not None:
            body["dlperf_per_dphtotal"] = {"gte": cfg.dlperf_per_dphtotal_min}
        return body

    # -- pick & bid strategies -------------------------------------------------

    _VALID_PICK_STRATEGIES = frozenset({"cheapest", "best_value", "score"})
    _VALID_BID_STRATEGIES = frozenset({"multiplier", "percentile", "fixed"})

    @staticmethod
    def _rank_offers(offers: list[dict], cfg: VastAIMachineConfig) -> None:
        """Sort *offers* in-place according to ``cfg.pick_strategy``."""
        pick = cfg.pick_strategy
        INF = float("inf")

        if pick == "cheapest":
            offers.sort(key=lambda o: o.get("min_bid", INF))
        elif pick == "best_value":
            offers.sort(key=lambda o: o.get("dlperf_per_dphtotal", 0.0), reverse=True)
        elif pick == "score":
            for o in offers:
                mb = max(o.get("min_bid", INF), 0.001)
                o["_score"] = o.get("dlperf", 0.0) * o.get("reliability", 0.0) / mb
            offers.sort(key=lambda o: o.get("_score", 0.0), reverse=True)
        else:
            raise ProviderError(
                f"Unknown pick_strategy '{pick}'. "
                f"Valid: {', '.join(sorted(VastAIProvider._VALID_PICK_STRATEGIES))}"
            )

    @staticmethod
    def _compute_bid(offer: dict, offers: list[dict], cfg: VastAIMachineConfig) -> float:
        """Compute the bid price for *offer* according to ``cfg.bid_strategy``."""
        min_bid = offer.get("min_bid", 0.0)

        def _clamp(price: float) -> float:
            price = max(price, min_bid, cfg.price)
            if cfg.price_max is not None:
                price = min(price, cfg.price_max)
            return round(price, 4)

        strategy = cfg.bid_strategy

        if strategy == "fixed":
            return _clamp(cfg.price)

        if strategy == "multiplier":
            return _clamp(min_bid * cfg.bid_multiplier)

        if strategy == "percentile":
            all_bids = sorted(o.get("min_bid", 0.0) for o in offers)
            idx = min(int(len(all_bids) * cfg.bid_percentile), len(all_bids) - 1)
            return _clamp(all_bids[idx])

        raise ProviderError(
            f"Unknown bid_strategy '{strategy}'. "
            f"Valid: {', '.join(sorted(VastAIProvider._VALID_BID_STRATEGIES))}"
        )

    @staticmethod
    def _apply_bid_strategy(
        offers: list[dict], cfg: VastAIMachineConfig
    ) -> tuple[dict, float]:
        """Select an offer and compute a bid price.

        Uses ``cfg.pick_strategy`` to rank offers and ``cfg.bid_strategy``
        to determine the bid price.  For on-demand offers both are ignored
        and the cheapest offer is returned with price 0.
        """
        if cfg.offer_type != "bid":
            offers.sort(key=lambda o: o.get("dph_total", float("inf")))
            return offers[0], 0.0

        VastAIProvider._rank_offers(offers, cfg)
        offer = offers[0]
        bid = VastAIProvider._compute_bid(offer, offers, cfg)
        return offer, bid

    # -- offer listing ---------------------------------------------------------

    def list_compatible_offers(
        self, cfg: VastAIMachineConfig
    ) -> list[dict]:
        """Return all matching offers sorted according to ``cfg.pick_strategy``.

        Each offer dict is augmented with ``_computed_bid`` (the bid price the
        bid strategy would produce) so callers can display it.
        """
        gpu_names = self._resolve_gpu_names(cfg)
        search_body = self.build_search_body(cfg, gpu_names=gpu_names)
        offers = self.search_offers(search_body)
        if not offers:
            return []

        is_bid = cfg.offer_type == "bid"

        if is_bid:
            self._rank_offers(offers, cfg)
            for o in offers:
                o["_computed_bid"] = self._compute_bid(o, offers, cfg)
        else:
            offers.sort(key=lambda o: o.get("dph_total", INF))
            for o in offers:
                o["_computed_bid"] = 0.0

        return offers

    # -- offer resolution ------------------------------------------------------

    def resolve_offer_id(
        self,
        cfg: VastAIMachineConfig,
        *,
        exclude_offer_ids: set[str] | None = None,
    ) -> ResolvedOffer:
        """Search for the best offer matching the config requirements.

        The offer selection and bid price are determined by
        ``cfg.bid_strategy``.
        """
        gpu_names = self._resolve_gpu_names(cfg)
        search_body = self.build_search_body(cfg, gpu_names=gpu_names)

        reqs = []
        if cfg.gpu_ram_min is not None:
            reqs.append(f"vram>={cfg.gpu_ram_min}GB")
        if cfg.gpu_name:
            reqs.append(f"gpu={cfg.gpu_name}")
        if cfg.num_gpus_max is not None:
            reqs.append(f"gpus={cfg.num_gpus}..{cfg.num_gpus_max}")
        else:
            reqs.append(f"gpus>={cfg.num_gpus}")
        if cfg.dlperf_min is not None:
            reqs.append(f"dlperf>={cfg.dlperf_min}")
        if cfg.price_max is not None:
            reqs.append(f"max=${cfg.price_max:.4f}/hr")
        log.step(
            f"Searching for offers ({', '.join(reqs)}) "
            f"[pick={cfg.pick_strategy}, bid={cfg.bid_strategy}] ..."
        )

        offers = self.search_offers(search_body)
        if exclude_offer_ids:
            offers = [o for o in offers if str(o.get("id", "")) not in exclude_offer_ids]
        if not offers:
            raise ProviderError(
                f"No offers found matching requirements ({', '.join(reqs)})"
            )

        offer, bid_price = self._apply_bid_strategy(offers, cfg)

        offer_id = str(offer.get("id", ""))
        gpu_name = offer.get("gpu_name", "unknown")
        gpu_ram_mb = offer.get("gpu_ram", 0)
        gpu_ram_gb = gpu_ram_mb / 1024 if gpu_ram_mb else 0
        n_gpus = offer.get("num_gpus", 0)
        dph = offer.get("dph_total", 0.0)
        min_bid = offer.get("min_bid", 0.0)
        dlperf = offer.get("dlperf", 0)

        if cfg.offer_type == "bid":
            price_label = f"min_bid=${min_bid:.4f}/hr  bid=${bid_price:.4f}/hr"
        else:
            price_label = f"${dph:.4f}/hr"
        log.done(
            f"Selected offer {offer_id}: "
            f"{n_gpus}x {gpu_name} ({gpu_ram_gb:.0f}GB) "
            f"@ {price_label}  "
            f"dlperf={dlperf:.1f}  "
            f"[{len(offers)} offer(s) found]"
        )
        return ResolvedOffer(
            offer_id=offer_id,
            dph_total=dph,
            gpu_name=gpu_name,
            num_gpus=n_gpus,
            bid_price=bid_price,
        )
