from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from app.config import Settings, get_settings


logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 600
DEFAULT_BLOCKING_TIMEOUT = 600


def _redis_connection(settings: Settings):
    from falkordb import FalkorDB

    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    return db.connection


def lock_key(owner: str, repo: str, suffix: str = "main") -> str:
    return f"graphora:lock:graph:{owner}:{repo}:{suffix}"


@contextmanager
def graph_write_lock(
    owner: str,
    repo: str,
    suffix: str = "main",
    settings: Settings | None = None,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    blocking_timeout: float = DEFAULT_BLOCKING_TIMEOUT,
) -> Iterator[None]:
    """Serialize writes to a single FalkorDB graph across threads and processes.

    FalkorDB has no MVCC, so concurrent writers to the same graph (e.g. two PRs
    merging into ``:main`` at once) must be ordered. Backed by a Redis lock on the
    same FalkorDB connection. Degrades to a no-op if Redis is unreachable (e.g. in
    unit tests where graph writes are mocked).
    """
    settings = settings or get_settings()
    key = lock_key(owner, repo, suffix)
    lock = None
    try:
        connection = _redis_connection(settings)
        lock = connection.lock(key, timeout=lock_timeout, blocking_timeout=blocking_timeout)
        if not lock.acquire():
            logger.warning(
                "Timed out acquiring graph write lock %s after %ss; proceeding without it",
                key,
                blocking_timeout,
            )
            lock = None
    except Exception as exc:  # noqa: BLE001 - best-effort lock; never block real writes
        logger.debug("Graph write lock %s unavailable: %s", key, exc)
        lock = None

    try:
        yield
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to release graph write lock %s: %s", key, exc)
