from __future__ import annotations

import dataclasses
import logging

from app.config import Settings, get_settings
from app.provision import registry
from app.provision.base import GraphInstance, GraphProvisioner


logger = logging.getLogger(__name__)

_provisioner: GraphProvisioner | None = None


def get_provisioner(settings: Settings | None = None) -> GraphProvisioner | None:
    """Return the configured provisioner, or None when per-installation DBs are off."""
    global _provisioner
    settings = settings or get_settings()
    if not settings.per_installation_db:
        return None
    if _provisioner is None:
        from app.provision.docker_provisioner import DockerProvisioner

        _provisioner = DockerProvisioner(settings)
    return _provisioner


def set_provisioner(provisioner: GraphProvisioner | None) -> None:
    """Override the provisioner (used by tests)."""
    global _provisioner
    _provisioner = provisioner


def settings_for_installation(installation_id: int, settings: Settings | None = None) -> Settings:
    """Return Settings pointed at the installation's own FalkorDB.

    In single-tenant mode (default) the base settings are returned unchanged, so
    every graph operation uses the shared FalkorDB exactly as before. In
    per-installation mode the instance is provisioned (or looked up) and the
    FalkorDB host/port are overridden for that installation.
    """
    settings = settings or get_settings()
    provisioner = get_provisioner(settings)
    if provisioner is None:
        return settings

    instance = provisioner.get(installation_id) or provisioner.ensure(installation_id)
    return _with_endpoint(settings, instance)


def provision_installation(installation_id: int, settings: Settings | None = None) -> Settings:
    """Ensure the installation's FalkorDB exists and return Settings targeting it."""
    settings = settings or get_settings()
    provisioner = get_provisioner(settings)
    if provisioner is None:
        return settings
    instance = provisioner.ensure(installation_id)
    logger.info("Installation %s using FalkorDB at %s:%s", installation_id, instance.host, instance.port)
    return _with_endpoint(settings, instance)


def destroy_installation(installation_id: int, settings: Settings | None = None) -> None:
    """Tear down the installation's FalkorDB instance, if any."""
    settings = settings or get_settings()
    provisioner = get_provisioner(settings)
    if provisioner is None:
        registry.delete_instance(installation_id, settings)
        return
    provisioner.destroy(installation_id)


def _with_endpoint(settings: Settings, instance: GraphInstance) -> Settings:
    return dataclasses.replace(settings, falkordb_host=instance.host, falkordb_port=instance.port)
