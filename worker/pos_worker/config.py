"""Worker settings. A node needs only the coordinator URL and its credentials;
everything else (parsers, targets, rates, limits) comes from the coordinator.

Local limits set here always win over what the coordinator asks for: whoever
owns the machine and the connection decides.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POS_WORKER_", env_file=".env", extra="ignore")

    coordinator_url: str = "http://localhost:8000"
    enrollment_code: str | None = None
    name: str | None = None
    state_dir: Path = Path("/var/lib/pos-worker")
    # ":memory:" keeps the spool in RAM (Raspberry Pi SD cards); default is state_dir/spool.db
    spool_path: str | None = None
    spool_max_results: int = 50_000

    # Cloudflare Access service token, sent on every request when set.
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None

    # Local caps (None = no local cap).
    max_http_slots: int | None = None
    max_browser_slots: int | None = None

    resource_poll_s: float = 30.0
    flush_interval_s: float = 2.0
    result_batch_size: int = 200
    lease_poll_s: float = 2.0
    request_timeout_s: float = 30.0
    cgroup_root: Path = Path("/sys/fs/cgroup")
