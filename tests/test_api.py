import base64

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from pos_coordinator.models import AuditLog, Node
from pos_shared.version import API_VERSION_HEADER
from tests.helpers import new_enrollment_code


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def keypair():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, b64u(pub)


async def enroll(http, admin, key_b64, **kw):
    code = await new_enrollment_code(admin, **kw)
    return await http.post(
        "/api/v1/nodes/enroll",
        json={"enrollment_code": code, "public_key": key_b64, "name": "t", "version": "0.1.0"},
    )


async def login(http, node_id, key) -> str:
    r = await http.post("/api/v1/nodes/challenge", json={"node_id": node_id})
    nonce = r.json()["nonce"]
    r = await http.post(
        "/api/v1/nodes/token",
        json={"node_id": node_id, "nonce": nonce, "signature": b64u(key.sign(nonce.encode()))},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def test_admin_requires_token(http):
    assert (await http.get("/api/admin/nodes")).status_code == 401
    r = await http.get("/api/admin/nodes", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


async def test_api_version_is_enforced(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
        r = await c.post("/api/v1/nodes/challenge", json={"node_id": "x"})
        assert r.status_code == 426
        r = await c.post(
            "/api/v1/nodes/challenge", json={"node_id": "x"}, headers={API_VERSION_HEADER: "2.0"}
        )
        assert r.status_code == 426


async def test_enroll_login_revoke(http, admin, sessionmaker):
    key, pub = keypair()
    r = await enroll(http, admin, pub)
    assert r.status_code == 200, r.text
    node_id = r.json()["node_id"]

    token = await login(http, node_id, key)
    auth = {"Authorization": f"Bearer {token}"}
    hb = {"version": "0.1.0", "state": "running"}
    assert (await http.post("/api/v1/nodes/heartbeat", json=hb, headers=auth)).status_code == 200

    r = await admin.post(f"/api/admin/nodes/{node_id}/revoke")
    assert r.status_code == 200
    assert (await http.post("/api/v1/nodes/heartbeat", json=hb, headers=auth)).status_code == 401
    r = await http.post("/api/v1/nodes/challenge", json={"node_id": node_id})
    assert r.status_code == 403

    async with sessionmaker() as s:
        actions = (await s.execute(select(AuditLog.action))).scalars().all()
    assert "node.revoke" in actions and "enrollment_code.create" in actions


async def test_enrollment_code_is_single_use(http, admin):
    code = await new_enrollment_code(admin)
    _, pub1 = keypair()
    _, pub2 = keypair()
    body = {"enrollment_code": code, "name": "t", "version": "0.1.0"}
    assert (
        await http.post("/api/v1/nodes/enroll", json={**body, "public_key": pub1})
    ).status_code == 200
    r = await http.post("/api/v1/nodes/enroll", json={**body, "public_key": pub2})
    assert r.status_code == 403


async def test_wrong_signature_and_nonce_reuse_rejected(http, admin):
    key, pub = keypair()
    node_id = (await enroll(http, admin, pub)).json()["node_id"]
    other, _ = keypair()

    nonce = (await http.post("/api/v1/nodes/challenge", json={"node_id": node_id})).json()["nonce"]
    bad = {"node_id": node_id, "nonce": nonce, "signature": b64u(other.sign(nonce.encode()))}
    assert (await http.post("/api/v1/nodes/token", json=bad)).status_code == 401
    # the nonce was consumed by the failed attempt
    good = {**bad, "signature": b64u(key.sign(nonce.encode()))}
    assert (await http.post("/api/v1/nodes/token", json=good)).status_code == 401


async def test_strict_schema_rejects_unknown_fields(http, admin):
    key, pub = keypair()
    node_id = (await enroll(http, admin, pub)).json()["node_id"]
    token = await login(http, node_id, key)
    r = await http.post(
        "/api/v1/nodes/heartbeat",
        json={"version": "0.1.0", "state": "running", "sneaky": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


async def test_egress_group_from_proxy_header(app, admin, settings):
    settings.trust_proxy_headers = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://c",
        headers={API_VERSION_HEADER: "1.0", "CF-Connecting-IP": "2001:db8::1234"},
    ) as c:
        _, pub = keypair()
        r = await enroll(c, admin, pub)
    node = (await admin.get("/api/admin/nodes")).json()[0]
    assert node["id"] == r.json()["node_id"]
    assert node["egress_ip"] == "2001:db8::1234"
    assert node["egress_group"] == "2001:db8::/64"


async def test_settings_roundtrip_and_validation(admin):
    r = await admin.get("/api/admin/settings")
    assert r.json()["rate_limit_scope"] == "per_egress_ip"
    r = await admin.put("/api/admin/settings/rate_limit_scope", json={"value": "global"})
    assert r.status_code == 200
    assert (await admin.get("/api/admin/settings")).json()["rate_limit_scope"] == "global"
    r = await admin.put("/api/admin/settings/rate_limit_scope", json={"value": "bogus"})
    assert r.status_code == 422
    assert (await admin.put("/api/admin/settings/nope", json={"value": 1})).status_code == 404


async def test_budget_endpoint(admin):
    r = await admin.get("/api/admin/budget")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ok" and float(body["hard_cap_usd"]) == 10.0


async def test_revoked_nodes_jobs_return_to_queue(http, admin, sessionmaker):
    key, pub = keypair()
    node_id = (await enroll(http, admin, pub)).json()["node_id"]
    token = await login(http, node_id, key)
    await admin.post("/api/admin/jobs", json={"jobs": [{"type": "noop"}] * 3})
    r = await http.post(
        "/api/v1/jobs/lease",
        json={"job_types": ["noop"], "max_jobs": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert len(r.json()["jobs"]) == 3
    r = await admin.post(f"/api/admin/nodes/{node_id}/revoke")
    assert r.json()["released_jobs"] == 3
    stats = (await admin.get("/api/admin/jobs/stats")).json()
    assert stats == [{"type": "noop", "status": "queued", "count": 3}]
    async with sessionmaker() as s:
        assert (await s.execute(select(Node.revoked_at))).scalar_one() is not None


async def test_enrollment_retry_after_lost_response_returns_the_same_node(http, admin):
    """Codex finding: enrollment committed but the response was lost; retrying with
    the same code and key must recover the node instead of stranding it."""
    code = await new_enrollment_code(admin)
    _, pub = keypair()
    body = {"enrollment_code": code, "public_key": pub, "name": "t", "version": "0.1.0"}
    first = await http.post("/api/v1/nodes/enroll", json=body)
    retry = await http.post("/api/v1/nodes/enroll", json=body)
    assert first.status_code == retry.status_code == 200
    assert first.json()["node_id"] == retry.json()["node_id"]
    assert len((await admin.get("/api/admin/nodes")).json()) == 1

    # ...but the used code still cannot enroll a different key
    _, other = keypair()
    r = await http.post("/api/v1/nodes/enroll", json={**body, "public_key": other})
    assert r.status_code == 403


async def test_job_list_retry_cancel_and_audit(admin):
    await admin.post("/api/admin/jobs", json={"jobs": [{"type": "noop"}, {"type": "echo"}]})
    jobs = (await admin.get("/api/admin/jobs", params={"status": "queued"})).json()
    assert {j["type"] for j in jobs} == {"noop", "echo"}
    noop = next(j for j in jobs if j["type"] == "noop")
    assert (
        await admin.post(f"/api/admin/jobs/{noop['id']}", json={"action": "retry"})
    ).status_code == 409
    assert (
        await admin.post(f"/api/admin/jobs/{noop['id']}", json={"action": "cancel"})
    ).status_code == 200
    r = await admin.post(f"/api/admin/jobs/{noop['id']}", json={"action": "retry"})
    assert r.status_code == 200
    again = (await admin.get("/api/admin/jobs", params={"type": "noop"})).json()
    assert again[0]["status"] == "queued" and again[0]["attempts"] == 0
    actions = [a["action"] for a in (await admin.get("/api/admin/audit")).json()]
    assert "job.retry" in actions and "job.cancel" in actions


async def test_no_fetch_jobs_while_the_raw_store_is_away(http, admin, app):
    key, pub = keypair()
    node_id = (await enroll(http, admin, pub)).json()["node_id"]
    auth = {"Authorization": f"Bearer {await login(http, node_id, key)}"}
    await admin.post("/api/admin/jobs", json={"jobs": [{"type": "dummy_fetch"}, {"type": "noop"}]})
    marker = app.state.rawstore.root / ".pos-rawstore"
    saved = marker.read_text()
    marker.unlink()  # the NAS is not mounted
    try:
        assert (await http.get("/healthz")).json()["raw_store"] is False
        r = await http.post(
            "/api/v1/jobs/lease",
            json={"job_types": ["dummy_fetch", "noop"], "max_jobs": 10},
            headers=auth,
        )
        assert [j["type"] for j in r.json()["jobs"]] == ["noop"]
        assert (
            await http.head(f"/api/v1/raw/snippets/{'a' * 64}", headers=auth)
        ).status_code == 503
    finally:
        marker.write_text(saved)
    r = await http.post(
        "/api/v1/jobs/lease", json={"job_types": ["dummy_fetch"], "max_jobs": 10}, headers=auth
    )
    assert [j["type"] for j in r.json()["jobs"]] == ["dummy_fetch"]
