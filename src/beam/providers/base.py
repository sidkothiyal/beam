"""Abstract provider interface for cloud GPU providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from beam.ssh import SSHInfo


class ProviderError(Exception):
    pass


class ProviderBase(ABC):
    """Interface that every cloud GPU provider must implement."""

    @abstractmethod
    def create_instance(self, config: dict) -> str:
        """Create a new instance. Returns instance/pod ID."""
        ...

    @abstractmethod
    def start_instance(self, instance_id: str) -> None:
        """Start a stopped instance."""
        ...

    @abstractmethod
    def stop_instance(self, instance_id: str) -> None:
        """Stop (but keep) an instance."""
        ...

    @abstractmethod
    def destroy_instance(self, instance_id: str) -> None:
        """Permanently destroy an instance."""
        ...

    @abstractmethod
    def get_status(self, instance_id: str) -> str:
        """Return a normalised status string (running, stopped, exited, unknown, error)."""
        ...

    @abstractmethod
    def get_ssh_info(self, instance_id: str) -> SSHInfo | None:
        """Return SSH connection info, or None if not yet available."""
        ...

    @abstractmethod
    def wait_ready(self, instance_id: str, timeout: int = 300) -> None:
        """Block until the instance reaches running status."""
        ...

    @abstractmethod
    def wait_ssh_info(self, instance_id: str, timeout: int = 120) -> SSHInfo:
        """Block until SSH info is available; return it."""
        ...
