"""Ensure a remote machine is running and reachable via SSH."""

from __future__ import annotations

import time

from beam import log
from beam.config import (
    BeamConfig,
    RunPodMachineConfig,
    VastAIMachineConfig,
)
from beam.providers.base import ProviderBase, ProviderError
from beam.providers.runpod import RunPodProvider
from beam.providers.vastai import VastAIProvider
from beam.ssh import SSHClient, SSHInfo


class MachineResult:
    """Outcome of ensure_machine: connection info + provider state."""

    def __init__(
        self,
        ssh_info: SSHInfo,
        pod_id: str = "",
        provider_name: str = "",
    ) -> None:
        self.ssh_info = ssh_info
        self.pod_id = pod_id
        self.provider_name = provider_name


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


def _get_provider(name: str) -> ProviderBase:
    if name == "runpod":
        return RunPodProvider()
    if name in ("vast.ai", "vastai"):
        return VastAIProvider()
    raise RuntimeError(f"Unknown provider: {name} (supported: runpod, vast.ai)")


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
    provider = _get_provider(provider_name)
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
    if not client.wait_for_ssh():
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
    provider = _get_provider(provider_name)
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
            if not client.wait_for_ssh():
                raise RuntimeError("SSH not reachable after start")
            return MachineResult(ssh_info=ssh_info, pod_id=pod_id, provider_name=provider_name)

    # Create new
    configs = _resolve_fallback_configs(cfg, provider_name, pod_config_name)

    for i, machine_cfg in enumerate(configs):
        try:
            new_pod_id, ssh_info = _create_one(
                provider, provider_name, machine_cfg, ghcr_image
            )
            ssh_info.user = cfg.remote.user
            ssh_info.identity_file = cfg.ssh.identity_file
            client = SSHClient(info=ssh_info, cfg=cfg.ssh)
            client.remove_from_known_hosts()
            if not client.wait_for_ssh():
                raise RuntimeError("SSH not reachable after create")
            return MachineResult(
                ssh_info=ssh_info,
                pod_id=new_pod_id,
                provider_name=provider_name,
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
) -> tuple[str, SSHInfo]:
    """Create a single instance and return (pod_id, ssh_info)."""
    # Vast.ai needs offer resolution first
    if provider_name in ("vast.ai", "vastai"):
        assert isinstance(provider, VastAIProvider)
        mc: VastAIMachineConfig = create_cfg["_machine_cfg"]
        offer_id = provider.resolve_offer_id(mc)
        create_cfg["offer_id"] = offer_id
        if ghcr_image:
            create_cfg["payload"]["image"] = ghcr_image

    pod_id = provider.create_instance(create_cfg)
    provider.wait_ready(pod_id)
    ssh_info = provider.wait_ssh_info(pod_id)
    return pod_id, ssh_info
