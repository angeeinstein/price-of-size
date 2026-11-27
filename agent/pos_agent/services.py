"""What the agent runs for each role, as Docker container definitions.

worker:       one worker container at a time (two workers with one identity
              would both lease jobs), so the old slot is stopped (drained)
              before the new one starts.
coordinator:  blue-green. The new slot starts beside the old one; Caddy has
              both slots as upstreams with active health checks and the
              `first` policy, so traffic moves once the old slot is stopped.
              Database migrations are expand-only, so the old version keeps
              working on the new schema during the overlap.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from pos_agent.config import AgentSettings
from pos_agent.docker import Docker
from pos_shared import exchange

LOG_CONFIG = {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}
MANAGED_LABEL = "org.price-of-size.managed"
RAW = "/var/lib/pos/raw"


def read_env_file(path: Path) -> list[str]:
    """KEY=VALUE lines -> Docker Env list. Comments and blank lines are skipped."""
    env: list[str] = []
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env.append(f"{key}={value}")
    return env


def volume(source: str, target: str) -> dict[str, str]:
    return {"Type": "volume", "Source": source, "Target": target}


def bind(source: str, target: str) -> dict[str, str]:
    return {"Type": "bind", "Source": source, "Target": target}


def labels(settings: AgentSettings, component: str, slot: str | None, version: str | None):
    out = {MANAGED_LABEL: "true", "org.price-of-size.component": component}
    if slot:
        out["org.price-of-size.slot"] = slot
    if version:
        out["org.price-of-size.version"] = version
    return out


class Service:
    """Base for the two roles."""

    component: str
    stop_old_first: bool
    extra_components: tuple[str, ...] = ()  # other images of a release this role runs

    def __init__(self, settings: AgentSettings, docker: Docker):
        self.settings = settings
        self.docker = docker

    def container_name(self, slot: str) -> str:
        return f"{self.settings.name_prefix}-{self.component}-{slot}"

    def env(self) -> list[str]:
        return read_env_file(self.settings.service_env_file)

    def container_config(self, image: str, slot: str, version: str | None) -> dict[str, Any]:
        raise NotImplementedError

    def selftest_config(self, image: str) -> dict[str, Any]:
        raise NotImplementedError

    async def prepare(self) -> None:
        """Ensure shared infrastructure (networks, proxies) exists."""

    async def pre_switch(self, image: str, version: str, backup: bool = True) -> None:
        """Run before the new slot starts (e.g. backup + migrations)."""

    async def is_healthy(self, slot: str, version: str, since: float) -> bool:
        raise NotImplementedError

    async def run_once(self, name: str, config: dict[str, Any], timeout_s: float) -> str:
        """Run a one-off container to completion; raise with its logs if it fails."""
        await self.docker.remove(name)
        await self.docker.create(name, config)
        try:
            await self.docker.start(name)
            code = await self.docker.wait(name, timeout_s)
            logs = await self.docker.logs(name, tail=100)
        finally:
            await self.docker.remove(name)
        if code != 0:
            raise RuntimeError(f"{name} exited with {code}: {logs[-1500:]}")
        return logs


class WorkerService(Service):
    component = "worker"
    stop_old_first = True
    state_dir = "/var/lib/pos-worker"
    exchange_dir = "/var/lib/pos-worker-exchange"

    def _mounts(self) -> list[dict[str, Any]]:
        return [
            {"Type": "volume", "Source": self.settings.state_volume, "Target": self.state_dir},
            {
                "Type": "volume",
                "Source": self.settings.exchange_volume,
                "Target": self.exchange_dir,
            },
        ]

    def _base(self, image: str) -> dict[str, Any]:
        return {
            "Image": image,
            "Env": [
                *self.env(),
                f"POS_WORKER_STATE_DIR={self.state_dir}",
                f"POS_WORKER_AGENT_DIR={self.exchange_dir}",
            ],
        }

    def container_config(self, image: str, slot: str, version: str | None) -> dict[str, Any]:
        cfg = self._base(image)
        cfg |= {
            "Cmd": ["pos-worker"],
            "Labels": labels(self.settings, self.component, slot, version),
            "StopTimeout": self.settings.drain_timeout_s,
            "HostConfig": {
                "Mounts": self._mounts(),
                "RestartPolicy": {"Name": "unless-stopped"},
                "Init": True,
                "ShmSize": 1 << 30,  # Chromium needs a large /dev/shm
                "LogConfig": LOG_CONFIG,
                "NetworkMode": self._network_mode(),
            },
        }
        return cfg

    def _network_mode(self) -> str:
        # All-in-one installs put the worker on the coordinator's network.
        return self.settings.network if self.settings.worker_on_network else "bridge"

    def selftest_config(self, image: str) -> dict[str, Any]:
        cfg = self._base(image)
        cfg |= {
            "Cmd": ["pos-worker", "selftest"],
            "Labels": labels(self.settings, f"{self.component}-selftest", None, None),
            "HostConfig": {
                "Mounts": self._mounts(),
                "LogConfig": LOG_CONFIG,
                "NetworkMode": self._network_mode(),
            },
        }
        return cfg

    async def is_healthy(self, slot: str, version: str, since: float) -> bool:
        info = await self.docker.inspect(self.container_name(slot))
        if info is None or not info["State"].get("Running"):
            return False
        health = exchange.read_json(self.settings.exchange_dir / exchange.HEALTH)
        return (
            isinstance(health, dict)
            and health.get("version") == version
            and float(health.get("ok_at", 0)) >= since
        )


class CoordinatorService(Service):
    component = "coordinator"
    stop_old_first = False
    extra_components = ("caddy",)
    port = 8000

    def _mounts(self) -> list[dict[str, Any]]:
        s = self.settings
        mounts = [
            {
                "Type": "volume",
                "Source": s.coordinator_data_volume,
                "Target": "/var/lib/pos/coordinator",
            },
            {"Type": "volume", "Source": s.exchange_volume, "Target": "/var/lib/pos-exchange"},
        ]
        if s.host_raw_store:
            mounts.append(
                {"Type": "bind", "Source": s.host_raw_store, "Target": "/var/lib/pos/raw"}
            )
        else:
            mounts.append({"Type": "volume", "Source": "pos-raw", "Target": "/var/lib/pos/raw"})
        if s.host_backup_dir:
            mounts.append({"Type": "bind", "Source": s.host_backup_dir, "Target": "/backups"})
        return mounts

    def _base(self, image: str) -> dict[str, Any]:
        return {
            "Image": image,
            "Env": [
                *self.env(),
                "POS_SIGNING_KEY_PATH=/var/lib/pos/coordinator/signing_key.pem",
                "POS_RAW_STORE_PATH=/var/lib/pos/raw",
                "POS_AGENT_DIR=/var/lib/pos-exchange",
                "POS_BACKUP_DIR=/backups",
            ],
        }

    def _host_config(self, restart: bool) -> dict[str, Any]:
        hc: dict[str, Any] = {
            "Mounts": self._mounts(),
            "Init": True,
            "LogConfig": LOG_CONFIG,
            "NetworkMode": self.settings.network,
        }
        if restart:
            hc["RestartPolicy"] = {"Name": "unless-stopped"}
        return hc

    def container_config(self, image: str, slot: str, version: str | None) -> dict[str, Any]:
        cfg = self._base(image)
        cfg["Env"].append("POS_TRUST_PROXY_HEADERS=true")
        cfg |= {
            "Cmd": ["pos-coordinator", "serve", "--port", str(self.port)],
            "Labels": labels(self.settings, self.component, slot, version),
            "StopTimeout": 30,
            "HostConfig": self._host_config(restart=True),
        }
        return cfg

    def _oneoff(self, image: str, cmd: list[str], component: str) -> dict[str, Any]:
        cfg = self._base(image)
        cfg |= {
            "Cmd": cmd,
            "Labels": labels(self.settings, component, None, None),
            "HostConfig": self._host_config(restart=False),
        }
        return cfg

    def selftest_config(self, image: str) -> dict[str, Any]:
        return self._oneoff(image, ["pos-coordinator", "check"], "coordinator-selftest")

    async def prepare(self) -> None:
        await self.docker.ensure_network(self.settings.network)
        await self.ensure_caddy()

    async def pre_switch(self, image: str, version: str, backup: bool = True) -> None:
        if backup and self.settings.backup_before_migrate:
            await self.run_once(
                f"{self.settings.name_prefix}-coordinator-backup",
                self._oneoff(image, ["pos-coordinator", "backup"], "coordinator-backup"),
                timeout_s=3600,
            )
        await self.run_once(
            f"{self.settings.name_prefix}-coordinator-migrate",
            self._oneoff(image, ["pos-coordinator", "migrate"], "coordinator-migrate"),
            timeout_s=3600,
        )

    async def is_healthy(self, slot: str, version: str, since: float) -> bool:
        url = f"http://{self.container_name(slot)}:{self.port}/healthz"
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(url)
            return r.status_code == 200 and r.json().get("version") == version
        except (httpx.HTTPError, ValueError):
            return False

    # --- reverse proxy --------------------------------------------------------

    def caddy_config(self) -> dict[str, Any]:
        upstreams = [{"dial": f"{self.container_name(s)}:{self.port}"} for s in ("a", "b")]
        return {
            "admin": {"disabled": True},
            "apps": {
                "http": {
                    "servers": {
                        "pos": {
                            "listen": [":80"],
                            "automatic_https": {"disable": True},
                            "routes": [
                                {
                                    "handle": [
                                        {
                                            "handler": "reverse_proxy",
                                            "upstreams": upstreams,
                                            "load_balancing": {
                                                "selection_policy": {"policy": "first"},
                                                "try_duration": "15s",
                                                "try_interval": "250ms",
                                            },
                                            "health_checks": {
                                                "active": {
                                                    "uri": "/healthz",
                                                    "interval": "2s",
                                                    "timeout": "2s",
                                                },
                                                "passive": {"fail_duration": "5s"},
                                            },
                                            "flush_interval": -1,  # stream SSE/WebSocket
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                }
            },
        }

    async def ensure_caddy(self) -> None:
        name = f"{self.settings.name_prefix}-caddy"
        image = self.caddy_image
        if image is None:
            return
        info = await self.docker.inspect(name)
        config_json = json.dumps(self.caddy_config(), sort_keys=True)
        if info is not None:
            env = info["Config"].get("Env") or []
            if info["Config"]["Image"] == image and f"CADDY_CONFIG_JSON={config_json}" in env:
                if not info["State"].get("Running"):
                    await self.docker.start(name)
                return
            await self.docker.remove(name)
        if not await self.docker.image_exists(image):
            await self.docker.pull(image)
        await self.docker.create(
            name,
            {
                "Image": image,
                "Env": [f"CADDY_CONFIG_JSON={config_json}"],
                "Cmd": [
                    "sh",
                    "-c",
                    'printf "%s" "$CADDY_CONFIG_JSON" > /tmp/caddy.json'
                    " && exec caddy run --config /tmp/caddy.json",
                ],
                "Labels": labels(self.settings, "caddy", None, None),
                "ExposedPorts": {"80/tcp": {}},
                "HostConfig": {
                    "NetworkMode": self.settings.network,
                    "PortBindings": {"80/tcp": [{"HostPort": str(self.settings.http_port)}]},
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "LogConfig": LOG_CONFIG,
                },
            },
        )
        await self.docker.start(name)

    caddy_image: str | None = None  # set by the manager from the active manifest


def make_service(settings: AgentSettings, docker: Docker) -> Service:
    if settings.role == "coordinator":
        return CoordinatorService(settings, docker)
    return WorkerService(settings, docker)
