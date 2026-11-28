"""install.sh, piece by piece: functions run in bash against fake system files."""

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
    [(UBUNTU, "ubuntu|debian|noble"), (DEBIAN, "debian||bookworm")],
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
