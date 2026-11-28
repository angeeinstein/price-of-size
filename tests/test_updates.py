"""One-click updates: the GitHub check, starting an update, and the rollout
(coordinator, then one node, then the rest)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from pos_coordinator import runtime_settings, updates
from pos_coordinator.backup import _prune
from pos_coordinator.models import AuditLog, Node, Release, UpdateRollout
from pos_shared import exchange
from pos_shared.version import __version__

REPO = "owner/price-of-size"
NEW = "9.9.9"


def _images(n: int = 1, repo: str = "ghcr.io/owner/price-of-size") -> dict[str, str]:
    return {c: f"{repo}-{c}@sha256:{str(n) * 64}" for c in ("coordinator", "worker", "agent")} | {
        "caddy": f"caddy@sha256:{'c' * 64}"
    }


def github(releases: dict[str, dict], status: int = 200):
    """A fake GitHub: tag -> {"assets": bool, "release_json": dict}; the newest first."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        tags = list(releases)
        if path == f"/repos/{REPO}/releases/latest":
            tag = tags[0] if tags else None
        elif path.startswith(f"/repos/{REPO}/releases/tags/"):
            tag = path.rsplit("/", 1)[1]
        elif path.startswith("/download/"):
            return httpx.Response(200, json=releases[path.split("/")[2]]["release_json"])
        else:
            return httpx.Response(404)
        if status != 200:
            return httpx.Response(status)
        if tag not in releases:
            return httpx.Response(404, json={"message": "Not Found"})
        r = releases[tag]
        assets = (
            [{"name": "release.json", "browser_download_url": f"https://dl.test/download/{tag}"}]
            if r.get("assets", True)
            else []
        )
        return httpx.Response(
            200,
            json={
                "tag_name": tag,
                "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
                "body": "What changed",
                "published_at": "2026-11-28T02:31:05Z",
                "assets": assets,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def release(version: str = NEW, images: dict | None = None, **kw) -> dict:
    return {"release_json": {"version": version, "images": images or _images()}, **kw}


@pytest.fixture
def agent_dir(app, tmp_path):
    d = tmp_path / "agent"
    d.mkdir()
    app.state.settings.agent_dir = d
    app.state.settings.update_repository = REPO
    return d


async def _node(sm, name, version, *, online=True, managed=True, agent=None) -> uuid.UUID:
    async with sm() as s:
        n = Node(
            name=name,
            public_key=uuid.uuid4().bytes * 2,
            version=version,
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=5 if online else 3600),
            agent_status=(agent or {"running_version": version, "update_state": "idle"})
            if managed
            else None,
        )
        s.add(n)
        await s.commit()
        return n.id


def test_versions_are_ordered_as_releases():
    key = updates.version_key
    assert key("0.1.10") > key("0.1.9") > key("0.1.2")
    assert key("1.0.0") > key("1.0.0-rc2") > key("1.0.0-rc1") > key("0.9.9")
    assert key("not a version") < key("0.0.0")


async def test_the_check_finds_the_latest_release_and_reads_its_manifest():
    http, seen = github({f"v{NEW}": release()})
    result = await updates.check(REPO, http)
    assert result.error is None
    assert result.latest.version == NEW and result.latest.images == _images()
    assert result.latest.notes == "What changed"
    assert seen == [
        f"https://api.github.com/repos/{REPO}/releases/latest",
        f"https://dl.test/download/v{NEW}",
    ]
    assert "images" not in result.view()["latest"]


@pytest.mark.parametrize(
    "releases, says",
    [
        ({}, "no release"),
        ({f"v{NEW}": release(assets=False)}, "no release.json yet"),
        ({f"v{NEW}": release(version="9.9.8")}, "release.json says"),
        ({"latest-build": release()}, "is not a version"),
        (
            {f"v{NEW}": release(images=_images() | {"worker": "ghcr.io/x/worker:latest"})},
            "pinned by digest",
        ),
        (
            {f"v{NEW}": release(images={"coordinator": _images()["coordinator"]})},
            "no image for worker, agent",
        ),
    ],
)
async def test_the_check_says_what_is_wrong(releases, says):
    http, _ = github(releases)
    result = await updates.check(REPO, http)
    assert result.latest is None and says in result.error, result.error


async def test_github_unreachable_or_checks_off():
    http, _ = github({}, status=503)
    assert "503" in (await updates.check(REPO, http)).error
    assert "off" in (await updates.check("")).error


async def test_check_now_and_the_overview(app, admin, agent_dir):
    app.state.update_http, _ = github({f"v{NEW}": release()})
    r = await admin.get("/api/admin/updates")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == __version__ and body["check"] is None
    assert body["update_available"] is False and body["self_update"] is True
    r = await admin.post("/api/admin/updates/check")
    body = r.json()
    assert body["update_available"] is True
    assert body["check"]["latest"]["version"] == NEW
    assert body["check"]["latest"]["url"].endswith(f"/v{NEW}")


async def test_an_update_registers_signs_and_hands_the_release_to_the_agent(
    app, admin, agent_dir, sessionmaker
):
    app.state.update_http, _ = github({f"v{NEW}": release()})
    r = await admin.post("/api/admin/updates/start", json={"version": NEW})
    assert r.status_code == 200, r.text
    (rollout,) = r.json()["rollouts"]
    assert rollout["status"] == "coordinator" and rollout["from_version"] == __version__
    async with sessionmaker() as s:
        rel = await s.get(Release, NEW)
        assert rel.manifest["images"] == _images() and rel.notes == "What changed"
        assert await runtime_settings.get(s, "coordinator.target_version") == NEW
        actions = (await s.execute(select(AuditLog.action))).scalars().all()
        assert {"release.create", "update.start"} <= set(actions)
    desired = exchange.read_json(agent_dir / exchange.DESIRED)
    assert desired["manifest"]["version"] == NEW and desired["signature"] == rel.signature
    # one at a time
    r = await admin.post("/api/admin/updates/start", json={"version": NEW})
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    # cancelled: may start again
    assert (await admin.post("/api/admin/updates/cancel")).status_code == 200
    assert (await admin.post("/api/admin/updates/cancel")).status_code == 409


async def test_a_registered_release_needs_no_github(app, admin, agent_dir, sessionmaker):
    from pos_coordinator.api.releases import register

    async with sessionmaker() as s:
        register(s, app.state.signing_key, NEW, _images(7), None, "admin")
        await s.commit()
    app.state.update_http, seen = github({})
    r = await admin.post("/api/admin/updates/start", json={"version": NEW})
    assert r.status_code == 200, r.text
    assert seen == []


@pytest.mark.parametrize(
    "version, says",
    [("0.0.1", "older than the running"), (__version__, "everything already runs")],
)
async def test_what_an_update_refuses(app, admin, agent_dir, version, says):
    app.state.update_http, _ = github({})
    r = await admin.post("/api/admin/updates/start", json={"version": version})
    assert r.status_code == 409 and says in r.json()["detail"], r.text


async def test_no_self_update_without_an_agent(app, admin):
    app.state.settings.agent_dir = None
    r = await admin.post("/api/admin/updates/start", json={"version": NEW})
    assert r.status_code == 409 and "install.sh" in r.json()["detail"]


async def test_an_update_never_moves_images_to_another_repository(
    app, admin, agent_dir, sessionmaker
):
    from pos_coordinator.api.releases import register

    async with sessionmaker() as s:  # the running release, from the usual repository
        register(s, app.state.signing_key, __version__, _images(1), None, "admin")
        await s.commit()
    app.state.update_http, _ = github(
        {f"v{NEW}": release(images=_images(2, repo="ghcr.io/someone-else/pos"))}
    )
    r = await admin.post("/api/admin/updates/start", json={"version": NEW})
    assert r.status_code == 409 and "would move" in r.json()["detail"]
    async with sessionmaker() as s:
        assert await s.get(Release, NEW) is None


async def test_the_viewer_sees_but_cannot_start(app, sessionmaker, agent_dir):
    from pos_coordinator.panel import auth
    from tests.test_panel_auth import browser, login, make_user

    app.state.settings.cookie_secure = False  # plain http in the test client
    await make_user(sessionmaker, name="vera", role="viewer")
    async with browser(app) as c:
        csrf = (await login(c, "vera")).json()["csrf_token"]
        assert (await c.get("/api/admin/updates")).status_code == 200
        r = await c.post(
            "/api/admin/updates/start", json={"version": NEW}, headers={auth.CSRF_HEADER: csrf}
        )
        assert r.status_code == 403


# --- the rollout -----------------------------------------------------------------


async def _start(sm, key, agent_dir, version=NEW):
    from pos_coordinator.api.releases import register

    async with sm() as s:
        if await s.get(Release, version) is None:
            register(s, key, version, _images(), None, "admin")
        await updates.start(
            s, key=key, agent_dir=agent_dir, repository=REPO, version=version, actor="admin"
        )
        await s.commit()


async def _advance(sm, agent_dir, own=__version__):
    async with sm() as s:
        status = await updates.advance(s, agent_dir, 90, own_version=own)
        await s.commit()
        return status


async def _targets(sm) -> dict[str, str | None]:
    async with sm() as s:
        return {n.name: n.desired_target_version for n in (await s.execute(select(Node))).scalars()}


async def _switched(sm, node_id, version=NEW):
    """The node's worker runs the version and its agent has recorded the switch."""
    await _set(sm, node_id, version=version,
               agent_status={"running_version": version, "update_state": "idle"})  # fmt: skip


async def _set(sm, node_id, **fields):
    async with sm() as s:
        n = await s.get(Node, node_id)
        for k, v in fields.items():
            setattr(n, k, v)
        await s.commit()


async def test_coordinator_first_then_one_node_then_the_rest(app, agent_dir, sessionmaker):
    sm, key = sessionmaker, app.state.signing_key
    a = await _node(sm, "a", __version__)
    b = await _node(sm, "b", __version__)
    await _node(sm, "c", __version__, online=False)  # gets the target, does not hold up
    await _node(sm, "simple", __version__, managed=False)  # no agent: never targeted
    await _start(sm, key, agent_dir)
    # the old coordinator still answers: nodes wait
    assert await _advance(sm, agent_dir) == "coordinator"
    assert set((await _targets(sm)).values()) == {None}
    # the new coordinator answers: the first online node goes first
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    assert await _targets(sm) == {"a": NEW, "b": None, "c": None, "simple": None}
    assert await _advance(sm, agent_dir, own=NEW) == "canary"  # still switching
    await _switched(sm, a)
    assert await _advance(sm, agent_dir, own=NEW) == "nodes"
    assert await _targets(sm) == {"a": NEW, "b": NEW, "c": NEW, "simple": None}
    await _switched(sm, b)
    assert await _advance(sm, agent_dir, own=NEW) == "done"  # c is offline: updates later
    assert await _advance(sm, agent_dir, own=NEW) is None
    async with sm() as s:
        (r,) = (await s.execute(select(UpdateRollout))).scalars().all()
        assert r.finished_at is not None and r.error is None


async def test_the_nodes_wait_until_the_coordinator_agent_has_switched(
    app, agent_dir, sessionmaker
):
    await _node(sessionmaker, "a", __version__)
    await _start(sessionmaker, app.state.signing_key, agent_dir)
    status = agent_dir / exchange.STATUS
    # the new version answers, but its agent is still checking it (and might roll back)
    exchange.write_json(status, {"running_version": __version__, "update_state": "health_check"})
    assert await _advance(sessionmaker, agent_dir, own=NEW) == "coordinator"
    exchange.write_json(status, {"running_version": NEW, "update_state": "idle"})
    assert await _advance(sessionmaker, agent_dir, own=NEW) == "canary"


async def test_the_others_wait_until_the_first_node_agent_has_switched(
    app, agent_dir, sessionmaker
):
    """The new worker reports its version before its agent has finished checking it
    (and might still roll back): that is not yet a node on the new version."""
    sm = sessionmaker
    a = await _node(sm, "a", __version__)
    await _node(sm, "b", __version__)
    await _start(sm, app.state.signing_key, agent_dir)
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    checking = {"running_version": __version__, "update_state": "health_check"}
    await _set(sm, a, version=NEW, agent_status=checking)
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    assert (await _targets(sm))["b"] is None
    await _switched(sm, a)
    assert await _advance(sm, agent_dir, own=NEW) == "nodes"


async def test_a_failed_coordinator_update_ends_the_rollout(app, agent_dir, sessionmaker):
    await _node(sessionmaker, "a", __version__)
    await _start(sessionmaker, app.state.signing_key, agent_dir)
    exchange.write_json(
        agent_dir / exchange.STATUS,
        {"update_state": "failed", "update_target": NEW, "update_error": "migrate exited 1"},
    )
    assert await _advance(sessionmaker, agent_dir) == "failed"
    assert set((await _targets(sessionmaker)).values()) == {None}  # nodes never touched
    async with sessionmaker() as s:
        (r,) = (await s.execute(select(UpdateRollout))).scalars().all()
        assert r.error == "coordinator: migrate exited 1"


async def test_a_failed_first_node_stops_the_others(app, agent_dir, sessionmaker):
    sm = sessionmaker
    a = await _node(sm, "a", __version__)
    await _node(sm, "b", __version__)
    await _start(sm, app.state.signing_key, agent_dir)
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    await _set(
        sm, a,
        agent_status={"update_state": "failed", "update_target": NEW,
                      "update_error": "did not become healthy in time"},
    )  # fmt: skip
    assert await _advance(sm, agent_dir, own=NEW) == "failed"
    assert (await _targets(sm))["b"] is None
    async with sm() as s:
        (r,) = (await s.execute(select(UpdateRollout))).scalars().all()
        assert r.error == "a: did not become healthy in time"


async def test_failed_nodes_among_the_rest_are_reported(app, agent_dir, sessionmaker):
    sm = sessionmaker
    a = await _node(sm, "a", __version__)
    b = await _node(sm, "b", __version__)
    await _start(sm, app.state.signing_key, agent_dir)
    await _advance(sm, agent_dir, own=NEW)
    await _switched(sm, a)
    assert await _advance(sm, agent_dir, own=NEW) == "nodes"
    await _set(sm, b, agent_status={"update_state": "failed", "update_target": NEW,
                                     "update_error": "pull failed"})  # fmt: skip
    assert await _advance(sm, agent_dir, own=NEW) == "failed"
    async with sm() as s:
        (r,) = (await s.execute(select(UpdateRollout))).scalars().all()
        assert r.error == "b: pull failed"


async def test_no_online_node_goes_straight_to_all(app, agent_dir, sessionmaker):
    await _node(sessionmaker, "away", __version__, online=False)
    await _start(sessionmaker, app.state.signing_key, agent_dir)
    assert await _advance(sessionmaker, agent_dir, own=NEW) == "done"
    assert await _targets(sessionmaker) == {"away": NEW}


async def test_a_revoked_canary_is_replaced(app, agent_dir, sessionmaker):
    sm = sessionmaker
    a = await _node(sm, "a", __version__)
    await _node(sm, "b", __version__)
    await _start(sm, app.state.signing_key, agent_dir)
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    await _set(sm, a, revoked_at=datetime.now(UTC))
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    async with sm() as s:
        (r,) = (await s.execute(select(UpdateRollout))).scalars().all()
        assert (await s.get(Node, r.canary_node_id)).name == "b"


async def test_a_canary_that_left_agent_mode_is_replaced(app, agent_dir, sessionmaker):
    """A node switched to simple mode reports no agent: it cannot follow the target,
    so the rollout must not wait for it."""
    sm = sessionmaker
    a = await _node(sm, "a", __version__)
    await _node(sm, "b", __version__)
    await _start(sm, app.state.signing_key, agent_dir)
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    await _set(sm, a, agent_status=None)
    assert await _advance(sm, agent_dir, own=NEW) == "canary"
    async with sm() as s:
        (r,) = (await s.execute(select(UpdateRollout))).scalars().all()
        assert (await s.get(Node, r.canary_node_id)).name == "b"


async def test_the_nodes_can_be_updated_when_the_coordinator_already_is(
    app, admin, agent_dir, sessionmaker
):
    """Starting again for the version the coordinator runs rolls it out to the nodes."""
    from pos_coordinator.api.releases import register

    async with sessionmaker() as s:
        register(s, app.state.signing_key, __version__, _images(), None, "admin")
        await s.commit()
    await _node(sessionmaker, "behind", "0.0.1")
    await _node(sessionmaker, "current", __version__)
    assert (await admin.get("/api/admin/updates")).json()["nodes_behind"] == 1
    r = await admin.post("/api/admin/updates/start", json={"version": __version__})
    assert r.status_code == 200, r.text
    assert await _advance(sessionmaker, agent_dir) == "canary"
    assert await _targets(sessionmaker) == {"behind": __version__, "current": None}


async def test_the_overview_shows_the_steps(app, admin, agent_dir, sessionmaker):
    await _node(sessionmaker, "a", __version__)
    await _start(sessionmaker, app.state.signing_key, agent_dir)
    exchange.write_json(
        agent_dir / exchange.STATUS,
        {"running_version": __version__, "update_state": "migrating", "update_target": NEW},
    )
    body = (await admin.get("/api/admin/updates")).json()
    assert body["coordinator_agent"]["update_state"] == "migrating"
    assert body["rollouts"][0]["status"] == "coordinator"
    (node,) = body["nodes"]
    assert node["name"] == "a" and node["managed"] and node["online"]
    json.dumps(body)  # serialisable as it is


# --- backups before updates ------------------------------------------------------


def test_only_dumps_before_updates_are_pruned_together(tmp_path):
    names = [
        "pos-20261101T000000Z-before-0.1.0.dump",
        "pos-20261102T000000Z-before-0.1.1.dump",
        "pos-20261103T000000Z-manual.dump",
        "pos-20261104T000000Z-before-0.1.2.dump",
        "pos-20261105T000000Z-before-0.1.3.dump",
    ]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    _prune(tmp_path, 2, "before-0.1.3")
    assert sorted(p.name for p in tmp_path.iterdir()) == [names[2], names[3], names[4]]


def test_no_migrations_pending_at_head(settings):
    from pos_coordinator import schema
    from tests.conftest import alembic_config

    assert schema.pending(alembic_config(), settings.database_url) is False
