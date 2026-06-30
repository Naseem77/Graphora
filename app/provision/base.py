from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GraphInstance:
    """A FalkorDB endpoint dedicated to a single GitHub App installation."""

    installation_id: int
    host: str
    port: int
    container_id: str | None = None
    container_name: str | None = None


class GraphProvisioner(Protocol):
    """Provisions and tears down per-installation FalkorDB instances.

    Implementations back this with a concrete platform (local Docker today,
    Kubernetes or a cloud orchestrator later). The rest of the app depends only
    on this interface, so the platform can change without touching webhook or
    graph code.
    """

    def ensure(self, installation_id: int) -> GraphInstance:
        """Return a ready FalkorDB instance for the installation, creating it if needed."""
        ...

    def get(self, installation_id: int) -> GraphInstance | None:
        """Return the known instance for the installation, or None if not provisioned."""
        ...

    def destroy(self, installation_id: int) -> None:
        """Tear down the installation's FalkorDB instance and forget it."""
        ...
