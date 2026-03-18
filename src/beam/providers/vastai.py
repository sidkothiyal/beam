"""Vast.ai REST API provider (httpx)."""

from __future__ import annotations

import os
import time

import httpx

from beam import log
from beam.config import VastAIMachineConfig
from beam.providers.base import ProviderBase, ProviderError
from beam.ssh import SSHInfo

VAST_API_BASE = "https://console.vast.ai/api/v0"


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
            return body["instances"]
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
        code, _ = self._request("DELETE", f"/instances/{instance_id}/")
        if code not in (200, 204):
            log.warn(f"Destroy instance returned HTTP {code}")

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

    def get_ssh_info(self, instance_id: str) -> SSHInfo | None:
        info = self.get_instance(instance_id)
        host = info.get("ssh_host", "")
        port = info.get("ssh_port", "")
        if host and port:
            return SSHInfo(host=str(host), port=str(port))
        return None

    # -- polling ---------------------------------------------------------------

    def wait_ready(self, instance_id: str, timeout: int = 300) -> None:
        log.step(f"Waiting for instance {instance_id} to become ready ...")
        elapsed = 0
        interval = 10
        while elapsed < timeout:
            status = self.get_status(instance_id)
            if status == "running":
                log.done("Instance is running.")
                return
            log.info(f"  Waiting ... status={status} ({elapsed}s / {timeout}s)")
            time.sleep(interval)
            elapsed += interval
        raise ProviderError(f"Instance did not become ready within {timeout}s")

    def wait_ssh_info(self, instance_id: str, timeout: int = 120) -> SSHInfo:
        log.step(f"Retrieving SSH info for instance {instance_id} ...")
        elapsed = 0
        interval = 10
        while elapsed < timeout:
            ssh = self.get_ssh_info(instance_id)
            if ssh:
                log.done(f"SSH ready: {ssh.host}:{ssh.port}")
                return ssh
            log.info(f"  Waiting for SSH info ... ({elapsed}s / {timeout}s)")
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
    def build_search_body(cfg: VastAIMachineConfig) -> dict:
        """Build the search-offers body."""
        body: dict = {
            "limit": cfg.offer_limit,
            "type": cfg.offer_type,
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
        }
        if cfg.gpu_name:
            body["gpu_name"] = {"eq": cfg.gpu_name}
        if cfg.num_gpus:
            body["num_gpus"] = {"gte": cfg.num_gpus}
        if cfg.machine_id:
            body["machine_id"] = {"eq": cfg.machine_id}
        if cfg.disk_gb:
            body["disk_space"] = {"gte": cfg.disk_gb}
        if cfg.reliability_min is not None:
            body["reliability"] = {"gte": cfg.reliability_min}
        if cfg.dph_max is not None:
            body["dph_total"] = {"lte": cfg.dph_max}
        return body

    def resolve_offer_id(self, cfg: VastAIMachineConfig) -> str:
        """Search for an offer matching the config and return its ID."""
        search_body = self.build_search_body(cfg)
        log.step("Searching for offers ...")
        offers = self.search_offers(search_body)
        if not offers:
            raise ProviderError(
                f"No offers found matching config (gpu_name={cfg.gpu_name})"
            )
        offer_id = str(offers[0].get("id", ""))
        log.done(f"Using offer ID: {offer_id}")
        return offer_id
