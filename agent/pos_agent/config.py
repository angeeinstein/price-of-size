"""Agent settings (environment, set once by install.sh). Everything that changes
at runtime comes from coordinator-signed manifests via the exchange volume."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POS_AGENT_", extra="ignore")

    role: Literal["worker", "coordinator"] = "worker"
    docker_socket: str = "/var/run/docker.sock"
    data_dir: Path = Path("/data")  # private to the agent: pinned key, state, lock
    exchange_dir: Path = Path("/exchange")  # shared with the service
    # Docker volume names, used when creating service containers.
    exchange_volume: str = "pos-worker-exchange"
    state_volume: str = "pos-worker-state"
    agent_data_volume: str = "pos-agent-data"
    # Env file for the service, as seen inside the agent (host /etc/pos is mounted read-only).
    service_env_file: Path = Path("/etc/pos/worker.env")

    # release.json of the installed version (from install.sh): images for the first start.
    # Not coordinator-signed; every later version must be.
    bootstrap_release: Path = Path("/etc/pos/release.json")
    self_image: str | None = None  # the image this agent runs, digest-pinned
    container_name: str = "pos-agent"  # this agent's own container
    name_prefix: str = "pos"
    worker_on_network: bool = False  # all-in-one: worker joins the coordinator network
    backup_before_migrate: bool = True

    check_interval_s: float = 10.0
    selftest_timeout_s: float = 180.0
    drain_timeout_s: int = 120
    health_timeout_s: float = 180.0
    failed_retry_after_s: float = 6 * 3600
    agent_health_timeout_s: float = 120.0

    # Coordinator role
    network: str = "pos-net"
    coordinator_data_volume: str = "pos-coordinator-data"
    host_raw_store: str | None = None  # host path (NAS mount) for the raw store
    host_backup_dir: str | None = None  # host path for pre-migration backups
    http_port: int = 8080  # host port Caddy publishes (cloudflared points here)
    switch_grace_s: float = 10.0

    # Optional registry credentials (private GHCR packages)
    registry_username: str | None = None
    registry_password: str | None = None
    registry_server: str = "ghcr.io"
