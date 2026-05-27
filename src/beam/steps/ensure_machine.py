"""Ensure a remote machine is running and reachable via SSH."""

from __future__ import annotations

import time
from typing import Callable

from beam import log
from beam.config import (
    BeamConfig,
    RunPodMachineConfig,
    VastAIMachineConfig,
)
from beam.providers.base import ProviderBase, ProviderError
from beam.providers.runpod import RunPodProvider
from beam.providers.vastai import OutbidError, VastAIProvider
from beam.ssh import SSHClient, SSHInfo

# Callback: given SSHInfo (and optionally pod_id), returns True if SSH is reachable.
_SSHVerifyFunc = Callable[..., bool]


class MachineResult:
    """Outcome of ensure_machine: connection info + provider state."""

    def __init__(
        self,
        ssh_info: SSHInfo,
        pod_id: str = "",
        provider_name: str = "",
        dph_total: float = 0.0,
    ) -> None:
        self.ssh_info = ssh_info
        self.pod_id = pod_id
        self.provider_name = provider_name
        self.dph_total = dph_total


def ensure_machine(
    cfg: BeamConfig,
    *,
    provider_name: str = "",
    pod_id: str = "",
    pod_config_name: str = "",
    ip: str = "",
    port: str = "",
    ghcr_image: str = "",
) -> MachineResult:
    """Make sure a remote machine is running and SSH-reachable.

    Logic mirrors the shell ``ensure_machine.sh``:
      1. If *ip* is given, check reachability; on failure, try provider recovery.
      2. If only *provider_name* is given, create or start via provider.
    """
    ssh_cfg = cfg.ssh

    # --------------------------------------------------------------- Case 1: IP given
    if ip:
        port = port or "22"
        info = SSHInfo(host=ip, port=port, user=cfg.remote.user, identity_file=ssh_cfg.identity_file)
        client = SSHClient(info=info, cfg=ssh_cfg)
        client.remove_from_known_hosts()

        log.step(f"Checking if {ip}:{port} is reachable ...")
        for attempt in range(1, 4):
            if client.check():
                log.done(f"Machine is reachable at {ip}:{port}.")
                return MachineResult(ssh_info=info, pod_id=pod_id, provider_name=provider_name)
            if attempt < 3:
                log.info(f"  Retrying in 10s ... ({attempt}/3)")
                time.sleep(10)

        log.warn(f"Machine at {ip}:{port} is unreachable after 3 attempts.")

        if provider_name and pod_id:
            return _recover_via_provider(
                cfg,
                provider_name=provider_name,
                pod_id=pod_id,
                pod_config_name=pod_config_name,
                ghcr_image=ghcr_image,
            )

        raise RuntimeError(
            "Machine is unreachable and no provider configured for auto-restart."
        )

    # --------------------------------------------------------------- Case 2: Provider
    if provider_name:
        return _create_via_provider(
            cfg,
            provider_name=provider_name,
            pod_id=pod_id,
            pod_config_name=pod_config_name,
            ghcr_image=ghcr_image,
        )

    raise RuntimeError(
        "No remote IP or provider specified. Use --ip or --provider."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_provider(name: str, cfg: BeamConfig | None = None) -> ProviderBase:
    creds = cfg.credentials if cfg else None
    if name == "runpod":
        return RunPodProvider(api_key=creds.runpod_api_key if creds else None)
    if name in ("vast.ai", "vastai"):
        return VastAIProvider(api_key=creds.vast_api_key if creds else None)
    raise RuntimeError(f"Unknown provider: {name} (supported: runpod, vast.ai)")


def _make_provider_status_fn(
    provider: ProviderBase, provider_name: str, pod_id: str
) -> Callable[[], dict | None]:
    """Return a status callback for ``SSHClient.wait_for_ssh``.

    The callback queries the provider API each poll and returns a dict
    with ``status`` (for logging) and ``stop`` (to abort early if the
    instance is dead/outbid).
    """
    if provider_name in ("vast.ai", "vastai") and isinstance(provider, VastAIProvider):
        def _vastai_status() -> dict | None:
            try:
                info = provider.get_instance(pod_id)
            except ProviderError:
                return {"status": "api_error"}
            cur = str(info.get("cur_state", "")).lower()
            actual = str(info.get("actual_status", "")).lower()
            intended = str(info.get("intended_status", "")).lower()
            label = f"cur={cur} actual={actual} intended={intended}"
            if provider._is_outbid(info, elapsed=provider._OUTBID_GRACE_SECONDS):
                return {"status": label, "stop": True, "message": f"instance outbid ({label})"}
            if cur in ("exited",) or (cur == "stopped" and intended == "stopped"):
                return {"status": label, "stop": True, "message": f"instance terminated ({label})"}
            return {"status": label}
        return _vastai_status

    # Generic fallback — no provider-level status available
    return lambda: None


def _get_recovery_policy(cfg: BeamConfig, provider_name: str, pod_config_name: str) -> str:
    """Read recovery_policy from the matching machine config."""
    if provider_name == "runpod":
        for mc in cfg.providers.runpod.configs:
            if mc.name == pod_config_name:
                return mc.recovery_policy
        if cfg.providers.runpod.configs:
            return cfg.providers.runpod.configs[0].recovery_policy
    elif provider_name in ("vast.ai", "vastai"):
        for mc in cfg.providers.vastai.configs:
            if mc.name == pod_config_name:
                return mc.recovery_policy
        if cfg.providers.vastai.configs:
            return cfg.providers.vastai.configs[0].recovery_policy
    return "restart"


def _recover_via_provider(
    cfg: BeamConfig,
    *,
    provider_name: str,
    pod_id: str,
    pod_config_name: str,
    ghcr_image: str,
) -> MachineResult:
    """Try to restart or recreate via provider after unreachable."""
    provider = _get_provider(provider_name, cfg)
    policy = _get_recovery_policy(cfg, provider_name, pod_config_name)

    if policy == "new":
        log.info("Recovery policy is 'new' -- creating a fresh instance ...")
        try:
            provider.stop_instance(pod_id)
        except Exception:
            pass
        return _create_via_provider(
            cfg,
            provider_name=provider_name,
            pod_id="",
            pod_config_name=pod_config_name,
            ghcr_image=ghcr_image,
        )

    log.info("Attempting to restart existing instance ...")
    provider.start_instance(pod_id)
    provider.wait_ready(pod_id)
    ssh_info = provider.wait_ssh_info(pod_id)
    ssh_info.user = cfg.remote.user
    ssh_info.identity_file = cfg.ssh.identity_file

    client = SSHClient(info=ssh_info, cfg=cfg.ssh)
    client.remove_from_known_hosts()
    status_fn = _make_provider_status_fn(provider, provider_name, pod_id)
    if not client.wait_for_ssh(status_fn=status_fn):
        raise RuntimeError("SSH not reachable after restart")
    return MachineResult(ssh_info=ssh_info, pod_id=pod_id, provider_name=provider_name)


def _create_via_provider(
    cfg: BeamConfig,
    *,
    provider_name: str,
    pod_id: str,
    pod_config_name: str,
    ghcr_image: str,
) -> MachineResult:
    """Create or start a machine via the provider, with fallback configs."""
    provider = _get_provider(provider_name, cfg)
    policy = ""

    # If there's an existing pod and policy != "new", try starting it
    if pod_id:
        policy = _get_recovery_policy(cfg, provider_name, pod_config_name)
        if policy != "new":
            provider.start_instance(pod_id)
            provider.wait_ready(pod_id)
            ssh_info = provider.wait_ssh_info(pod_id)
            ssh_info.user = cfg.remote.user
            ssh_info.identity_file = cfg.ssh.identity_file
            client = SSHClient(info=ssh_info, cfg=cfg.ssh)
            client.remove_from_known_hosts()
            status_fn = _make_provider_status_fn(provider, provider_name, pod_id)
            if not client.wait_for_ssh(status_fn=status_fn):
                raise RuntimeError("SSH not reachable after start")
            return MachineResult(ssh_info=ssh_info, pod_id=pod_id, provider_name=provider_name)

    # Create new
    configs = _resolve_fallback_configs(cfg, provider_name, pod_config_name)

    def _ssh_verify(ssh_info: SSHInfo, *, pod_id: str = "", _provider: ProviderBase = provider) -> bool:
        ssh_info.user = cfg.remote.user
        ssh_info.identity_file = cfg.ssh.identity_file
        client = SSHClient(info=ssh_info, cfg=cfg.ssh)
        client.remove_from_known_hosts()
        status_fn = _make_provider_status_fn(_provider, provider_name, pod_id) if pod_id else None
        return client.wait_for_ssh(status_fn=status_fn)

    for i, machine_cfg in enumerate(configs):
        try:
            new_pod_id, ssh_info, dph_total = _create_one(
                provider, provider_name, machine_cfg, ghcr_image,
                ssh_verify=_ssh_verify,
            )
            return MachineResult(
                ssh_info=ssh_info,
                pod_id=new_pod_id,
                provider_name=provider_name,
                dph_total=dph_total,
            )
        except (ProviderError, RuntimeError) as exc:
            log.warn(f"Creation failed with config '{machine_cfg.get('_name', '')}': {exc}")
            if i < len(configs) - 1:
                log.step("Waiting 15s before trying next config ...")
                time.sleep(15)

    raise RuntimeError(f"All {provider_name} configs failed.")


def _resolve_fallback_configs(
    cfg: BeamConfig, provider_name: str, pod_config_name: str
) -> list[dict]:
    """Return list of provider-ready payloads in fallback order."""
    if provider_name == "runpod":
        prov_cfg = cfg.providers.runpod
        order = prov_cfg.fallback_order
        configs_map = {mc.name: mc for mc in prov_cfg.configs}
        if pod_config_name and pod_config_name in configs_map:
            return [{"_name": pod_config_name, **RunPodProvider.build_create_payload(configs_map[pod_config_name])}]
        ordered = [configs_map[n] for n in order if n in configs_map] or prov_cfg.configs
        return [{"_name": mc.name, **RunPodProvider.build_create_payload(mc)} for mc in ordered]

    if provider_name in ("vast.ai", "vastai"):
        prov_cfg = cfg.providers.vastai
        order = prov_cfg.fallback_order
        configs_map = {mc.name: mc for mc in prov_cfg.configs}
        if pod_config_name and pod_config_name in configs_map:
            mc = configs_map[pod_config_name]
            return [_vastai_create_config(mc)]
        ordered = [configs_map[n] for n in order if n in configs_map] or prov_cfg.configs
        return [_vastai_create_config(mc) for mc in ordered]

    return []


def _vastai_create_config(mc: VastAIMachineConfig) -> dict:
    """Build the dict that VastAIProvider.create_instance expects."""
    return {
        "_name": mc.name,
        "_machine_cfg": mc,
        "offer_id": "",  # resolved at create time
        "payload": VastAIProvider.build_create_payload(mc),
        "price_max": mc.price_max,
        "price_step": mc.price_step,
    }


def _create_one(
    provider: ProviderBase,
    provider_name: str,
    create_cfg: dict,
    ghcr_image: str,
    ssh_verify: _SSHVerifyFunc | None = None,
) -> tuple[str, SSHInfo, float]:
    """Create a single instance and return (pod_id, ssh_info, dph_total).

    For Vast.ai bid instances, this loops through offers automatically:
    on outbid it rebids higher on the same offer (up to price_max), then
    excludes it and moves to the next cheapest offer.  The loop only stops
    when an instance is successfully created or all offers are exhausted.
    """
    dph_total = 0.0

    if provider_name in ("vast.ai", "vastai"):
        assert isinstance(provider, VastAIProvider)
        mc: VastAIMachineConfig = create_cfg["_machine_cfg"]
        excluded_offers: set[str] = set()
        # Track (current_bid, outbid_count) per offer
        offer_state: dict[str, tuple[float, int]] = {}
        attempt = 0

        while True:
            # resolve_offer_id raises ProviderError when no offers remain
            try:
                resolved = provider.resolve_offer_id(
                    mc, exclude_offer_ids=excluded_offers
                )
            except ProviderError:
                raise ProviderError(
                    f"All available offers exhausted after {attempt} "
                    f"attempt(s). No machines left to try."
                )

            offer_id = resolved.offer_id
            attempt += 1

            # Use escalated price if we've been outbid on this offer before
            if offer_id in offer_state:
                current_bid = offer_state[offer_id][0]
            else:
                current_bid = resolved.bid_price

            create_cfg["offer_id"] = offer_id
            dph_total = resolved.dph_total
            if current_bid:
                create_cfg["payload"]["price"] = current_bid
            if ghcr_image:
                create_cfg["payload"]["image"] = ghcr_image

            pod_id = provider.create_instance(create_cfg)
            try:
                provider.wait_ready(pod_id)
                ssh_info = provider.wait_ssh_info(pod_id)
            except OutbidError:
                _handle_outbid(
                    provider, mc, pod_id, offer_id, current_bid,
                    attempt, excluded_offers, offer_state,
                )
                continue

            # SSH verification — if it fails, check whether we were outbid
            if ssh_verify is not None and not ssh_verify(ssh_info, pod_id=pod_id):
                if provider.check_outbid(pod_id):
                    _handle_outbid(
                        provider, mc, pod_id, offer_id, current_bid,
                        attempt, excluded_offers, offer_state,
                    )
                    continue
                raise RuntimeError("SSH not reachable after create")

            return pod_id, ssh_info, dph_total

    pod_id = provider.create_instance(create_cfg)
    provider.wait_ready(pod_id)
    ssh_info = provider.wait_ssh_info(pod_id)
    if ssh_verify is not None and not ssh_verify(ssh_info, pod_id=pod_id):
        raise RuntimeError("SSH not reachable after create")
    return pod_id, ssh_info, dph_total


_MAX_REBIDS_PER_OFFER = 2


def _handle_outbid(
    provider: VastAIProvider,
    mc: VastAIMachineConfig,
    pod_id: str,
    offer_id: str,
    current_bid: float,
    attempt: int,
    excluded_offers: set[str],
    offer_state: dict[str, tuple[float, int]],
) -> None:
    """Handle an outbid event: destroy instance, decide rebid vs next offer.

    Rebids up to ``_MAX_REBIDS_PER_OFFER`` times on the same offer (bumping
    by ``price_step`` each time).  After that, excludes the offer and the
    next iteration of the caller's loop will pick a different machine.
    """
    try:
        provider.destroy_instance(pod_id)
    except Exception:
        pass

    _, outbid_count = offer_state.get(offer_id, (current_bid, 0))
    outbid_count += 1
    next_bid = round(current_bid + mc.price_step, 4)

    if outbid_count < _MAX_REBIDS_PER_OFFER and offer_id not in excluded_offers:
        offer_state[offer_id] = (next_bid, outbid_count)
        log.warn(
            f"Instance {pod_id} was outbid on offer {offer_id} "
            f"(bid ${current_bid:.4f}/hr). "
            f"Rebidding at ${next_bid:.4f}/hr on same offer ... "
            f"(attempt {attempt}, rebid {outbid_count}/{_MAX_REBIDS_PER_OFFER})"
        )
    else:
        # Exhausted rebids on this offer — move to next machine
        excluded_offers.add(offer_id)
        offer_state.pop(offer_id, None)
        log.warn(
            f"Instance {pod_id} was outbid on offer {offer_id} "
            f"(bid ${current_bid:.4f}/hr, {outbid_count} rebid(s)). "
            f"Excluding offer, moving to next machine ... "
            f"(attempt {attempt})"
        )

    time.sleep(5)
