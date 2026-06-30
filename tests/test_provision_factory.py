from pathlib import Path

from app.config import Settings
from app.provision import factory
from app.provision.base import GraphInstance


def _settings(per_installation_db=False):
    return Settings(
        github_app_id="1",
        github_private_key_path=Path("/tmp/key.pem"),
        github_webhook_secret="s",
        azure_api_key="",
        azure_api_base="",
        azure_api_version="",
        review_model="",
        falkordb_host="shared",
        falkordb_port=6379,
        per_installation_db=per_installation_db,
        instance_host="127.0.0.1",
    )


class FakeProvisioner:
    def __init__(self):
        self.ensured = []
        self.destroyed = []
        self._known = {}

    def ensure(self, installation_id):
        self.ensured.append(installation_id)
        instance = GraphInstance(installation_id, "127.0.0.1", 7001, "cid", "name")
        self._known[installation_id] = instance
        return instance

    def get(self, installation_id):
        return self._known.get(installation_id)

    def destroy(self, installation_id):
        self.destroyed.append(installation_id)


def teardown_function():
    factory.set_provisioner(None)


def test_settings_for_installation_single_tenant_returns_base():
    base = _settings(per_installation_db=False)
    result = factory.settings_for_installation(42, base)
    assert result is base


def test_settings_for_installation_overrides_endpoint(monkeypatch):
    base = _settings(per_installation_db=True)
    fake = FakeProvisioner()
    factory.set_provisioner(fake)

    result = factory.settings_for_installation(42, base)

    assert result.falkordb_host == "127.0.0.1"
    assert result.falkordb_port == 7001
    assert fake.ensured == [42]


def test_settings_for_installation_reuses_known_instance():
    base = _settings(per_installation_db=True)
    fake = FakeProvisioner()
    fake._known[42] = GraphInstance(42, "10.0.0.1", 8000)
    factory.set_provisioner(fake)

    result = factory.settings_for_installation(42, base)

    assert (result.falkordb_host, result.falkordb_port) == ("10.0.0.1", 8000)
    assert fake.ensured == []


def test_provision_installation_calls_ensure():
    base = _settings(per_installation_db=True)
    fake = FakeProvisioner()
    factory.set_provisioner(fake)

    factory.provision_installation(7, base)

    assert fake.ensured == [7]


def test_destroy_installation_calls_destroy():
    base = _settings(per_installation_db=True)
    fake = FakeProvisioner()
    factory.set_provisioner(fake)

    factory.destroy_installation(7, base)

    assert fake.destroyed == [7]
