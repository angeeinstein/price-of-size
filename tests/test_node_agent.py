"""Node agent (A/B updater) logic against an in-memory fake Docker daemon."""

import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pos_agent.config import AgentSettings
from pos_agent.docker import DockerError
from pos_agent.manager import Manager
from pos_agent.replace import clone_config, replace
from pos_agent.state import AgentState
from pos_shared import exchange, signing

D1 = "ghcr.io/x/worker@sha256:" + "1" * 64
D2 = "ghcr.io/x/worker@sha256:" + "2" * 64
A1 = "ghcr.io/x/agent@sha256:" + "a" * 64
A2 = "ghcr.io/x/agent@sha256:" + "b" * 64
C1 = "ghcr.io/x/coordinator@sha256:" + "c" * 64
C2 = "ghcr.io/x/coordinator@sha256:" + "d" * 64
CADDY = "caddy@sha256:" + "e" * 64


class FakeDocker:
    """Containers as dicts; `behaviour` decides what a started image does."""

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.images: set[str] = set()
        self.events: list[tuple] = []
        self.exit_codes: dict[tuple[str, str], int] = {}  # (image, first cmd arg) -> code
        self.on_start = lambda name, c: None
        self.fail_pull: set[str] = set()

    async def ping(self):
        return True

    async def pull(self, ref, timeout=0):
        self.events.append(("pull", ref))
        if ref in self.fail_pull:
            raise DockerError(500, "manifest unknown")
        self.images.add(ref)

    async def image_exists(self, ref):
        return ref in self.images

    async def create(self, name, config):
        if name in self.containers:
            raise DockerError(409, "name in use")
        self.events.append(("create", name, config["Image"]))
        self.containers[name] = {
            "Name": "/" + name,
            "Config": {
                "Image": config["Image"],
                "Env": config.get("Env", []),
                "Cmd": config.get("Cmd"),
                "Labels": config.get("Labels", {}),
            },
            "HostConfig": config.get("HostConfig", {}),
            "NetworkSettings": {"Networks": {}},
            "State": {"Running": False, "Status": "created"},
        }
        return name

    async def start(self, name):
        c = self.containers[name]
        self.events.append(("start", name))
        c["State"] = {"Running": True, "Status": "running"}
        self.on_start(name, c)

    async def stop(self, name, timeout_s=30):
        if name in self.containers:
            self.events.append(("stop", name))
            self.containers[name]["State"] = {"Running": False, "Status": "exited"}

    async def remove(self, name, force=True):
        if self.containers.pop(name, None) is not None:
            self.events.append(("remove", name))

    async def rename(self, name, new):
        self.containers[new] = self.containers.pop(name)
        self.events.append(("rename", name, new))

    async def inspect(self, name):
        return self.containers.get(name)

    async def wait(self, name, timeout_s):
        c = self.containers[name]
        return self.exit_codes.get((c["Config"]["Image"], c["Config"]["Cmd"][1]), 0)

    async def logs(self, name, tail=200):
        return "fake logs"

    async def ensure_network(self, name):
        pass


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def env(tmp_path, key):
    exch, data, etc = tmp_path / "exchange", tmp_path / "data", tmp_path / "etc"
    for d in (exch, data, etc):
        d.mkdir()
    (etc / "release.json").write_text(
        json.dumps({"version": "0.1.0", "images": {"worker": D1, "agent": A1}})
    )
    (etc / "worker.env").write_text("POS_WORKER_COORDINATOR_URL=https://c.example\n# comment\n")
    (exch / exchange.PINNED_KEY).write_text(signing.public_key_b64(key) + "\n")
    settings = AgentSettings(
        exchange_dir=exch,
        data_dir=data,
        bootstrap_release=etc / "release.json",
        service_env_file=etc / "worker.env",
        self_image=A1,
        health_timeout_s=10,
        switch_grace_s=0,
    )
    return settings


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def make_manager(settings, docker):
    clock = Clock()
    return Manager(settings, docker, clock=clock, sleep=clock.sleep), clock


