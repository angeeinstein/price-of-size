"""`pos-worker` command."""

import asyncio
import logging
import signal
import sys

from pos_worker.agent import Agent, RingLogHandler
from pos_worker.config import WorkerSettings


def main() -> None:
    ring = RingLogHandler()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), ring],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def run() -> int:
        agent = Agent(WorkerSettings(), log_buffer=ring)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, agent.stop)
        return await agent.run()

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
