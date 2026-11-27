"""`pos-agent` command.

pos-agent             run the reconcile loop (default)
pos-agent replace     swap the agent container (used by the self-update helper)
pos-agent status      print the agent state
"""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time

from pos_agent.config import AgentSettings
from pos_agent.docker import Docker, RegistryAuth
from pos_agent.manager import STATE_FILE, Manager
from pos_agent.replace import replace

log = logging.getLogger("pos_agent")


def _docker(settings: AgentSettings) -> Docker:
    auth = None
    if settings.registry_username and settings.registry_password:
        auth = RegistryAuth(
            settings.registry_username, settings.registry_password, settings.registry_server
        )
    return Docker(settings.docker_socket, registry_auth=auth)


def _lock(settings: AgentSettings):
    """Only one agent may act on a node; a second one waits for the lock."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.data_dir / "agent.lock", os.O_RDWR | os.O_CREAT, 0o600)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            log.warning("another agent holds the lock; waiting")
            time.sleep(5)


async def _run(settings: AgentSettings) -> None:
    docker = _docker(settings)
    manager = Manager(settings, docker)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await manager.run_forever()
    except asyncio.CancelledError:
        log.info("agent stopping")
    finally:
        await docker.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="pos-agent")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    rp = sub.add_parser("replace")
    rp.add_argument("--container", required=True)
    rp.add_argument("--image", required=True)
    rp.add_argument("--version", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    settings = AgentSettings()

    if args.cmd == "replace":

        async def go() -> bool:
            docker = _docker(settings)
            try:
                return await replace(
                    docker,
                    args.container,
                    args.image,
                    args.version,
                    settings.data_dir,
                    settings.agent_health_timeout_s,
                )
            finally:
                await docker.aclose()

        sys.exit(0 if asyncio.run(go()) else 1)
    if args.cmd == "status":
        path = settings.data_dir / STATE_FILE
        print(json.dumps(json.loads(path.read_text()) if path.exists() else {}, indent=2))
        return
    _lock(settings)
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
