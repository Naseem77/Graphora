from pathlib import Path

from docker.errors import NotFound

from app.config import Settings
from app.provision import docker_provisioner, registry
from app.provision.base import GraphInstance


def _settings():
    return Settings(
        github_app_id="1",
        github_private_key_path=Path("/tmp/key.pem"),
        github_webhook_secret="s",
        azure_api_key="",
        azure_api_base="",
        azure_api_version="",
        review_model="",
        per_installation_db=True,
        falkordb_image="falkordb/falkordb:latest",
        instance_host="127.0.0.1",
    )


class FakeContainer:
    def __init__(self, name, status="running", host_port=7001):
        self.id = "cid-" + name
        self.name = name
        self.status = status
        self._host_port = host_port
        self.started = False
        self.stopped = False
        self.removed = False

    def start(self):
        self.started = True
        self.status = "running"

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False, v=False):
        self.removed = True

    def reload(self):
        pass

    @property
    def attrs(self):
        return {"NetworkSettings": {"Ports": {"6379/tcp": [{"HostPort": str(self._host_port)}]}}}


class FakeContainers:
    def __init__(self):
        self.store = {}
        self.run_calls = []

    def get(self, name):
        if name not in self.store:
            raise NotFound(name)
        return self.store[name]

    def run(self, image, name=None, **kwargs):
        self.run_calls.append((image, name, kwargs))
        container = FakeContainer(name)
        self.store[name] = container
        return container


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers()


def _patch_registry(monkeypatch):
    saved = {}

    monkeypatch.setattr(registry, "save_instance", lambda instance, settings=None: saved.update(instance=instance))
    monkeypatch.setattr(registry, "get_instance", lambda installation_id, settings=None: saved.get("get"))
    monkeypatch.setattr(registry, "delete_instance", lambda installation_id, settings=None: saved.update(deleted=installation_id))
    return saved


def test_ensure_creates_container_and_registers(monkeypatch):
    saved = _patch_registry(monkeypatch)
    monkeypatch.setattr(docker_provisioner.registry, "get_instance", lambda installation_id, settings=None: None)
    monkeypatch.setattr(docker_provisioner.registry, "save_instance", lambda instance, settings=None: saved.update(instance=instance))

    client = FakeClient()
    provisioner = docker_provisioner.DockerProvisioner(_settings(), client=client)

    instance = provisioner.ensure(123)

    assert instance.host == "127.0.0.1"
    assert instance.port == 7001
    assert instance.container_name == "graphora-falkordb-123"
    assert client.containers.run_calls[0][1] == "graphora-falkordb-123"
    assert saved["instance"].installation_id == 123


def test_ensure_reuses_running_instance(monkeypatch):
    existing = GraphInstance(123, "127.0.0.1", 7001, "cid", "graphora-falkordb-123")
    monkeypatch.setattr(docker_provisioner.registry, "get_instance", lambda installation_id, settings=None: existing)
    save_calls = []
    monkeypatch.setattr(docker_provisioner.registry, "save_instance", lambda instance, settings=None: save_calls.append(instance))

    client = FakeClient()
    client.containers.store["graphora-falkordb-123"] = FakeContainer("graphora-falkordb-123", status="running")
    provisioner = docker_provisioner.DockerProvisioner(_settings(), client=client)

    instance = provisioner.ensure(123)

    assert instance is existing
    assert client.containers.run_calls == []
    assert save_calls == []


def test_destroy_stops_removes_and_deregisters(monkeypatch):
    deleted = []
    monkeypatch.setattr(docker_provisioner.registry, "delete_instance", lambda installation_id, settings=None: deleted.append(installation_id))

    client = FakeClient()
    container = FakeContainer("graphora-falkordb-123")
    client.containers.store["graphora-falkordb-123"] = container
    provisioner = docker_provisioner.DockerProvisioner(_settings(), client=client)

    provisioner.destroy(123)

    assert container.stopped is True
    assert container.removed is True
    assert deleted == [123]
