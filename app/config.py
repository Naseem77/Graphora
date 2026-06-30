from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    github_app_id: str
    github_private_key_path: Path
    github_webhook_secret: str
    azure_api_key: str
    azure_api_base: str
    azure_api_version: str
    review_model: str
    falkordb_host: str = "falkordb"
    falkordb_port: int = 6379
    per_installation_db: bool = False
    falkordb_image: str = "falkordb/falkordb:latest"
    instance_network: str | None = None
    instance_host: str = "127.0.0.1"
    s3_bucket: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    bot_login: str = "graphreview"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            github_app_id=os.getenv("GITHUB_APP_ID", ""),
            github_private_key_path=Path(os.getenv("GITHUB_PRIVATE_KEY_PATH", "./private-key.pem")),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
            azure_api_key=os.getenv("AZURE_API_KEY", ""),
            azure_api_base=os.getenv("AZURE_API_BASE", ""),
            azure_api_version=os.getenv("AZURE_API_VERSION", "2024-02-15-preview"),
            review_model=os.getenv("REVIEW_MODEL", ""),
            falkordb_host=os.getenv("FALKORDB_HOST", "falkordb"),
            falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
            per_installation_db=os.getenv("GRAPHORA_PER_INSTALL_DB", "").lower() in {"1", "true", "yes"},
            falkordb_image=os.getenv("FALKORDB_IMAGE", "falkordb/falkordb:latest"),
            instance_network=os.getenv("GRAPHORA_INSTANCE_NETWORK") or None,
            instance_host=os.getenv("GRAPHORA_INSTANCE_HOST", "127.0.0.1"),
            s3_bucket=os.getenv("S3_BUCKET") or None,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY") or None,
            bot_login=os.getenv("GRAPHREVIEW_BOT_LOGIN", "graphreview"),
        )

    def require_github_app(self) -> None:
        missing = [
            name
            for name, value in (
                ("GITHUB_APP_ID", self.github_app_id),
                ("GITHUB_PRIVATE_KEY_PATH", str(self.github_private_key_path)),
                ("GITHUB_WEBHOOK_SECRET", self.github_webhook_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required GitHub App settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
