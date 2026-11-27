"""HTTPS client for the coordinator API: auth, version header, token refresh."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from pos_shared import api
from pos_shared.version import API_VERSION, API_VERSION_HEADER, __version__, is_compatible
from pos_worker.config import WorkerSettings
from pos_worker.identity import Identity

log = logging.getLogger(__name__)


class CoordinatorError(Exception):
    pass


class IncompatibleCoordinator(CoordinatorError):
    pass


class NotEnrolled(CoordinatorError):
    pass


class CoordinatorClient:
    def __init__(
        self,
        settings: WorkerSettings,
        identity: Identity,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.identity = identity
        headers = {API_VERSION_HEADER: API_VERSION, "User-Agent": f"pos-worker/{__version__}"}
        if settings.cf_access_client_id and settings.cf_access_client_secret:
            headers["CF-Access-Client-Id"] = settings.cf_access_client_id
            headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret
        self._http = httpx.AsyncClient(
            base_url=settings.coordinator_url.rstrip("/"),
            headers=headers,
            timeout=settings.request_timeout_s,
            transport=transport,
        )
        self._token: str | None = None
        self._token_expires: datetime | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- auth ------------------------------------------------------------

    async def enroll(self, code: str, name: str, capabilities: list[str]) -> api.EnrollResponse:
        body = api.EnrollRequest(
            enrollment_code=code,
            public_key=self.identity.public_key_b64,
            name=name,
            version=__version__,
            capabilities=capabilities,
        )
        r = await self._http.post("/api/v1/nodes/enroll", json=body.model_dump(mode="json"))
        self._raise(r)
        return api.EnrollResponse.model_validate(r.json())

    async def _authenticate(self) -> None:
        if self.identity.node_id is None:
            raise NotEnrolled("node has no id; enroll first")
        r = await self._http.post(
            "/api/v1/nodes/challenge", json={"node_id": str(self.identity.node_id)}
        )
        if r.status_code == 403:
            raise NotEnrolled("coordinator does not know this node (revoked?)")
        self._raise(r)
        nonce = api.ChallengeResponse.model_validate(r.json()).nonce
        r = await self._http.post(
            "/api/v1/nodes/token",
            json={
                "node_id": str(self.identity.node_id),
                "nonce": nonce,
                "signature": self.identity.sign(nonce.encode()),
            },
        )
        if r.status_code == 401:
            raise NotEnrolled("challenge rejected (revoked or key mismatch)")
        self._raise(r)
        tok = api.TokenResponse.model_validate(r.json())
        self._token, self._token_expires = tok.access_token, tok.expires_at

    async def _ensure_token(self) -> str:
        if (
            self._token is None
            or self._token_expires is None
            or self._token_expires - datetime.now(UTC) < timedelta(seconds=60)
        ):
            await self._authenticate()
        assert self._token is not None
        return self._token

    # --- requests --------------------------------------------------------

    @staticmethod
    def _raise(r: httpx.Response) -> None:
        if r.status_code == 426:
            raise IncompatibleCoordinator(r.text)
        if r.is_error:
            raise CoordinatorError(
                f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:500]}"
            )

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            token = await self._ensure_token()
            r = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kw
            )
            if r.status_code == 401 and attempt == 1:
                self._token = None  # token revoked or expired early: re-authenticate once
                continue
            self._raise(r)
            return r
        raise AssertionError("unreachable")

    async def check_auth(self) -> None:
        """Authenticate and check versions without changing any node state."""
        r = await self._http.get("/api/v1/version")
        self._raise(r)
        theirs = r.json()["api_version"]
        if not is_compatible(API_VERSION, theirs):
            raise IncompatibleCoordinator(f"coordinator speaks {theirs}, we speak {API_VERSION}")
        self._token = None
        await self._ensure_token()

    async def get_signing_key(self) -> str:
        r = await self._call("GET", "/api/v1/nodes/signing-key")
        return r.json()["public_key"]

    async def get_release(self, version: str) -> dict:
        """The signed manifest as received: its signature covers these exact values."""
        r = await self._call("GET", f"/api/v1/releases/{version}")
        return r.json()

    async def heartbeat(self, hb: api.Heartbeat) -> api.HeartbeatResponse:
        r = await self._call("POST", "/api/v1/nodes/heartbeat", json=hb.model_dump(mode="json"))
        return api.HeartbeatResponse.model_validate(r.json())

    async def lease(self, job_types: list[str], max_jobs: int) -> list[api.LeasedJob]:
        body = api.LeaseRequest(job_types=job_types, max_jobs=max_jobs)
        r = await self._call("POST", "/api/v1/jobs/lease", json=body.model_dump(mode="json"))
        return api.LeaseResponse.model_validate(r.json()).jobs

    async def extend(self, leases: list[api.LeaseRef]) -> api.ExtendResponse:
        body = api.ExtendRequest(leases=leases)
        r = await self._call("POST", "/api/v1/jobs/extend", json=body.model_dump(mode="json"))
        return api.ExtendResponse.model_validate(r.json())

    async def push_results(self, results: list[dict[str, Any]]) -> api.ResultAck:
        r = await self._call("POST", "/api/v1/jobs/results", json={"results": results})
        return api.ResultAck.model_validate(r.json())

    async def upload_raw(self, kind: str, sha256: str, data: bytes) -> None:
        head = await self._call_allow_404("HEAD", f"/api/v1/raw/{kind}/{sha256}")
        if head.status_code == 200:
            return
        await self._call("PUT", f"/api/v1/raw/{kind}/{sha256}", content=data)

    async def _call_allow_404(self, method: str, path: str) -> httpx.Response:
        token = await self._ensure_token()
        r = await self._http.request(method, path, headers={"Authorization": f"Bearer {token}"})
        if r.status_code not in (200, 404):
            self._raise(r)
        return r