def healthy_worker(settings, clock, healthy_images=(D1, D2)):
    """Started worker containers write health.json for their version, if healthy."""

    def on_start(name, c):
        if c["Config"]["Image"] in healthy_images and c["Config"]["Cmd"] == ["pos-worker"]:
            version = c["Config"]["Labels"].get("org.price-of-size.version")
            exchange.write_json(
                settings.exchange_dir / exchange.HEALTH, {"version": version, "ok_at": clock() + 1}
            )

    return on_start


def offer(settings, key, version, images):
    manifest = {"version": version, "images": images, "created_at": datetime.now(UTC).isoformat()}
    from pos_shared.api import ReleaseManifest

    payload = ReleaseManifest.model_validate(manifest).model_dump(mode="json")
    signed = {"manifest": payload, "signature": signing.sign(key, payload), "key_id": "k"}
    exchange.write_json(settings.exchange_dir / exchange.DESIRED, signed)


async def test_bootstrap_and_crash_recovery(env):
    docker = FakeDocker()
    m, _ = make_manager(env, docker)
    st = await m.reconcile_once()
    assert st.active == "a" and st.slots["a"].version == "0.1.0"
    c = docker.containers["pos-worker-a"]
    assert c["State"]["Running"] and c["Config"]["Image"] == D1
    assert "POS_WORKER_COORDINATOR_URL=https://c.example" in c["Config"]["Env"]
    assert c["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    status = exchange.read_json(env.exchange_dir / exchange.STATUS)
    assert status["running_version"] == "0.1.0" and status["update_state"] == "idle"

    await docker.stop("pos-worker-a")
    await m.reconcile_once()
    assert docker.containers["pos-worker-a"]["State"]["Running"]

    await docker.remove("pos-worker-a")
    await m.reconcile_once()
    assert docker.containers["pos-worker-a"]["State"]["Running"]


async def test_successful_update_keeps_old_slot_for_rollback(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    offer(env, key, "0.2.0", {"worker": D2, "agent": A1})
    st = await m.reconcile_once()

    assert st.active == "b" and st.slots["b"].version == "0.2.0"
    assert docker.containers["pos-worker-b"]["State"]["Running"]
    assert not docker.containers["pos-worker-a"]["State"]["Running"]  # kept, stopped
    order = [e for e in docker.events if e[0] in ("stop", "start") and "selftest" not in e[1]]
    # worker role: the old worker is drained before the new one starts
    assert order[-2:] == [("stop", "pos-worker-a"), ("start", "pos-worker-b")]
    assert st.update_state == "idle"


async def test_failed_selftest_changes_nothing_and_is_not_retried_at_once(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    docker.exit_codes[(D2, "selftest")] = 1
    offer(env, key, "0.2.0", {"worker": D2})
    st = await m.reconcile_once()

    assert st.active == "a" and st.update_state == "failed"
    assert "selftest" in st.update_error
    assert docker.containers["pos-worker-a"]["State"]["Running"]
    assert ("stop", "pos-worker-a") not in docker.events

    def attempts():  # each update attempt self-tests the new image
        return len([e for e in docker.events if e[0] == "create" and "selftest" in e[1]])

    tried = attempts()
    await m.reconcile_once()
    assert attempts() == tried  # backed off
    clock.t += env.failed_retry_after_s + 1
    await m.reconcile_once()
    assert attempts() == tried + 1  # retried later


async def test_unhealthy_new_version_is_rolled_back(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock, healthy_images=(D1,))
    await m.reconcile_once()
    offer(env, key, "0.2.0", {"worker": D2})
    st = await m.reconcile_once()

    assert st.active == "a" and st.update_state == "failed"
    assert "healthy" in st.update_error
    assert docker.containers["pos-worker-a"]["State"]["Running"]
    assert "pos-worker-b" not in docker.containers
    assert AgentState.load(env.data_dir / "state.json").failed["0.2.0"]


async def test_pull_failure_leaves_service_untouched(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    docker.fail_pull.add(D2)
    offer(env, key, "0.2.0", {"worker": D2})
    st = await m.reconcile_once()
    assert st.active == "a" and "manifest unknown" in st.update_error
    assert docker.containers["pos-worker-a"]["State"]["Running"]


async def test_unsigned_or_foreign_releases_are_refused(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()  # pins the offered key

    attacker = Ed25519PrivateKey.generate()
    # A compromised service swaps the offered key and signs its own release:
    (env.exchange_dir / exchange.PINNED_KEY).write_text(signing.public_key_b64(attacker))
    offer(env, attacker, "6.6.6", {"worker": D2})
    st = await m.reconcile_once()
    assert st.active == "a" and st.slots["a"].version == "0.1.0"
    assert ("pull", D2) not in docker.events
    status = exchange.read_json(env.exchange_dir / exchange.STATUS)
    assert "not signed by the pinned key" in status["update_error"]


async def test_agent_updates_itself_only_after_the_service(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    offer(env, key, "0.2.0", {"worker": D2, "agent": A2})
    await m.reconcile_once()  # round 1: the service only
    assert "pos-agent-updater" not in docker.containers
    await m.reconcile_once()  # round 2: service is current, now the agent
    helper = docker.containers["pos-agent-updater"]
    assert helper["Config"]["Image"] == A2
    assert helper["Config"]["Cmd"][:2] == ["pos-agent", "replace"]
    assert helper["HostConfig"]["AutoRemove"] is True


async def test_failed_agent_update_is_recorded_and_backed_off(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    offer(env, key, "0.1.0", {"worker": D1, "agent": A2})
    exchange.write_json(
        env.data_dir / "agent-update.json", {"version": "0.1.0", "result": "failed", "error": "x"}
    )
    await m.reconcile_once()
    assert "pos-agent-updater" not in docker.containers
    assert not (env.data_dir / "agent-update.json").exists()


async def test_replace_swaps_agent_and_rolls_back(tmp_path):
    docker = FakeDocker()
    await docker.create(
        "pos-agent",
        {
            "Image": A1,
            "Env": ["POS_AGENT_SELF_IMAGE=" + A1],
            "Cmd": ["pos-agent"],
            "HostConfig": {"Mounts": []},
        },
    )
    await docker.start("pos-agent")

    def healthy_agent(name, c):
        if c["Config"]["Image"] == A2:
            exchange.write_json(tmp_path / "agent-health.json", {"image": A2, "ok_at": 9e12})

    docker.on_start = healthy_agent
    assert await replace(docker, "pos-agent", A2, "0.2.0", tmp_path, health_timeout_s=5)
    c = docker.containers["pos-agent"]
    assert c["Config"]["Image"] == A2 and c["State"]["Running"]
    assert "POS_AGENT_SELF_IMAGE=" + A2 in c["Config"]["Env"]
    assert "pos-agent-next" not in docker.containers
    assert exchange.read_json(tmp_path / "agent-update.json")["result"] == "ok"

    docker.on_start = lambda n, c: None  # the next version never reports healthy
    (tmp_path / "agent-health.json").unlink()
    A3 = "ghcr.io/x/agent@sha256:" + "f" * 64
    assert not await replace(docker, "pos-agent", A3, "0.3.0", tmp_path, health_timeout_s=0.1)
    c = docker.containers["pos-agent"]
    assert c["Config"]["Image"] == A2 and c["State"]["Running"]
    assert "pos-agent-next" not in docker.containers
    assert exchange.read_json(tmp_path / "agent-update.json")["result"] == "failed"


def test_clone_config_keeps_mounts_and_restart_policy():
    info = {
        "Config": {
            "Image": A1,
            "Env": ["A=1", "POS_AGENT_SELF_IMAGE=" + A1],
            "Cmd": ["x"],
            "Labels": {"l": "v"},
        },
        "HostConfig": {
            "Mounts": [{"Type": "volume"}],
            "RestartPolicy": {"Name": "always"},
            "NetworkMode": "pos-net",
            "Privileged": False,
        },
        "NetworkSettings": {"Networks": {"pos-net": {"Aliases": ["pos-agent"]}}},
    }
    cfg = clone_config(info, A2)
    assert cfg["Image"] == A2
    assert cfg["Env"] == ["A=1", "POS_AGENT_SELF_IMAGE=" + A2]
    assert cfg["HostConfig"]["RestartPolicy"] == {"Name": "always"}
    assert cfg["NetworkingConfig"] == {"EndpointsConfig": {"pos-net": {"Aliases": ["pos-agent"]}}}


async def test_coordinator_blue_green_with_backup_and_migrations(env, key, tmp_path):
    (tmp_path / "etc" / "coordinator.env").write_text("POS_DATABASE_URL=postgresql+psycopg://x\n")
    exchange.write_json(
        env.bootstrap_release,
        {"version": "0.1.0", "images": {"coordinator": C1, "agent": A1, "caddy": CADDY}},
    )
    settings = env.model_copy(
        update={"role": "coordinator", "service_env_file": tmp_path / "etc" / "coordinator.env"}
    )
    docker = FakeDocker()
    m, clock = make_manager(settings, docker)

    async def healthy(slot, version, since):
        c = docker.containers.get(f"pos-coordinator-{slot}")
        return bool(c and c["State"]["Running"])

    m.service.is_healthy = healthy
    await m.reconcile_once()
    assert docker.containers["pos-caddy"]["State"]["Running"]
    assert docker.containers["pos-coordinator-a"]["State"]["Running"]
    first = [e[1] for e in docker.events if e[0] == "create"]
    assert first.index("pos-coordinator-migrate") < first.index("pos-coordinator-a")
    assert "pos-coordinator-backup" not in first  # nothing to back up on first start

    docker.events.clear()
    offer(settings, key, "0.2.0", {"coordinator": C2, "agent": A1, "caddy": CADDY})
    st = await m.reconcile_once()
    assert st.active == "b"
    names = [(e[0], e[1]) for e in docker.events if e[0] in ("create", "start", "stop")]
    created = [n for op, n in names if op == "create"]
    assert created[:3] == [
        "pos-coordinator-selftest",
        "pos-coordinator-backup",
        "pos-coordinator-migrate",
    ]
    # blue-green: the new slot starts before the old one stops
    assert names.index(("start", "pos-coordinator-b")) < names.index(("stop", "pos-coordinator-a"))


async def test_worker_role_does_not_pull_coordinator_only_images(env, key):
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    docker.fail_pull.add(CADDY)  # unreachable, and irrelevant for a worker
    offer(env, key, "0.2.0", {"worker": D2, "caddy": CADDY})
    st = await m.reconcile_once()
    assert st.active == "b" and st.update_state == "idle"
    assert ("pull", CADDY) not in docker.events


async def test_caddy_publishes_on_loopback_and_is_fixed_if_not(env, key, tmp_path):
    (tmp_path / "etc" / "coordinator.env").write_text("POS_DATABASE_URL=x\n")
    exchange.write_json(
        env.bootstrap_release, {"version": "0.1.0", "images": {"coordinator": C1, "caddy": CADDY}}
    )
    settings = env.model_copy(
        update={"role": "coordinator", "service_env_file": tmp_path / "etc" / "coordinator.env"}
    )
    docker = FakeDocker()
    m, _ = make_manager(settings, docker)
    await m.reconcile_once()
    binding = docker.containers["pos-caddy"]["HostConfig"]["PortBindings"]["80/tcp"][0]
    assert binding == {"HostIp": "127.0.0.1", "HostPort": "8080"}
    # an older Caddy published on every interface is replaced
    docker.containers["pos-caddy"]["HostConfig"]["PortBindings"]["80/tcp"][0] = {"HostPort": "8080"}
    await m.reconcile_once()
    fixed = docker.containers["pos-caddy"]["HostConfig"]["PortBindings"]["80/tcp"][0]
    assert fixed["HostIp"] == "127.0.0.1"


async def test_recovery_stops_the_other_slot_before_starting_the_active_one(env, key):
    """Codex finding: agent restarted mid-update; the new slot still runs while the
    recorded active slot is stopped. Never two workers at the same time."""
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()  # slot a active
    await docker.stop("pos-worker-a")
    await docker.create("pos-worker-b", {"Image": D2, "Cmd": ["pos-worker"]})
    await docker.start("pos-worker-b")
    docker.events.clear()
    await m.reconcile_once()
    ops = [(e[0], e[1]) for e in docker.events if e[0] in ("start", "stop")]
    assert ops.index(("stop", "pos-worker-b")) < ops.index(("start", "pos-worker-a"))
    assert docker.containers["pos-worker-a"]["State"]["Running"]
    assert not docker.containers["pos-worker-b"]["State"]["Running"]


async def test_config_drift_recreates_the_service(env, key):
    """Codex finding: a changed env file (or mount) must reach the running container."""
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    first = docker.containers["pos-worker-a"]
    await m.reconcile_once()
    assert docker.containers["pos-worker-a"] is first  # unchanged: left alone
    env.service_env_file.write_text("POS_WORKER_COORDINATOR_URL=https://new.example\n")
    await m.reconcile_once()
    c = docker.containers["pos-worker-a"]
    assert c is not first and "POS_WORKER_COORDINATOR_URL=https://new.example" in c["Config"]["Env"]
    assert c["State"]["Running"]


async def test_rollback_works_while_the_registry_is_down(env, key):
    """Codex finding: the previous release's images are kept, and a digest names exact
    bytes, so going back must not depend on the registry."""
    docker = FakeDocker()
    m, clock = make_manager(env, docker)
    docker.on_start = healthy_worker(env, clock)
    await m.reconcile_once()
    offer(env, key, "0.2.0", {"worker": D2, "agent": A1})
    assert (await m.reconcile_once()).slots["b"].version == "0.2.0"
    docker.fail_pull.update({D1, D2})  # registry unreachable from now on
    docker.events.clear()
    offer(env, key, "0.1.0", {"worker": D1, "agent": A1})
    st = await m.reconcile_once()
    assert st.slots[st.active].version == "0.1.0" and st.update_state == "idle"
    assert not [e for e in docker.events if e[0] == "pull"]


@pytest.mark.parametrize(
    ("bind", "proxies", "trusted"),
    [
        ("127.0.0.1", "", ["0.0.0.0/0", "::/0"]),  # only this machine can call
        ("0.0.0.0", "", []),  # the LAN can call: nobody's visitor address is believed
        ("0.0.0.0", "192.168.1.10, 10.0.0.0/24", ["192.168.1.10/32", "10.0.0.0/24"]),
        ("192.168.1.5", "fd00::7", ["fd00::7/128"]),
    ],
)
def test_caddy_trusts_the_visitor_address_only_from_trusted_proxies(bind, proxies, trusted):
    from pos_agent.services import CoordinatorService

    s = AgentSettings(role="coordinator", http_bind=bind, trusted_proxies=proxies)
    svc = CoordinatorService(s, FakeDocker())
    assert svc.trusted_proxy_ranges() == trusted
    server = svc.caddy_config()["apps"]["http"]["servers"]["pos"]
    if trusted:
        assert server["trusted_proxies"] == {"source": "static", "ranges": trusted}
        assert server["client_ip_headers"] == ["CF-Connecting-IP", "X-Forwarded-For"]
    else:
        assert "trusted_proxies" not in server and "client_ip_headers" not in server
    # every request carries Caddy's own client address, overwriting the caller's
    first = server["routes"][0]["handle"][0]
    assert first == {
        "handler": "headers",
        "request": {"set": {"X-Pos-Client-Ip": ["{http.vars.client_ip}"]}},
    }
    assert server["routes"][0]["handle"][1]["handler"] == "reverse_proxy"


def test_a_malformed_trusted_proxy_is_an_error():
    from pos_agent.services import CoordinatorService

    s = AgentSettings(role="coordinator", http_bind="0.0.0.0", trusted_proxies="cloudflared")
    with pytest.raises(ValueError):
        CoordinatorService(s, FakeDocker()).caddy_config()
