"""RunPod REST API provider (httpx)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from beam import log
from beam.config import RunPodMachineConfig
from beam.providers.base import ProviderBase, ProviderError
from beam.ssh import SSHInfo

RUNPOD_API_BASE = "https://rest.runpod.io/v1"


def _cheapest_price(gpu: dict, spot: bool, cloud_tier: str) -> float:
    """Extract the most relevant price from a GPU type entry."""
    INF = float("inf")
    if spot:
        for key in ("communitySpotPrice", "secureSpotPrice", "spotPrice"):
            val = gpu.get(key)
            if val is not None:
                return float(val)
    tier = cloud_tier.lower()
    if tier == "secure":
        for key in ("securePrice", "secureCloudPrice"):
            val = gpu.get(key)
            if val is not None:
                return float(val)
    for key in ("communityPrice", "communityCloudPrice", "lowestPrice"):
        val = gpu.get(key)
        if val is not None:
            return float(val)
    return INF


class RunPodProvider(ProviderBase):

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        if not self.api_key:
            raise ProviderError(
                "RUNPOD_API_KEY is not set. "
                "Get one at https://www.runpod.io/console/user/settings"
            )
        self._client = httpx.Client(
            base_url=RUNPOD_API_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    # -- low-level API ---------------------------------------------------------

    def _request(self, method: str, path: str, json: dict | None = None) -> tuple[int, dict]:
        resp = self._client.request(method, path, json=json)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return resp.status_code, body

    # -- queries ---------------------------------------------------------------

    def get_pod(self, pod_id: str) -> dict:
        code, body = self._request("GET", f"/pods/{pod_id}")
        if code != 200:
            raise ProviderError(f"GET /pods/{pod_id} returned HTTP {code}: {body}")
        return body

    def list_pods(self, status: str | None = None) -> list[dict]:
        path = "/pods"
        if status:
            path += f"?desiredStatus={status}"
        code, body = self._request("GET", path)
        if code != 200:
            raise ProviderError(f"GET {path} returned HTTP {code}")
        return body if isinstance(body, list) else body.get("pods", [body])

    # -- lifecycle -------------------------------------------------------------

    def create_instance(self, config: dict) -> str:
        log.step("Creating RunPod pod ...")
        log.debug(f"Payload: {config}")
        code, body = self._request("POST", "/pods", json=config)
        if code not in (200, 201):
            msg = body.get("message") or body.get("error") or str(body)
            raise ProviderError(f"Failed to create pod (HTTP {code}): {msg}")
        pod_id = body.get("id", "")
        if not pod_id:
            raise ProviderError(f"No pod ID in response: {body}")
        log.done(f"Pod created: {pod_id}")
        return pod_id

    def start_instance(self, instance_id: str) -> None:
        log.step(f"Starting RunPod pod {instance_id} ...")
        code, body = self._request("POST", f"/pods/{instance_id}/start")
        if code not in (200, 201):
            msg = body.get("message") or body.get("error") or str(body)
            raise ProviderError(f"Failed to start pod (HTTP {code}): {msg}")

    def stop_instance(self, instance_id: str) -> None:
        log.step(f"Stopping RunPod pod {instance_id} ...")
        code, body = self._request("POST", f"/pods/{instance_id}/stop")
        if code not in (200, 201):
            msg = body.get("message") or body.get("error") or str(body)
            raise ProviderError(f"Failed to stop pod (HTTP {code}): {msg}")
        log.done(f"Pod {instance_id} stopped.")

    def destroy_instance(self, instance_id: str) -> None:
        log.step(f"Removing RunPod pod {instance_id} ...")
        code, body = self._request("DELETE", f"/pods/{instance_id}")
        if code not in (200, 204):
            log.warn(f"Remove pod returned HTTP {code}: {body}")

    # -- status ----------------------------------------------------------------

    def get_status(self, instance_id: str) -> str:
        try:
            info = self.get_pod(instance_id)
        except ProviderError:
            return "api_error"
        runtime_status = (info.get("runtime") or {}).get("status", "")
        if runtime_status:
            return runtime_status.lower()
        desired = info.get("desiredStatus", "")
        if desired:
            return desired.lower()
        return "unknown"

    def get_ssh_info(self, instance_id: str) -> SSHInfo | None:
        info = self.get_pod(instance_id)
        ip = info.get("publicIp", "")
        mappings = info.get("portMappings") or {}
        port = mappings.get("22") or (list(mappings.values())[0] if mappings else "")
        if ip and port:
            return SSHInfo(host=ip, port=str(port))
        return None

    # -- polling ---------------------------------------------------------------

    _LOADING_TIMEOUT = 600

    def wait_ready(self, instance_id: str, timeout: int = 300) -> None:
        log.step(f"Waiting for pod {instance_id} to become ready ...")
        elapsed = 0
        interval = 10
        effective_timeout = timeout
        loading_extended = False

        while elapsed < effective_timeout:
            try:
                info = self.get_pod(instance_id)
            except ProviderError:
                log.info(f"  Waiting ... status=api_error ({elapsed}s / {effective_timeout}s)")
                time.sleep(interval)
                elapsed += interval
                continue

            desired = info.get("desiredStatus", "")
            runtime = info.get("runtime") or {}
            runtime_status = runtime.get("status", "")

            if runtime_status and runtime_status.lower() == "running":
                log.done("Pod is running.")
                return

            # Fallback: publicIp + portMappings indicate the container is up
            # (handles REST API responses that lack runtime.status)
            public_ip = info.get("publicIp", "")
            port_mappings = info.get("portMappings") or {}
            if public_ip and port_mappings:
                log.done("Pod is running (SSH info available).")
                return

            if desired in ("EXITED", "TERMINATED"):
                raise ProviderError(
                    f"Pod {instance_id} was interrupted (desiredStatus={desired}). "
                    f"The spot instance may have been reclaimed."
                )

            if desired == "RUNNING" and not loading_extended:
                loading_extended = True
                if effective_timeout < self._LOADING_TIMEOUT:
                    effective_timeout = self._LOADING_TIMEOUT
                    log.info(
                        f"  Pod is initializing (possible docker image pull) — "
                        f"extending timeout to {effective_timeout}s"
                    )

            status_display = runtime_status or f"initializing (desired={desired})"
            log.info(f"  Waiting ... status={status_display} ({elapsed}s / {effective_timeout}s)")
            time.sleep(interval)
            elapsed += interval
        raise ProviderError(f"Pod did not become ready within {effective_timeout}s")

    def wait_ssh_info(self, instance_id: str, timeout: int = 120) -> SSHInfo:
        log.step(f"Retrieving SSH info for pod {instance_id} ...")
        elapsed = 0
        interval = 10
        while elapsed < timeout:
            ssh = self.get_ssh_info(instance_id)
            if ssh:
                log.done(f"SSH ready: {ssh.host}:{ssh.port}")
                return ssh
            log.info(f"  Waiting for port info ... ({elapsed}s / {timeout}s)")
            time.sleep(interval)
            elapsed += interval
        raise ProviderError(f"SSH info not available after {timeout}s")

    # -- GPU type listing ------------------------------------------------------

    def list_gpu_types(
        self, cfg: RunPodMachineConfig | None = None
    ) -> list[dict]:
        """Return available GPU types from the RunPod API.

        If *cfg* is provided, results are filtered by the config's ``gpu_type``
        and ``max_cost`` constraints.  Each returned dict contains at minimum:
        ``id``, ``displayName``, ``memoryInGb``, and pricing fields.
        """
        code, body = self._request("GET", "/gpu-types")
        if code != 200:
            raise ProviderError(f"GET /gpu-types returned HTTP {code}: {body}")

        if isinstance(body, list):
            gpu_types = body
        elif isinstance(body, dict):
            gpu_types = body.get("gpuTypes", body.get("gpu_types", []))
            if not gpu_types and "id" in body:
                gpu_types = [body]
        else:
            gpu_types = []

        if cfg:
            if cfg.gpu_type:
                needle = cfg.gpu_type.lower()
                gpu_types = [
                    t for t in gpu_types
                    if needle in str(t.get("id", "")).lower()
                    or needle in str(t.get("displayName", "")).lower()
                ]
            if cfg.max_cost is not None:
                gpu_types = [
                    t for t in gpu_types
                    if _cheapest_price(t, cfg.spot, cfg.cloud_tier) <= cfg.max_cost
                ]

        gpu_types.sort(key=lambda t: _cheapest_price(t, cfg.spot if cfg else False, cfg.cloud_tier if cfg else "community"))
        return gpu_types

    # -- JSON builder ----------------------------------------------------------

    @staticmethod
    def build_create_payload(cfg: RunPodMachineConfig, ghcr_image: str = "") -> dict:
        """Build the JSON payload for POST /pods from a RunPodMachineConfig."""
        payload: dict = {
            "name": cfg.name or "beam-training",
            "gpuTypeIds": [cfg.gpu_type],
            "gpuCount": cfg.gpu_count,
            "cloudType": cfg.cloud_tier.upper(),
            "containerDiskInGb": cfg.container_disk_size,
        }
        if cfg.spot:
            payload["interruptible"] = True
        image = ghcr_image or cfg.image
        if image:
            payload["imageName"] = image
        if cfg.template_id:
            payload["templateId"] = cfg.template_id
        if cfg.volume_size:
            payload["volumeInGb"] = cfg.volume_size
        if cfg.volume_path:
            payload["volumeMountPath"] = cfg.volume_path
        if cfg.network_volume_id:
            payload["networkVolumeId"] = cfg.network_volume_id
        if cfg.ports:
            payload["ports"] = cfg.ports

        env: dict = {}
        # SSH public keys
        for key_path in cfg.ssh_public_key_files:
            expanded = Path(key_path).expanduser()
            if expanded.is_file():
                content = expanded.read_text().strip()
                existing = env.get("PUBLIC_KEY", "")
                env["PUBLIC_KEY"] = f"{existing}\n{content}".strip() if existing else content

        # Extra env vars
        if cfg.env_vars:
            for pair in cfg.env_vars.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    env[k.strip()] = v.strip()

        if env:
            payload["env"] = env

        return payload
