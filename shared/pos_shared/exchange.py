"""File exchange between a service (worker or coordinator) and its node agent.

The agent manages containers through the Docker socket and never talks to the
coordinator; the service never touches Docker. They meet in a shared directory:

    desired.json   written by the service: the signed release it should run
    status.json    written by the agent:   slots, versions, update progress
    health.json    written by the service: proof of life after a (re)start

All writes are atomic (temp file + rename), so a reader never sees half a file.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

DESIRED = "desired.json"
STATUS = "status.json"
HEALTH = "health.json"
PINNED_KEY = "signing_key.pub"

# `pos-coordinator migrations-pending` answers with its exit code; anything else
# (e.g. a release from before the command existed) means it cannot tell.
MIGRATIONS_PENDING = 0
NO_MIGRATIONS_PENDING = 3


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> Any | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_health(directory: Path, version: str, **extra: Any) -> None:
    write_json(directory / HEALTH, {"version": version, "ok_at": time.time(), **extra})
