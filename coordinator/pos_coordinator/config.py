"""Static settings (environment / .env). Runtime switches live in the `setting` table."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://pos:pos@localhost:5432/pos"
    # Every request uses at most one pooled connection; size for nodes x concurrency.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: float = 30.0
    raw_store_path: Path = Path("/var/lib/pos/raw")
    signing_key_path: Path = Path("/var/lib/pos/coordinator/signing_key.pem")
    # Exchange directory shared with the coordinator's own agent (A/B updater).
    agent_dir: Path | None = None
    backup_dir: Path | None = None

    # Bootstrap admin token for the admin API until panel user accounts exist.
    admin_token: str | None = Field(None, min_length=32)

    # Honour CF-Connecting-IP / X-Forwarded-For when behind Cloudflare Tunnel or Caddy.
    trust_proxy_headers: bool = False
    # Session cookies only over HTTPS (Cloudflare). Off only for local development.
    cookie_secure: bool = True
    panel_dist: Path | None = None  # built control panel; auto-detected if unset

    access_token_ttl_s: int = 15 * 60
    challenge_ttl_s: int = 60
    enrollment_code_ttl_s: int = 24 * 3600
    default_job_lease_s: int = 300
    max_raw_object_bytes: int = 50 * 1024 * 1024
    reaper_interval_s: float = 15.0
    derive_interval_s: float = 10.0
    node_offline_after_s: int = 90
    # The Claude API key (only the coordinator calls the API). Unset: LLM stages idle.
    anthropic_api_key: SecretStr | None = None
    llm_poll_interval_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
