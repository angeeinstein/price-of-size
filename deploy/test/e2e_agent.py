"""End-to-end test of the node agent with real Docker.

Builds three versions of the images, pushes them to a local registry (so every
reference is pinned by digest, as in production), and checks:

1. a coordinator agent bootstraps Caddy + coordinator (migrations included)
2. a worker agent bootstraps a worker that enrolls and processes jobs
3. the worker updates to a signed release (A/B, health-checked)
4. a broken release is rolled back automatically
5. the coordinator updates blue-green without failing a single request
6. both agents update themselves through the helper container

Requirements: Docker, a PostgreSQL with pgvector reachable from containers at
--db-host (default: the pos-net gateway), a registry at localhost:5000
(`docker run -d --network host registry:2`). Run from the repository root:

    uv run python deploy/test/e2e_agent.py --db-url postgresql+psycopg://pos:pos@{host}:5432/pos_e2e

Destructive for anything named pos-* on this Docker host. Never run it on a node.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "localhost:5000"
COMPONENTS = ("coordinator", "worker", "agent")
ADMIN_TOKEN = "e2e-admin-token-" + "z" * 32
NET = "pos-net"


def sh(*cmd: str, check: bool = True, capture: bool = True) -> str:
    r = subprocess.run(cmd, check=False, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{r.stdout}\n{r.stderr}")
    return (r.stdout or "").strip()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait(what: str, predicate, timeout: float = 240, interval: float = 2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what} (last: {last!r})")


# --- images --------------------------------------------------------------------


def build_version(version: str, reported: str, dockerfile_filter: str | None, build_args):
    """Build all images for `version`; the code inside reports `reported`."""
    ctx = Path(tempfile.mkdtemp(prefix=f"pos-e2e-{version}-"))
    files = sh("git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard")
    for rel in files.splitlines():
        src = ROOT / rel
        if src.is_file():
            dst = ctx / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    extra = ROOT / ".sandbox-ca.crt"
    if extra.exists():
        shutil.copy2(extra, ctx / ".sandbox-ca.crt")
    vfile = ctx / "shared/pos_shared/version.py"
    # whatever version the checkout is at: the images report the one asked for
    text, n = re.subn(
        r'^__version__ = "[^"]*"$', f'__version__ = "{reported}"', vfile.read_text(), flags=re.M
    )
    if n != 1:
        raise SystemExit(f"{vfile}: expected one __version__ line, found {n}")
    vfile.write_text(text)
    refs = {}
    for c in COMPONENTS:
        dockerfile = ctx / f"deploy/docker/{c}.Dockerfile"
        if dockerfile_filter:
            sh(dockerfile_filter, str(dockerfile), str(dockerfile))
        tag = f"{REGISTRY}/pos-{c}:{version}"
        log(f"building {tag}")
        sh("docker", "build", *build_args, "-q", "-f", str(dockerfile), "-t", tag, str(ctx))
        sh("docker", "push", "-q", tag)
        digest = sh("docker", "inspect", "--format", "{{index .RepoDigests 0}}", tag)
        refs[c] = digest
    shutil.rmtree(ctx)
    return refs


def caddy_ref() -> str:
    # pull only if missing: Docker Hub rate-limits anonymous pulls
    if not sh("docker", "images", "-q", "caddy:2.8-alpine", check=False):
        sh("docker", "pull", "-q", "caddy:2.8-alpine")
    return sh("docker", "inspect", "--format", "{{index .RepoDigests 0}}", "caddy:2.8-alpine")


# --- environment -------------------------------------------------------------------


def cleanup() -> None:
    ids = sh("docker", "ps", "-aq", "--filter", "label=org.price-of-size.managed=true")
    ids += "\n" + sh("docker", "ps", "-aq", "--filter", "name=pos-agent")
    ids = [i for i in ids.split() if i]
    if ids:
        sh("docker", "rm", "-f", *ids, check=False)
    vols = [v for v in sh("docker", "volume", "ls", "-q").split() if v.startswith("pos-")]
    if vols:
        sh("docker", "volume", "rm", "-f", *vols, check=False)


def start_agent(role: str, etc: Path, agent_image: str, extra_env: dict[str, str]) -> None:
    name = f"pos-agent-{role}"
    env = {
        "POS_AGENT_ROLE": role,
        "POS_AGENT_SELF_IMAGE": agent_image,
        "POS_AGENT_CONTAINER_NAME": name,
        "POS_AGENT_AGENT_DATA_VOLUME": f"pos-agent-{role}-data",
        "POS_AGENT_EXCHANGE_VOLUME": f"pos-{role}-exchange",
        "POS_AGENT_SERVICE_ENV_FILE": f"/etc/pos/{role}.env",
        "POS_AGENT_CHECK_INTERVAL_S": "2",
        **extra_env,
    }
    args = ["docker", "run", "-d", "--name", name, "--restart", "always", "--network", NET]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"pos-agent-{role}-data:/data",
        "-v",
        f"pos-{role}-exchange:/exchange",
        "-v",
        f"{etc}:/etc/pos:ro",
        agent_image,
    ]
    sh(*args)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", help="may contain {host} for the pos-net gateway")
    ap.add_argument("--dockerfile-filter", default=os.environ.get("POS_E2E_DOCKERFILE_FILTER"))
    ap.add_argument("--build-arg", action="append", default=[])
    ap.add_argument("--keep", action="store_true", help="leave containers running at the end")
    ap.add_argument(
        "--build-only",
        metavar="RELEASE_JSON",
        help="only build and push 0.1.0 and write its release.json here",
    )
    args = ap.parse_args()
    build_args = []
    for b in args.build_arg:
        build_args += ["--build-arg", b]
    if os.environ.get("POS_E2E_BUILD_NETWORK"):
        build_args += ["--network", os.environ["POS_E2E_BUILD_NETWORK"]]

    if args.build_only:
        refs = build_version("0.1.0", "0.1.0", args.dockerfile_filter, build_args)
        Path(args.build_only).write_text(
            json.dumps({"version": "0.1.0", "images": {**refs, "caddy": caddy_ref()}}, indent=2)
        )
        log(f"wrote {args.build_only}")
        return 0

    cleanup()
    sh("docker", "network", "create", NET, check=False)
    gateway = sh("docker", "network", "inspect", NET, "-f", "{{(index .IPAM.Config 0).Gateway}}")
    if not args.db_url:
        ap.error("--db-url is required")
    db_url = args.db_url.format(host=gateway)

    v1 = build_version("0.1.0", "0.1.0", args.dockerfile_filter, build_args)
    v2 = build_version("0.2.0", "0.2.0", args.dockerfile_filter, build_args)
    broken = build_version("0.3.0", "0.0.0-broken", args.dockerfile_filter, build_args)
    caddy = caddy_ref()

    tmp = Path(tempfile.mkdtemp(prefix="pos-e2e-etc-"))
    etc_c, etc_w, backups = tmp / "etc-c", tmp / "etc-w", tmp / "backups"
    for d in (etc_c, etc_w, backups):
        d.mkdir()
    # Owned by the coordinator's service user; via a container, since CI runs unprivileged.
    sh(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "chown",
        "-v",
        f"{backups}:/b",
        v1["agent"],
        "10001:10001",
        "/b",
    )
    release_1 = {"version": "0.1.0", "images": {**v1, "caddy": caddy}}
    (etc_c / "release.json").write_text(json.dumps(release_1))
    (etc_w / "release.json").write_text(json.dumps(release_1))
    (etc_c / "coordinator.env").write_text(
        f"POS_DATABASE_URL={db_url}\nPOS_ADMIN_TOKEN={ADMIN_TOKEN}\nPOS_REAPER_INTERVAL_S=2\n"
    )

    # 1. coordinator ----------------------------------------------------------------
    log("starting the coordinator agent")
    start_agent(
        "coordinator",
        etc_c,
        v1["agent"],
        {
            "POS_AGENT_HOST_BACKUP_DIR": str(backups),
            "POS_AGENT_HTTP_PORT": "8080",
            "POS_AGENT_SWITCH_GRACE_S": "3",
        },
    )
    base = "http://localhost:8080"
    wait("coordinator 0.1.0", lambda: httpx.get(f"{base}/healthz").json()["version"] == "0.1.0")
    admin = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    for version, images in (("0.1.0", v1), ("0.2.0", v2), ("0.3.0", broken)):
        r = admin.post(
            "/api/admin/releases", json={"version": version, "images": {**images, "caddy": caddy}}
        )
        assert r.status_code == 201, r.text
    log("coordinator up, releases registered")

    # 2. worker ------------------------------------------------------------------------
    code = admin.post("/api/admin/enrollment-codes", json={"label": "e2e"}).json()["code"]
    (etc_w / "worker.env").write_text(
        f"POS_WORKER_COORDINATOR_URL=http://pos-caddy:80\nPOS_WORKER_ENROLLMENT_CODE={code}\n"
        "POS_WORKER_NAME=e2e-worker\n"
    )
    start_agent("worker", etc_w, v1["agent"], {"POS_AGENT_WORKER_ON_NETWORK": "true"})

    def node():
        nodes = admin.get("/api/admin/nodes").json()
        return nodes[0] if nodes else None

    wait("worker online", lambda: (n := node()) and n["online"] and n["agent_status"])
    node_id = node()["id"]
    log(f"worker {node_id} online, agent reports {node()['agent_status']['running_version']}")

    def jobs_done(n):
        admin.post(
            "/api/admin/jobs",
            json={
                "jobs": [
                    {"type": "dummy_fetch", "payload": {"url": f"https://e2e.invalid/{i}"}}
                    for i in range(n)
                ]
            },
        )
        wait(
            f"{n} jobs done",
            lambda: (
                not [
                    s
                    for s in admin.get("/api/admin/jobs/stats").json()
                    if s["status"] not in ("done",)
                ]
            ),
            timeout=120,
        )

    jobs_done(20)
    log("jobs processed on 0.1.0")

    # 3. worker update ---------------------------------------------------------------
    admin.patch(f"/api/admin/nodes/{node_id}", json={"target_version": "0.2.0"})
    wait(
        "worker on 0.2.0",
        lambda: (
            node()["version"] == "0.2.0" and node()["agent_status"]["running_version"] == "0.2.0"
        ),
    )
    jobs_done(20)
    log("worker updated to 0.2.0 and processing")

    # 6a. worker agent updates itself once the worker is current
    wait(
        "worker agent on 0.2.0",
        lambda: (
            sh("docker", "inspect", "-f", "{{.Config.Image}}", "pos-agent-worker") == v2["agent"]
        ),
    )
    wait("new worker agent healthy", lambda: node()["agent_status"]["agent_version"] == "0.2.0")
    log("worker agent replaced itself with 0.2.0")

    # 4. broken release ----------------------------------------------------------------
    admin.patch(f"/api/admin/nodes/{node_id}", json={"target_version": "0.3.0"})
    wait(
        "rollback of 0.3.0", lambda: node()["agent_status"]["update_state"] == "failed", timeout=400
    )
    st = node()["agent_status"]
    assert st["running_version"] == "0.2.0", st
    assert "healthy" in st["update_error"], st  # rolled back by the health check, not earlier
    wait("worker back online on 0.2.0", lambda: node()["online"] and node()["version"] == "0.2.0")
    jobs_done(10)
    log(f"broken release rolled back: {st['update_error'][:120]}")

    # 5. coordinator blue-green -----------------------------------------------------------
    errors, requests_ = [], [0]
    stop = threading.Event()

    def probe():
        with httpx.Client(timeout=10) as c:
            while not stop.is_set():
                try:
                    r = c.get(f"{base}/healthz")
                    if r.status_code != 200:
                        errors.append(r.status_code)
                except httpx.HTTPError as e:
                    errors.append(repr(e))
                requests_[0] += 1
                time.sleep(0.05)

    # The new release has a database migration to run (the database is put one
    # revision back), so its agent backs the database up before migrating.
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "-1"],
        cwd=ROOT,
        env={**os.environ, "POS_DATABASE_URL": db_url},
        check=True,
    )
    t = threading.Thread(target=probe, daemon=True)
    t.start()
    r = admin.put("/api/admin/settings/coordinator.target_version", json={"value": "0.2.0"})
    assert r.status_code == 200, r.text
    wait(
        "coordinator on 0.2.0",
        lambda: httpx.get(f"{base}/healthz").json()["version"] == "0.2.0",
        timeout=400,
    )
    time.sleep(10)  # keep probing past the old slot's shutdown
    stop.set()
    t.join()
    log(f"coordinator switched blue-green: {requests_[0]} probes, {len(errors)} errors")
    assert not errors, errors[:5]
    assert list(backups.glob("pos-*-before-0.2.0.dump")), "no pre-migration backup"
    bind = sh(
        "docker",
        "inspect",
        "-f",
        '{{(index (index .HostConfig.PortBindings "80/tcp") 0).HostIp}}',
        "pos-caddy",
    )
    assert bind == "127.0.0.1", bind

    wait(
        "coordinator agent on 0.2.0",
        lambda: (
            sh("docker", "inspect", "-f", "{{.Config.Image}}", "pos-agent-coordinator")
            == v2["agent"]
        ),
    )
    log("coordinator agent replaced itself with 0.2.0")

    jobs_done(10)
    log("ALL CHECKS PASSED")
    if not args.keep:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
