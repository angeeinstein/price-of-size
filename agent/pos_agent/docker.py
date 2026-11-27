"""Minimal async client for the Docker Engine API over the Unix socket.

Only what the agent needs. Unversioned paths are used, so the daemon serves
its own current API version; the agent relies only on long-stable fields.
"""

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class DockerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"docker API {status}: {message}")
        self.status = status


@dataclass
class RegistryAuth:
    username: str
    password: str
    serveraddress: str = "ghcr.io"

    def header(self) -> str:
        raw = json.dumps(
            {
                "username": self.username,
                "password": self.password,
                "serveraddress": self.serveraddress,
            }
        ).encode()
        return base64.urlsafe_b64encode(raw).decode()


class Docker:
    def __init__(
        self,
        socket_path: str = "/var/run/docker.sock",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        registry_auth: RegistryAuth | None = None,
        timeout: float = 60.0,
    ):
        self._http = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://docker",
            timeout=timeout,
        )
        self.registry_auth = registry_auth

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.is_error:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise DockerError(r.status_code, msg)

    async def ping(self) -> bool:
        r = await self._http.get("/_ping")
        return r.status_code == 200

    # --- images -------------------------------------------------------------

    async def pull(self, ref: str, timeout: float = 1800.0) -> None:
        """Pull an image reference (digest-pinned refs are verified by the daemon)."""
        headers = {}
        if self.registry_auth is not None:
            headers["X-Registry-Auth"] = self.registry_auth.header()
        async with self._http.stream(
            "POST",
            "/images/create",
            params={"fromImage": ref},
            headers=headers,
            timeout=timeout,
        ) as r:
            if r.is_error:
                await r.aread()
                self._check(r)
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "error" in msg or "errorDetail" in msg:
                    raise DockerError(500, msg.get("error") or str(msg["errorDetail"]))

    async def image_exists(self, ref: str) -> bool:
        r = await self._http.get(f"/images/{quote(ref, safe='')}/json")
        if r.status_code == 404:
            return False
        self._check(r)
        return True

    # --- containers -----------------------------------------------------------

    async def create(self, name: str, config: dict[str, Any]) -> str:
        r = await self._http.post("/containers/create", params={"name": name}, json=config)
        self._check(r)
        return r.json()["Id"]

    async def start(self, ref: str) -> None:
        r = await self._http.post(f"/containers/{ref}/start")
        if r.status_code != 304:  # 304: already running
            self._check(r)

    async def stop(self, ref: str, timeout_s: int = 30) -> None:
        r = await self._http.post(
            f"/containers/{ref}/stop", params={"t": timeout_s}, timeout=timeout_s + 30
        )
        if r.status_code not in (304, 404):  # already stopped / gone
            self._check(r)

    async def remove(self, ref: str, force: bool = True) -> None:
        r = await self._http.delete(f"/containers/{ref}", params={"force": str(force).lower()})
        if r.status_code != 404:
            self._check(r)

    async def rename(self, ref: str, new_name: str) -> None:
        r = await self._http.post(f"/containers/{ref}/rename", params={"name": new_name})
        self._check(r)

    async def inspect(self, ref: str) -> dict[str, Any] | None:
        r = await self._http.get(f"/containers/{ref}/json")
        if r.status_code == 404:
            return None
        self._check(r)
        return r.json()

    async def wait(self, ref: str, timeout_s: float) -> int:
        r = await self._http.post(f"/containers/{ref}/wait", timeout=timeout_s)
        self._check(r)
        return int(r.json()["StatusCode"])

    async def logs(self, ref: str, tail: int = 200) -> str:
        r = await self._http.get(
            f"/containers/{ref}/logs", params={"stdout": 1, "stderr": 1, "tail": tail}
        )
        self._check(r)
        return _demux(r.content)

    async def list(self, labels: dict[str, str] | None = None, all_: bool = True) -> list[dict]:
        filters = {"label": [f"{k}={v}" for k, v in (labels or {}).items()]}
        r = await self._http.get(
            "/containers/json", params={"all": str(all_).lower(), "filters": json.dumps(filters)}
        )
        self._check(r)
        return r.json()

    # --- networks and volumes -------------------------------------------------

    async def ensure_network(self, name: str) -> None:
        r = await self._http.get(f"/networks/{name}")
        if r.status_code == 200:
            return
        r = await self._http.post("/networks/create", json={"Name": name, "Driver": "bridge"})
        if r.status_code != 409:
            self._check(r)

    async def connect_network(self, network: str, container: str) -> None:
        r = await self._http.post(f"/networks/{network}/connect", json={"Container": container})
        if r.status_code == 403 and "already exists" in r.text:
            return
        self._check(r)


def _demux(raw: bytes) -> str:
    """Docker multiplexes stdout/stderr with 8-byte frame headers when there is no TTY."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1 : i + 4] == b"\0\0\0":
        size = int.from_bytes(raw[i + 4 : i + 8], "big")
        out.append(raw[i + 8 : i + 8 + size])
        i += 8 + size
    if i == 0:
        return raw.decode(errors="replace")
    return b"".join(out).decode(errors="replace")
