"""`pos-worker` command.

pos-worker            run the worker (default)
pos-worker selftest   check this image can run here: config, identity,
                      coordinator reachable and accepting our credentials
                      and API version. Exit 0 = healthy. The node agent
                      runs this in a new image before switching to it.
"""

import argparse
import asyncio
import logging
import signal
import sys

from pos_worker.config import WorkerSettings
from pos_worker.runtime import RingLogHandler, Worker


def _setup_logging() -> RingLogHandler:
    ring = RingLogHandler()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), ring],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return ring


async def _run(ring: RingLogHandler) -> int:
    worker = Worker(WorkerSettings(), log_buffer=ring)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    return await worker.run()


async def _selftest() -> int:
    worker = Worker(WorkerSettings(), open_spool=False)
    try:
        report = await worker.selftest()
    finally:
        await worker.client.aclose()
    for check, ok in report.items():
        print(f"{'ok  ' if ok is True else 'FAIL'} {check}{'' if ok is True else ': ' + str(ok)}")
    return 0 if all(v is True for v in report.values()) else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="pos-worker")
    parser.add_argument("cmd", nargs="?", default="run", choices=["run", "selftest"])
    args = parser.parse_args()
    ring = _setup_logging()
    if args.cmd == "selftest":
        sys.exit(asyncio.run(_selftest()))
    sys.exit(asyncio.run(_run(ring)))


if __name__ == "__main__":
    main()
