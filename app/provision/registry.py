from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings
from app.provision.base import GraphInstance
from app.state import STATE_GRAPH_NAME


def _state_graph(settings: Settings):
    try:
        from falkordb import FalkorDB
    except ImportError as exc:  # pragma: no cover - falkordb always present at runtime
        raise RuntimeError("falkordb is required for the installation registry") from exc

    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    return db.select_graph(STATE_GRAPH_NAME)


def save_instance(instance: GraphInstance, settings: Settings | None = None) -> None:
    graph = _state_graph(settings or get_settings())
    graph.query(
        """
        MERGE (instance:Installation {id: $id})
        SET instance.installation_id = $installation_id,
            instance.host = $host,
            instance.port = $port,
            instance.container_id = $container_id,
            instance.container_name = $container_name,
            instance.updated_at = $updated_at
        """,
        {
            "id": str(instance.installation_id),
            "installation_id": instance.installation_id,
            "host": instance.host,
            "port": instance.port,
            "container_id": instance.container_id,
            "container_name": instance.container_name,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def get_instance(installation_id: int, settings: Settings | None = None) -> GraphInstance | None:
    graph = _state_graph(settings or get_settings())
    result = graph.query(
        """
        MATCH (instance:Installation {id: $id})
        RETURN instance.installation_id, instance.host, instance.port,
               instance.container_id, instance.container_name
        LIMIT 1
        """,
        {"id": str(installation_id)},
    )
    rows = _rows(result)
    if not rows:
        return None
    installation, host, port, container_id, container_name = rows[0]
    return GraphInstance(
        installation_id=int(installation),
        host=str(host),
        port=int(port),
        container_id=container_id,
        container_name=container_name,
    )


def delete_instance(installation_id: int, settings: Settings | None = None) -> None:
    graph = _state_graph(settings or get_settings())
    graph.query(
        "MATCH (instance:Installation {id: $id}) DETACH DELETE instance",
        {"id": str(installation_id)},
    )


def _rows(result: Any) -> list:
    return getattr(result, "result_set", result) or []
