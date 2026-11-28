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
