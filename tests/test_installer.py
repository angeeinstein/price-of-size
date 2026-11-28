"""install.sh, piece by piece: functions run in bash against fake system files."""

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"

UBUNTU = """PRETTY_NAME="Ubuntu 24.04.5 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION="24.04.5 LTS (Noble Numbat)"
VERSION_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
"""
MINIMAL = """NAME="Some Debian derivative"
VERSION="1.0"
ID=debian
"""
DEBIAN = """PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
VERSION_CODENAME=bookworm
ID=debian
"""


def _function(name: str) -> str:
    """The text of one shell function of install.sh."""
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("os_release", "expected"),
    [
        (UBUNTU, "ubuntu|debian|noble"),
        (DEBIAN, "debian||bookworm"),
        (MINIMAL, "debian||"),  # no ID_LIKE, no codename: empty, not an abort
    ],
)
def test_detecting_the_system_keeps_the_release_to_install(tmp_path, os_release, expected):
    # os-release defines VERSION too; sourcing it into the script made the installer
    # look for a release named "v24.04.5 LTS (Noble Numbat)"
    (tmp_path / "os-release").write_text(os_release)
    script = "\n".join(
        [
            "set -Eeuo pipefail",
            'die() { echo "error: $*" >&2; exit 1; }',
            _function("detect_os"),
            "VERSION=0.1.0 NAME=keep",
            f"POS_OS_RELEASE={tmp_path / 'os-release'}",
            "detect_os",
            'echo "$VERSION|$NAME|$OS_ID|$OS_LIKE|$OS_CODENAME"',
        ]
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == f"0.1.0|keep|{expected}"


def _run(body: str, tty: bool = False) -> subprocess.CompletedProcess:
    """`body` in bash with install.sh's strict mode, ERR trap and `progress`."""
    script = "\n".join(
        [
            "set -Eeuo pipefail",
            'die() { echo "error: $*" >&2; exit 1; }',
            "trap 'die \"failed at line $LINENO\"' ERR",
            _function("progress"),
            body,
        ]
    )
    if tty:  # stderr on a terminal: the elapsed-time line
        return subprocess.run(
            ["script", "-qec", f"bash -c {shlex.quote(script)}", "/dev/null"],
            capture_output=True,
            text=True,
        )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_progress_says_what_runs_and_shows_output_only_on_failure():
    r = _run('progress "copying" sh -c "echo noise; sleep 1"; echo after')
    assert r.returncode == 0 and r.stdout == "after\n"
    assert "copying ..." in r.stderr and "noise" not in r.stderr
    r = _run('progress "breaking" sh -c "echo why it broke >&2; exit 3" || echo "rc=$?"')
    assert r.stdout == "rc=3\n"  # the command's own status; no trap fired inside
    assert "why it broke" in r.stderr and "failed at line" not in r.stderr


@pytest.mark.skipif(shutil.which("script") is None, reason="needs script(1)")
def test_progress_on_a_terminal_counts_the_seconds():
    r = _run('progress "waiting" sleep 2; echo done', tty=True)
    assert r.returncode == 0
    assert "waiting ... 1s" in r.stdout and "done" in r.stdout  # script(1) merges the streams
    r = _run('progress "breaking" sh -c "echo why >&2; exit 3" || echo "rc=$?"', tty=True)
    assert "rc=3" in r.stdout and "why" in r.stdout and "failed at line" not in r.stdout


# --- install.sh --update: the coordinator's update API, followed in the terminal ------


def _assignment(name: str) -> str:
    """A multi-line NAME='...' assignment of install.sh."""
    text = INSTALL.read_text()
    start = text.index(f"\n{name}='") + 1
    end = text.index("'\n", start + len(name) + 2) + 1
    return text[start:end]


class _FakeCoordinator:
    """The admin update API, answering GET /api/admin/updates from a script."""

    def __init__(self, polls, start_status=200, start_detail="", legacy=False, check_error=None):
        import http.server
        import json
        import threading

        self.requests = []
        self.upgraded = not legacy  # a legacy coordinator has no update API until updated
        polls = list(polls)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                fake.requests.append(
                    (self.command, self.path, self.headers.get("authorization"), body)
                )

            def do_GET(self):
                self._record()
                if self.path == "/healthz":
                    return self._send(
                        200, {"ok": True, "version": "0.2.0" if fake.upgraded else "0.1.0"}
                    )
                state = polls.pop(0) if len(polls) > 1 else polls[0]
                if state is None:
                    return self._send(502, {"detail": "bad gateway"})
                return self._send(200, state)

            def do_PUT(self):
                self._record()
                if self.path == "/api/admin/settings/coordinator.target_version":
                    fake.upgraded = True  # the agent switches it
                    return self._send(200, {"value": "0.2.0"})
                self._send(404, {"detail": "Not Found"})

            def do_POST(self):
                self._record()
                if self.path == "/api/admin/releases":
                    return self._send(201, {"manifest": {}})
                if not fake.upgraded:
                    return self._send(404, {"detail": "Not Found"})
                if self.path == "/api/admin/updates/check":
                    ov = _overview(available=not check_error)
                    if check_error:
                        ov["check"] = {"error": check_error, "latest": None}
                    return self._send(200, ov)
                if self.path == "/api/admin/updates/start":
                    if start_status != 200:
                        return self._send(start_status, {"detail": start_detail})
                    return self._send(200, _overview("coordinator", available=True))
                self._send(404, {"detail": "no"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def _overview(status=None, *, available=False, agent_state=None, a="0.1.0", b="0.1.0", error=None):
    nodes = [
        {"id": "n-a", "name": "node-a", "managed": True, "online": True, "version": a,
         "running_version": a,
         "update_state": "draining" if status == "canary" and a == "0.1.0" else "idle"},
        {"id": "n-b", "name": "node-b", "managed": True, "online": True, "version": b,
         "running_version": b,
         "update_state": "idle"},
    ]  # fmt: skip
    return {
        "current": "0.1.0",
        "update_available": available,
        "check": {"error": None, "latest": {"version": "0.2.0"}},
        "coordinator_agent": {"update_state": agent_state, "update_target": "0.2.0"},
        "rollouts": []
        if status is None
        else [
            {
                "id": "r1",
                "version": "0.2.0",
                "status": status,
                "canary_node_id": "n-a",
                "error": error,
            }
        ],  # fmt: skip
        "nodes": nodes,
    }


def _run_update(url: str, etc: Path | None = None, explicit: bool = False):
    script = "\n".join(
        [
            "set -Eeuo pipefail",
            'info() { echo "==> $*"; }',
            'ok() { echo "ok  $*"; }',
            'warn() { echo "warning: $*" >&2; }',
            'die() { echo "error: $*" >&2; exit 1; }',
            "sleep() { :; }",
            f"local_url() {{ echo {shlex.quote(url)}; }}",
            f"ETC={shlex.quote(str(etc or '/nonexistent'))} COORD_ADMIN_TOKEN=secret-token"
            " COORD_API_ERROR='' COORD_API_CODE=''"
            f" EXPLICIT_RELEASE={int(explicit)}",
            "docker() { :; }",
            _function("env_get"),
            _function("coord_api"),
            _assignment("ROLLOUT_LINE"),
            _function("follow_update"),
            _function("legacy_coordinator_update"),
            _function("update_installation"),
            "update_installation",
        ]
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_update_from_the_terminal_follows_every_step():
    fake = _FakeCoordinator(
        [
            _overview("coordinator", agent_state="backing_up"),
            _overview("coordinator", agent_state="migrating"),
            None,  # switching versions: not answering for a moment
            _overview("canary"),
            _overview("nodes", a="0.2.0"),
            _overview("done", a="0.2.0", b="0.2.0"),
        ]
    )
    try:
        r = _run_update(fake.url)
    finally:
        fake.close()
    assert r.returncode == 0, r.stderr
    progress = r.stderr
    for line in (
        "coordinator: backing up the database",
        "coordinator: migrating the database",
        "coordinator: switching versions",
        "first node node-a: stopping the old version",
        "other nodes: 1 of 2 on 0.2.0",
    ):
        assert line in progress, progress
    assert "ok  updated to 0.2.0" in r.stdout
    start = next(q for q in fake.requests if q[1] == "/api/admin/updates/start")
    assert start[2] == "Bearer secret-token" and '"version": "0.2.0"' in start[3]


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_a_failed_update_says_why():
    fake = _FakeCoordinator([_overview("failed", error="coordinator: migrate exited with 1")])
    try:
        r = _run_update(fake.url)
    finally:
        fake.close()
    assert r.returncode == 1
    assert "failed: coordinator: migrate exited with 1" in r.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_up_to_date_is_not_an_error():
    fake = _FakeCoordinator(
        [_overview()], start_status=409, start_detail="everything already runs 0.2.0"
    )
    try:
        r = _run_update(fake.url)
    finally:
        fake.close()
    assert r.returncode == 0, r.stderr
    assert "up to date: everything runs 0.2.0" in r.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_an_older_coordinator_is_moved_first_then_the_nodes_follow(tmp_path):
    """A coordinator from before the update API: registered and switched the earlier
    way, then the nodes are updated through the new API."""
    import json

    (tmp_path / "release.json").write_text(
        json.dumps({"version": "0.2.0", "images": {"coordinator": "c@sha256:" + "1" * 64}})
    )
    fake = _FakeCoordinator(
        [_overview("canary"), _overview("done", a="0.2.0", b="0.2.0")], legacy=True
    )
    try:
        r = _run_update(fake.url, etc=tmp_path)
    finally:
        fake.close()
    assert r.returncode == 0, r.stderr
    assert "predates one-click updates: moving it to 0.2.0" in r.stdout
    assert "ok  coordinator runs 0.2.0" in r.stdout
    assert "ok  updated to 0.2.0" in r.stdout
    calls = [(m, p) for m, p, _, _ in fake.requests if p != "/healthz"]
    assert calls[:4] == [
        ("POST", "/api/admin/updates/check"),  # 404: no update API yet
        ("POST", "/api/admin/releases"),
        ("PUT", "/api/admin/settings/coordinator.target_version"),
        ("POST", "/api/admin/updates/check"),
    ]
    register = next(b for m, p, _, b in fake.requests if p == "/api/admin/releases")
    assert json.loads(register)["version"] == "0.2.0"


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
@pytest.mark.parametrize("check_error", [None, "GitHub answered 403 for owner/repo"])
def test_an_explicit_release_is_the_one_rolled_out(tmp_path, check_error):
    """--version / --release-file: that release, registered first, not GitHub's latest
    (which need not even be known: GitHub down or checks off does not matter)."""
    import json

    (tmp_path / "release.json").write_text(
        json.dumps({"version": "0.1.5", "images": {"coordinator": "c@sha256:" + "1" * 64}})
    )
    fake = _FakeCoordinator([_overview("done", a="0.2.0", b="0.2.0")], check_error=check_error)
    try:
        r = _run_update(fake.url, etc=tmp_path, explicit=True)
    finally:
        fake.close()
    posts = [(p, json.loads(b) if b else None) for m, p, _, b in fake.requests if m == "POST"]
    assert (
        "/api/admin/releases",
        {"version": "0.1.5", "images": {"coordinator": "c@sha256:" + "1" * 64}},
    ) in posts
    start = next(b for p, b in posts if p == "/api/admin/updates/start")
    assert start == {"version": "0.1.5"}, r.stderr  # not the latest (0.2.0)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_a_failed_check_stops_an_update_to_the_latest():
    fake = _FakeCoordinator([_overview()], check_error="GitHub not reachable: ConnectError")
    try:
        r = _run_update(fake.url)
    finally:
        fake.close()
    assert r.returncode == 1 and "update check: GitHub not reachable" in r.stderr
    assert not [q for q in fake.requests if q[1] == "/api/admin/updates/start"]
