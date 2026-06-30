from app.graph import locks


class FakeLock:
    def __init__(self, key, acquired=True):
        self.key = key
        self._acquired = acquired
        self.released = False

    def acquire(self):
        return self._acquired

    def release(self):
        self.released = True


class FakeConnection:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.locks = []

    def lock(self, key, timeout=None, blocking_timeout=None):
        lock = FakeLock(key, self.acquired)
        self.locks.append(lock)
        return lock


def _settings():
    from app.config import Settings
    from pathlib import Path

    return Settings(
        github_app_id="1",
        github_private_key_path=Path("/tmp/key.pem"),
        github_webhook_secret="s",
        azure_api_key="",
        azure_api_base="",
        azure_api_version="",
        review_model="",
    )


def test_lock_acquires_and_releases(monkeypatch):
    connection = FakeConnection(acquired=True)
    monkeypatch.setattr(locks, "_redis_connection", lambda settings: connection)

    with locks.graph_write_lock("octo", "repo", "main", _settings()):
        pass

    assert len(connection.locks) == 1
    lock = connection.locks[0]
    assert lock.key == "graphora:lock:graph:octo:repo:main"
    assert lock.released is True


def test_lock_noop_when_redis_unreachable(monkeypatch):
    def boom(settings):
        raise ConnectionError("no redis")

    monkeypatch.setattr(locks, "_redis_connection", boom)

    entered = False
    with locks.graph_write_lock("octo", "repo", "pr:5", _settings()):
        entered = True
    assert entered is True


def test_lock_releases_even_when_body_raises(monkeypatch):
    connection = FakeConnection(acquired=True)
    monkeypatch.setattr(locks, "_redis_connection", lambda settings: connection)

    try:
        with locks.graph_write_lock("octo", "repo", settings=_settings()):
            raise ValueError("boom")
    except ValueError:
        pass

    assert connection.locks[0].released is True
