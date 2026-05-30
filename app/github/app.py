from __future__ import annotations

import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

import jwt
import requests

if TYPE_CHECKING:
    from github import Github

from app.config import Settings, get_settings


GITHUB_API = "https://api.github.com"


def _read_private_key(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"GitHub private key file does not exist: {path}")
    return path.read_text(encoding="utf-8")


def create_app_jwt(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if not settings.github_app_id:
        raise RuntimeError("GITHUB_APP_ID is required to create a GitHub App JWT")

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 9 * 60,
        "iss": settings.github_app_id,
    }
    return jwt.encode(payload, _read_private_key(settings.github_private_key_path), algorithm="RS256")


def create_installation_token(installation_id: int, settings: Settings | None = None) -> str:
    app_jwt = create_app_jwt(settings)
    response = requests.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Could not create installation token: {response.status_code} {response.text}")
    data: dict[str, Any] = response.json()
    return data["token"]


def get_github_client(installation_id: int, settings: Settings | None = None) -> "Github":
    from github import Github

    return Github(create_installation_token(installation_id, settings))
