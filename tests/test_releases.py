"""Signed releases: registration, node delivery, and hand-over to the node agent."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pos_coordinator import maintenance
from pos_coordinator.backup import libpq_env, libpq_url
from pos_shared import exchange, signing
from pos_shared.ids import uuid7
from tests.helpers import make_agent

W = "ghcr.io/x/price-of-size-worker@sha256:" + "1" * 64
A = "ghcr.io/x/price-of-size-agent@sha256:" + "2" * 64


async def _release(admin, version="0.2.0", **images):
    return await admin.post(
        "/api/admin/releases",
        json={"version": version, "images": images or {"worker": W, "agent": A}},
    )


async def test_releases_are_digest_pinned_signed_and_immutable(admin):
    r = await _release(admin)
    assert r.status_code == 201, r.text
    signed = r.json()
    key = (await admin.get("/api/admin/releases/signing-key")).json()["public_key"]
    assert signing.verify(key, signed["manifest"], signed["signature"])
    assert signed["key_id"] == signing.key_id(key)

    assert (await _release(admin)).status_code == 409  # a version is one set of digests forever
    r = await _release(admin, "0.3.0", worker="ghcr.io/x/worker:latest")
    assert r.status_code == 422 and "pinned by digest" in r.text
    r = await _release(admin, "not-a-version")
    assert r.status_code == 422
    listed = (await admin.get("/api/admin/releases")).json()
    assert [x["manifest"]["version"] for x in listed] == ["0.2.0"]


async def test_node_target_must_be_a_known_release(app, admin, tmp_path):
    worker = await make_agent(app, admin, tmp_path / "w", "w")
    node_id = worker.identity.node_id
    r = await admin.patch(f"/api/admin/nodes/{node_id}", json={"target_version": "9.9.9"})
    assert r.status_code == 422
    await _release(admin)
    r = await admin.patch(f"/api/admin/nodes/{node_id}", json={"target_version": "0.2.0"})
    assert r.status_code == 200
    await worker.client.aclose()


async def test_worker_hands_signed_release_to_the_agent(app, admin, tmp_path):
    exch = tmp_path / "exchange"
    worker = await make_agent(app, admin, tmp_path / "w", "w", agent_dir=exch)
    key = (await admin.get("/api/admin/releases/signing-key")).json()["public_key"]
    assert worker.identity.coordinator_signing_key == key  # pinned at enrollment

    await worker.heartbeat_once()
    assert (exch / exchange.PINNED_KEY).read_text().strip() == key
    assert exchange.read_json(exch / exchange.HEALTH)["version"] == "0.1.0"
    assert not (exch / exchange.DESIRED).exists()

    await _release(admin)
    await admin.patch(
        f"/api/admin/nodes/{worker.identity.node_id}", json={"target_version": "0.2.0"}
    )
    await worker.heartbeat_once()
    desired = exchange.read_json(exch / exchange.DESIRED)
    assert desired["manifest"]["version"] == "0.2.0"
    assert signing.verify(key, desired["manifest"], desired["signature"])

    # The agent's status comes back to the coordinator with the next heartbeat.
    exchange.write_json(
        exch / exchange.STATUS,
        {
            "agent_version": "0.1.0",
            "active_slot": "a",
            "running_version": "0.1.0",
            "update_state": "health_check",
            "update_target": "0.2.0",
            "unknown_key": 1,
        },
    )
    await worker.heartbeat_once()
    node = (await admin.get("/api/admin/nodes")).json()[0]
    assert node["agent_status"]["update_state"] == "health_check"
    await worker.client.aclose()


async def test_coordinator_syncs_its_own_agent_exchange(app, admin, tmp_path):
    exch = tmp_path / "coord-exchange"
    sm, key = app.state.sessionmaker, app.state.signing_key
    await maintenance.run_once(sm, exch, key)
    assert (exch / exchange.PINNED_KEY).read_text().strip() == signing.public_key_b64(key)
    assert not (exch / exchange.DESIRED).exists()

    await _release(admin, coordinator="ghcr.io/x/c@sha256:" + "3" * 64, agent=A)
    await admin.put("/api/admin/settings/coordinator.target_version", json={"value": "0.2.0"})
    await maintenance.run_once(sm, exch, key)
    assert exchange.read_json(exch / exchange.DESIRED)["manifest"]["version"] == "0.2.0"

    await admin.put("/api/admin/settings/coordinator.target_version", json={"value": None})
    await maintenance.run_once(sm, exch, key)
    assert not (exch / exchange.DESIRED).exists()  # mirrors the database


def test_backup_connection_settings():
    # Built at runtime: a credential-shaped literal would (rightly) trip secret scanners.
    secret = "".join(["not", "-a-", "real", "@", "secret"])
    quoted = secret.replace("@", "%40")
    url = f"postgresql+psycopg://user:{quoted}@db.invalid:6543/pos?sslmode=require"
    assert libpq_url(url) == f"postgresql://user:{quoted}@db.invalid:6543/pos?sslmode=require"
    assert libpq_env(url) == {
        "PGHOST": "db.invalid",
        "PGPORT": "6543",
        "PGDATABASE": "pos",
        "PGUSER": "user",
        "PGPASSWORD": secret,
        "PGSSLMODE": "require",
    }


async def test_clearing_the_target_cancels_a_pending_update(app, admin, tmp_path):
    """Codex finding: a cleared target must remove the manifest the agent would act on."""
    exch = tmp_path / "exchange"
    worker = await make_agent(app, admin, tmp_path / "w", "w", agent_dir=exch)
    await _release(admin)
    node = f"/api/admin/nodes/{worker.identity.node_id}"
    await admin.patch(node, json={"target_version": "0.2.0"})
    await worker.heartbeat_once()
    assert (exch / exchange.DESIRED).exists()
    await admin.patch(node, json={"target_version": None})
    await worker.heartbeat_once()
    assert not (exch / exchange.DESIRED).exists()
    await worker.client.aclose()


async def test_identity_without_signing_key_fetches_it(app, admin, tmp_path):
    """Codex finding: identities enrolled before keys came with enrollment must not be
    locked out of signed updates."""
    import json as _json

    exch = tmp_path / "exchange"
    worker = await make_agent(app, admin, tmp_path / "w", "w", agent_dir=exch)
    ident = tmp_path / "w" / "identity.json"
    ident.write_text(_json.dumps({"node_id": str(worker.identity.node_id)}))  # old format
    worker.identity.coordinator_signing_key = None
    await worker.heartbeat_once()
    key = (await admin.get("/api/admin/releases/signing-key")).json()["public_key"]
    assert worker.identity.coordinator_signing_key == key
    assert _json.loads(ident.read_text())["coordinator_signing_key"] == key
    assert (exch / exchange.PINNED_KEY).read_text().strip() == key
    await worker.client.aclose()


def test_coordinator_replies_tolerate_newer_fields():
    """Codex finding: a newer coordinator (same API major) may add reply fields; an
    older node must keep working, and node input stays strict."""
    import pydantic
    import pytest

    from pos_shared import api

    resp = api.EnrollResponse.model_validate(
        {"node_id": str(uuid7()), "coordinator_api_version": "1.9", "added_later": True}
    )
    assert resp.signing_public_key is None  # an older coordinator: fetched later
    hb = api.HeartbeatResponse.model_validate(
        {
            "desired": {"run_state": "running", "new_knob": 1, "limits": {"x": 2}},
            "commands": [{"id": str(uuid7()), "type": "ping", "priority": 5}],
            "coordinator_api_version": "1.9",
            "server_time": "2026-11-27T00:00:00Z",
            "new_section": {},
        }
    )
    assert hb.desired.run_state == api.RunState.running
    with pytest.raises(pydantic.ValidationError):
        api.Heartbeat.model_validate({"version": "0.1.0", "state": "running", "sneaky": 1})


def test_signed_manifest_is_verified_as_received():
    """The signature covers the coordinator's JSON; fields this node does not know
    must neither break verification nor let a modified manifest through."""
    key = Ed25519PrivateKey.generate()
    pub = signing.public_key_b64(key)
    manifest = {
        "version": "0.2.0",
        "images": {"worker": "r/w@sha256:" + "a" * 64},
        "created_at": "2026-11-27T00:00:00Z",
        "added_in_a_later_release": {"rollout": "canary"},
    }
    raw = {"manifest": manifest, "signature": signing.sign(key, manifest), "key_id": "k"}
    parsed, problem = signing.open_signed_manifest(raw, pub)
    assert problem is None and parsed.version == "0.2.0"
    tampered = {**raw, "manifest": {**manifest, "images": {"worker": "evil@sha256:" + "b" * 64}}}
    assert signing.open_signed_manifest(tampered, pub) == (None, "not signed by the pinned key")
    other = signing.public_key_b64(Ed25519PrivateKey.generate())
    assert signing.open_signed_manifest(raw, other)[0] is None
    assert signing.open_signed_manifest(raw, None)[0] is None
    assert signing.open_signed_manifest({"manifest": "x"}, pub)[0] is None
