from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings

STATE_GRAPH_NAME = "graph:graphreview:state"


def init_state(settings: Settings | None = None) -> None:
    graph = _select_state_graph(settings or get_settings())
    graph.query(
        """
        MERGE (state:GraphReviewState {id: 'global'})
        SET state.updated_at = $updated_at
        """,
        {"updated_at": _now()},
    )


def review_already_processed(owner: str, repo: str, pr_number: int, head_sha: str, settings: Settings | None = None) -> bool:
    graph = _select_state_graph(settings or get_settings())
    return _exists(
        graph,
        """
        MATCH (review:PRReview {id: $id, status: 'posted'})
        RETURN review.id
        LIMIT 1
        """,
        {"id": _review_id(owner, repo, pr_number, head_sha)},
    )


def ci_debug_already_processed(
    owner: str,
    repo: str,
    workflow_run_id: int,
    head_sha: str,
    settings: Settings | None = None,
) -> bool:
    graph = _select_state_graph(settings or get_settings())
    return _exists(
        graph,
        """
        MATCH (debug:CIDebug {id: $id, status: 'posted'})
        RETURN debug.id
        LIMIT 1
        """,
        {"id": _ci_debug_id(owner, repo, workflow_run_id, head_sha)},
    )


def mark_review_posted(owner: str, repo: str, pr_number: int, head_sha: str, settings: Settings | None = None) -> None:
    graph = _select_state_graph(settings or get_settings())
    graph.query(
        """
        MERGE (review:PRReview {id: $id})
        SET review.owner = $owner,
            review.repo = $repo,
            review.pr_number = $pr_number,
            review.head_sha = $head_sha,
            review.status = 'posted',
            review.updated_at = $updated_at
        """,
        {
            "id": _review_id(owner, repo, pr_number, head_sha),
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "updated_at": _now(),
        },
    )


def mark_ci_debug_posted(
    owner: str,
    repo: str,
    pr_number: int,
    workflow_run_id: int,
    head_sha: str,
    settings: Settings | None = None,
) -> None:
    graph = _select_state_graph(settings or get_settings())
    graph.query(
        """
        MERGE (debug:CIDebug {id: $id})
        SET debug.owner = $owner,
            debug.repo = $repo,
            debug.pr_number = $pr_number,
            debug.workflow_run_id = $workflow_run_id,
            debug.head_sha = $head_sha,
            debug.status = 'posted',
            debug.updated_at = $updated_at
        """,
        {
            "id": _ci_debug_id(owner, repo, workflow_run_id, head_sha),
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "workflow_run_id": workflow_run_id,
            "head_sha": head_sha,
            "updated_at": _now(),
        },
    )


def mark_graph_build(owner: str, repo: str, graph_scope: str, status: str, source_count: int, settings: Settings | None = None) -> None:
    graph = _select_state_graph(settings or get_settings())
    graph.query(
        """
        MERGE (build:GraphBuild {id: $id})
        SET build.owner = $owner,
            build.repo = $repo,
            build.graph_scope = $graph_scope,
            build.status = $status,
            build.source_count = $source_count,
            build.updated_at = $updated_at
        """,
        {
            "id": _graph_build_id(owner, repo, graph_scope),
            "owner": owner,
            "repo": repo,
            "graph_scope": graph_scope,
            "status": status,
            "source_count": source_count,
            "updated_at": _now(),
        },
    )


def list_graph_builds(settings: Settings | None = None) -> list[dict]:
    graph = _select_state_graph(settings or get_settings())
    result = graph.query(
        """
        MATCH (build:GraphBuild)
        RETURN build.owner, build.repo, build.graph_scope,
               build.status, build.source_count, build.updated_at
        ORDER BY build.updated_at DESC
        """
    )
    rows = getattr(result, "result_set", result) or []
    builds = []
    for row in rows:
        builds.append(
            {
                "owner": row[0],
                "repo": row[1],
                "graph_scope": row[2],
                "status": row[3],
                "source_count": row[4],
                "updated_at": row[5],
            }
        )
    return builds


def set_review_paused(owner: str, repo: str, paused: bool, settings: Settings | None = None) -> None:
    graph = _select_state_graph(settings or get_settings())
    graph.query(
        """
        MERGE (config:RepoConfig {id: $id})
        SET config.owner = $owner,
            config.repo = $repo,
            config.review_paused = $paused,
            config.updated_at = $updated_at
        """,
        {
            "id": _repo_config_id(owner, repo),
            "owner": owner,
            "repo": repo,
            "paused": paused,
            "updated_at": _now(),
        },
    )


def is_review_paused(owner: str, repo: str, settings: Settings | None = None) -> bool:
    graph = _select_state_graph(settings or get_settings())
    result = graph.query(
        """
        MATCH (config:RepoConfig {id: $id})
        RETURN config.review_paused
        LIMIT 1
        """,
        {"id": _repo_config_id(owner, repo)},
    )
    rows = getattr(result, "result_set", result) or []
    if not rows:
        return False
    return bool(rows[0][0])


def mark_delivery(event: str, delivery_id: str, settings: Settings | None = None) -> bool:
    graph = _select_state_graph(settings or get_settings())
    delivery_key = _delivery_id(event, delivery_id)
    if _exists(
        graph,
        """
        MATCH (delivery:WebhookDelivery {id: $id})
        RETURN delivery.id
        LIMIT 1
        """,
        {"id": delivery_key},
    ):
        return False

    graph.query(
        """
        MERGE (delivery:WebhookDelivery {id: $id})
        SET delivery.event = $event,
            delivery.delivery_id = $delivery_id,
            delivery.processed_at = $processed_at
        """,
        {
            "id": delivery_key,
            "event": event,
            "delivery_id": delivery_id,
            "processed_at": _now(),
        },
    )
    return True


def _select_state_graph(settings: Settings):
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise RuntimeError("falkordb is required for GraphReview state storage") from exc

    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    return db.select_graph(STATE_GRAPH_NAME)


def _exists(graph: Any, query: str, params: dict[str, Any]) -> bool:
    result = graph.query(query, params)
    rows = getattr(result, "result_set", result)
    return bool(rows)


def _review_id(owner: str, repo: str, pr_number: int, head_sha: str) -> str:
    return f"{owner}/{repo}:pr:{pr_number}:review:{head_sha}"


def _ci_debug_id(owner: str, repo: str, workflow_run_id: int, head_sha: str) -> str:
    return f"{owner}/{repo}:ci:{workflow_run_id}:debug:{head_sha}"


def _graph_build_id(owner: str, repo: str, graph_scope: str) -> str:
    return f"{owner}/{repo}:graph:{graph_scope}"


def _repo_config_id(owner: str, repo: str) -> str:
    return f"{owner}/{repo}:config"


def _delivery_id(event: str, delivery_id: str) -> str:
    return f"{event}:{delivery_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
