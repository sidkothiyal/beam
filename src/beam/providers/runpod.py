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

    def wait_ready(self, instance_id: str, timeout: int = 300) -> None:
        log.step(f"Waiting for pod {instance_id} to become ready ...")
        elapsed = 0
        interval = 10
        while elapsed < timeout:
            status = self.get_status(instance_id)
            if status == "running":
                log.done("Pod is running.")
                return
            log.info(f"  Waiting ... status={status} ({elapsed}s / {timeout}s)")
            time.sleep(interval)
            elapsed += interval
        raise ProviderError(f"Pod did not become ready within {timeout}s")

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
