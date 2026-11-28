"""Control panel accounts: sessions, CSRF, roles, re-authentication, TOTP, rate limits."""

import time
from datetime import UTC, datetime, timedelta

import httpx
import pyotp
import pytest
from sqlalchemy import func, select, update

from pos_coordinator.app import create_app
from pos_coordinator.models import AppUser, RawFetch, UserSession
from pos_coordinator.panel import auth
from pos_shared.ids import uuid7

PW = "correct horse battery staple"


@pytest.fixture
async def panel_app(settings):
    app = create_app(settings.model_copy(update={"cookie_secure": False}), run_maintenance=False)
    async with app.router.lifespan_context(app):
        yield app


def browser(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel")


async def make_user(sm, name="alice", role="admin", password=PW, totp=None):
    async with sm() as s:
        u = AppUser(
            username=name, password_hash=auth.hash_password(password), role=role, totp_secret=totp
        )
        s.add(u)
        await s.commit()
        return u.id


async def login(c, name="alice", password=PW, totp=None):
    r = await c.post(
        "/api/panel/login", json={"username": name, "password": password, "totp": totp}
    )
    return r


async def test_login_cookie_flags_and_me(panel_app, settings):
    await make_user(panel_app.state.sessionmaker)
    async with browser(panel_app) as c:
        r = await login(c, name="Alice")  # usernames are case-insensitive
        assert r.status_code == 200, r.text
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
        me = (await c.get("/api/panel/me")).json()
        assert me["user"]["username"] == "alice" and me["csrf_token"] == r.json()["csrf_token"]

    secure_app = create_app(settings, run_maintenance=False)  # production default
    async with secure_app.router.lifespan_context(secure_app), browser(secure_app) as c:
        r = await login(c)
        assert "Secure" in r.headers["set-cookie"]


async def test_the_cookie_is_secure_except_for_plain_http_on_the_lan(settings):
    # behind the agent's Caddy: it sets the client address and the scheme it saw
    app = create_app(settings.model_copy(update={"trust_proxy_headers": True}),
                     run_maintenance=False)  # fmt: skip
    async with app.router.lifespan_context(app):
        await make_user(app.state.sessionmaker)
        cases = [
            ("192.168.1.50", "http", False),  # the panel by its LAN address
            ("192.168.1.50", "https", True),
            ("203.0.113.9", "https", True),  # through Cloudflare
            ("203.0.113.9", "http", True),  # never a plain cookie for a public caller
        ]
        for ip, proto, secure in cases:
            async with browser(app) as c:
                c.headers.update({"X-Pos-Client-Ip": ip, "X-Forwarded-Proto": proto})
                r = await login(c)
                assert r.status_code == 200, r.text
                assert ("Secure" in r.headers["set-cookie"]) is secure, (ip, proto)


async def test_wrong_password_and_unknown_user_look_the_same(panel_app):
    await make_user(panel_app.state.sessionmaker)
    async with browser(panel_app) as c:
        a = await login(c, password="wrong password!!")
        b = await login(c, name="nobody")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


async def test_csrf_roles_and_reauth(panel_app):
    sm = panel_app.state.sessionmaker
    await make_user(sm)
    await make_user(sm, "victor", role="viewer")
    async with browser(panel_app) as c:
        csrf = (await login(c)).json()["csrf_token"]
        body = {"jobs": [{"type": "noop"}]}
        assert (await c.post("/api/admin/jobs", json=body)).status_code == 403  # no CSRF token
        r = await c.post("/api/admin/jobs", json=body, headers={auth.CSRF_HEADER: csrf})
        assert r.status_code == 201
        # sensitive action right after login: allowed (login counts as re-auth)
        r = await c.post("/api/admin/enrollment-codes", json={}, headers={auth.CSRF_HEADER: csrf})
        assert r.status_code == 201
        # ...but not once the re-auth window has passed
        async with sm() as s:
            await s.execute(
                update(UserSession).values(reauth_at=datetime.now(UTC) - timedelta(minutes=30))
            )
            await s.commit()
        r = await c.post("/api/admin/enrollment-codes", json={}, headers={auth.CSRF_HEADER: csrf})
        assert r.status_code == 403 and "reauth" in r.text
        r = await c.post(
            "/api/panel/reauth", json={"password": PW}, headers={auth.CSRF_HEADER: csrf}
        )
        assert r.status_code == 200
        r = await c.post("/api/admin/enrollment-codes", json={}, headers={auth.CSRF_HEADER: csrf})
        assert r.status_code == 201

    async with browser(panel_app) as c:
        csrf = (await login(c, "victor")).json()["csrf_token"]
        assert (await c.get("/api/admin/nodes")).status_code == 200  # viewers read
        r = await c.post(
            "/api/admin/jobs", json={"jobs": [{"type": "noop"}]}, headers={auth.CSRF_HEADER: csrf}
        )
        assert r.status_code == 403  # ...but do not change anything


async def test_totp_setup_login_and_replay(panel_app):
    sm = panel_app.state.sessionmaker
    await make_user(sm)
    async with browser(panel_app) as c:
        csrf = (await login(c)).json()["csrf_token"]
        h = {auth.CSRF_HEADER: csrf}
        secret = (await c.post("/api/panel/me/totp/setup", headers=h)).json()["secret"]
        bad = await c.post("/api/panel/me/totp/enable", json={"code": "000000"}, headers=h)
        assert bad.status_code == 422
        code = pyotp.TOTP(secret).now()
        assert (
            await c.post("/api/panel/me/totp/enable", json={"code": code}, headers=h)
        ).status_code == 200

    async with browser(panel_app) as c:
        r = await login(c)
        assert r.status_code == 401 and r.json()["detail"]["totp_required"] is True
        # the code used to enable TOTP cannot be used again
        assert (await login(c, totp=code)).status_code == 401
        async with sm() as s:  # pretend the next 30-second step has come
            await s.execute(update(AppUser).values(totp_last_counter=AppUser.totp_last_counter - 2))
            await s.commit()
        assert (await login(c, totp=pyotp.TOTP(secret).now())).status_code == 200
        assert (await login(c, totp=pyotp.TOTP(secret).now())).status_code == 401  # replay


async def test_rate_limit_after_repeated_failures(panel_app):
    await make_user(panel_app.state.sessionmaker)
    async with browser(panel_app) as c:
        for _ in range(auth.MAX_FAILS_PER_USER):
            assert (await login(c, password="nope nope nope")).status_code == 401
        r = await login(c)  # even the right password is refused for a while
        assert r.status_code == 429


async def test_logout_disable_and_password_change_end_sessions(panel_app):
    sm = panel_app.state.sessionmaker
    uid = await make_user(sm)
    await make_user(sm, "bob", role="viewer")
    async with browser(panel_app) as c1, browser(panel_app) as c2:
        csrf1 = (await login(c1)).json()["csrf_token"]
        await login(c2)
        new = "an entirely new passphrase"
        r = await c1.post(
            "/api/panel/me/password",
            json={"current": PW, "new": new},
            headers={auth.CSRF_HEADER: csrf1},
        )
        assert r.status_code == 200
        assert (await c1.get("/api/panel/me")).status_code == 200  # this session stays
        assert (await c2.get("/api/panel/me")).status_code == 401  # the other one ended
        await c1.post("/api/panel/logout")
        assert (await c1.get("/api/panel/me")).status_code == 401

    async with browser(panel_app) as admin_c, browser(panel_app) as bob_c:
        csrf = (await login(admin_c, password=new)).json()["csrf_token"]
        await login(bob_c, "bob")
        bob = next(
            u for u in (await admin_c.get("/api/panel/users")).json() if u["username"] == "bob"
        )
        r = await admin_c.patch(
            f"/api/panel/users/{bob['id']}",
            json={"disabled": True},
            headers={auth.CSRF_HEADER: csrf},
        )
        assert r.status_code == 200
        assert (await bob_c.get("/api/panel/me")).status_code == 401
        assert (await login(bob_c, "bob")).status_code == 401
        r = await admin_c.patch(
            f"/api/panel/users/{uid}", json={"role": "viewer"}, headers={auth.CSRF_HEADER: csrf}
        )
        assert r.status_code == 409  # not yourself


async def test_weak_passwords_are_refused(panel_app):
    await make_user(panel_app.state.sessionmaker)
    async with browser(panel_app) as c:
        csrf = (await login(c)).json()["csrf_token"]
        r = await c.post(
            "/api/panel/users",
            json={"username": "x1", "password": "short"},
            headers={auth.CSRF_HEADER: csrf},
        )
        assert r.status_code == 422


async def test_bearer_token_still_works_without_csrf(admin):
    r = await admin.post("/api/admin/jobs", json={"jobs": [{"type": "noop"}]})
    assert r.status_code == 201


async def test_overview(panel_app, sessionmaker):
    await make_user(panel_app.state.sessionmaker)
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        for ago in (timedelta(hours=2), timedelta(hours=2), timedelta(hours=30)):
            s.add(RawFetch(id=uuid7(), fetched_at=now - ago, url="https://x.invalid/", mode="http"))
        await s.commit()
    async with browser(panel_app) as c:
        await login(c)
        ov = (await c.get("/api/panel/overview")).json()
    assert set(ov) >= {"jobs", "nodes", "fetches_per_hour", "domains", "budget"}
    # 24 even hourly bars ending with the current hour; empty hours are zeros
    hours = [datetime.fromisoformat(x["hour"]) for x in ov["fetches_per_hour"]]
    assert len(hours) == 24
    assert all(b - a == timedelta(hours=1) for a, b in zip(hours, hours[1:], strict=False))
    assert hours[-1] == now.replace(minute=0, second=0, microsecond=0)
    counts = [x["fetches"] for x in ov["fetches_per_hour"]]
    assert counts[21] == 2 and sum(counts) == 2 and ov["fetches_24h"] == 2


async def test_cli_create_user(settings, monkeypatch, capsys):
    import io
    import sys
    from types import SimpleNamespace

    from pos_coordinator import config, main

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    args = SimpleNamespace(username="Carol", role="admin", password_stdin=True)
    assert await main._create_user(args) == 0
    assert await main._create_user(args) == 1  # exists
    app = create_app(settings, run_maintenance=False)
    async with app.router.lifespan_context(app), app.state.sessionmaker() as s:
        u = (await s.execute(select(AppUser))).scalar_one()
    assert u.username == "carol" and u.role == "admin"
    assert time.time()  # keep the import used


async def test_panel_is_served_with_security_headers(settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>panel</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    app = create_app(settings.model_copy(update={"panel_dist": dist}), run_maintenance=False)
    async with app.router.lifespan_context(app), browser(app) as c:
        root = await c.get("/")
        deep = await c.get("/nodes/some/client/route")
        asset = await c.get("/assets/app.js")
        api_404 = await c.get("/api/does-not-exist")
        escape = await c.get("/..%2f..%2fetc/passwd")
    assert root.text == deep.text == "<html>panel</html>"
    assert asset.text == "console.log(1)"
    assert api_404.status_code == 404 and "panel" not in api_404.text
    assert "root:" not in escape.text
    assert "frame-ancestors 'none'" in root.headers["content-security-policy"]
    assert root.headers["x-frame-options"] == "DENY"


async def test_one_totp_code_cannot_log_in_twice_at_once(app, sessionmaker):
    """Codex finding: two concurrent logins with the same code; only one may pass."""
    import asyncio as _asyncio

    import pyotp

    from pos_coordinator.models import AppUser
    from pos_coordinator.panel import auth as panel_auth

    secret = pyotp.random_base32()
    async with sessionmaker() as s:
        s.add(
            AppUser(
                username="twice",
                password_hash=panel_auth.hash_password("a-long-passphrase-1"),
                role="admin",
                totp_secret=secret,
            )
        )
        await s.commit()
    code = pyotp.TOTP(secret).now()

    async def attempt():
        async with sessionmaker() as s:
            try:
                await panel_auth.authenticate(s, "twice", "a-long-passphrase-1", code, None)
                await _asyncio.sleep(0.2)  # hold the transaction open, as a login does
                await s.commit()
                return True
            except panel_auth.AuthError:
                return False

    results = await _asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]


async def test_concurrent_guesses_cannot_pass_the_failure_limit(app, sessionmaker):
    """Codex finding: a burst of wrong passwords arriving together must still stop at
    the per-account limit."""
    import asyncio as _asyncio

    from pos_coordinator.models import AppUser, LoginAttempt
    from pos_coordinator.panel import auth as panel_auth

    async with sessionmaker() as s:
        s.add(
            AppUser(
                username="burst",
                password_hash=panel_auth.hash_password("a-long-passphrase-1"),
                role="viewer",
            )
        )
        await s.commit()

    async def guess(i):
        async with sessionmaker() as s:
            try:
                await panel_auth.authenticate(s, "burst", f"wrong-guess-{i}", None, "198.51.100.7")
            except panel_auth.RateLimited:
                return "limited"
            except panel_auth.AuthError:
                return "wrong"
            return "in"

    results = await _asyncio.gather(*(guess(i) for i in range(12)))
    assert results.count("wrong") == panel_auth.MAX_FAILS_PER_USER
    assert results.count("limited") == 12 - panel_auth.MAX_FAILS_PER_USER
    async with sessionmaker() as s:
        failures = (
            await s.execute(select(func.count()).where(LoginAttempt.username == "burst"))
        ).scalar_one()
    assert failures == panel_auth.MAX_FAILS_PER_USER
