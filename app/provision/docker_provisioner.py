from __future__ import annotations

import logging
import time

from app.config import Settings, get_settings
from app.provision import registry
from app.provision.base import GraphInstance


logger = logging.getLogger(__name__)


def container_name(installation_id: int) -> str:
    return f"graphora-falkordb-{installation_id}"


def volume_name(installation_id: int) -> str:
    return f"graphora-data-{installation_id}"


class DockerProvisioner:
    """Provisions one FalkorDB container per installation on a local Docker daemon.

    Idempotent: ``ensure`` reuses an existing container/registry entry when one is
    already running. The registry (installation_id -> host:port) is persisted in
    the shared control-plane FalkorDB so any server instance can resolve it.
    """

    def __init__(self, settings: Settings | None = None, client: object | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def ensure(self, installation_id: int) -> GraphInstance:
        existing = registry.get_instance(installation_id, self.settings)
        if existing is not None and self._container_running(existing.container_name):
            return existing

        name = container_name(installation_id)
        container = self._existing_container(name)
        if container is None:
            logger.info("Provisioning FalkorDB container %s", name)
            container = self.client.containers.run(
                self.settings.falkordb_image,
                name=name,
                detach=True,
                restart_policy={"Name": "unless-stopped"},
                ports={"6379/tcp": None},
                volumes={volume_name(installation_id): {"bind": "/data", "mode": "rw"}},
                labels={"app": "graphora", "installation_id": str(installation_id)},
                network=self.settings.instance_network or None,
            )
        elif container.status != "running":
            container.start()

        host, port = self._endpoint(container)
        instance = GraphInstance(
            installation_id=installation_id,
            host=host,
            port=port,
            container_id=container.id,
            container_name=name,
        )
        registry.save_instance(instance, self.settings)
        return instance

    def get(self, installation_id: int) -> GraphInstance | None:
        return registry.get_instance(installation_id, self.settings)

    def destroy(self, installation_id: int) -> None:
        name = container_name(installation_id)
        container = self._existing_container(name)
        if container is not None:
            logger.info("Destroying FalkorDB container %s", name)
            try:
                container.stop(timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to stop container %s: %s", name, exc)
            container.remove(force=True, v=True)
        registry.delete_instance(installation_id, self.settings)

    def _existing_container(self, name: str):
        from docker.errors import NotFound

        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _container_running(self, name: str | None) -> bool:
        if not name:
            return False
        container = self._existing_container(name)
        return container is not None and container.status == "running"

    def _endpoint(self, container) -> tuple[int, int]:
        host = self.settings.instance_host
        port = self._published_port(container)
        return host, port

    def _published_port(self, container) -> int:
        for _ in range(10):
            container.reload()
            ports = (container.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
            binding = ports.get("6379/tcp")
            if binding:
                return int(binding[0]["HostPort"])
            time.sleep(0.5)
        raise RuntimeError(f"FalkorDB container {container.name} did not publish a host port")
