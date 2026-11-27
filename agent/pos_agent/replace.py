"""`pos-agent replace`: runs in a short-lived helper container started from the
NEW agent image and swaps the agent container.

    1. create <name>-next from the old container's configuration, new image
    2. stop the old agent (its lock is released)
    3. start <name>-next and wait until it reports healthy with the new image
    4. ok:   remove the old container, rename <name>-next to <name>
       fail: remove <name>-next, start the old agent again

The worker keeps running throughout; only the agent is swapped. The outcome
is written to the agent's data volume for the (new or old) agent to read.
"""

import asyncio
import logging
import time
from typing import Any

from pos_agent.docker import Docker
from pos_shared import exchange

log = logging.getLogger(__name__)


def clone_config(info: dict[str, Any], image: str) -> dict[str, Any]:
    """Docker create-config equivalent to an inspected container, with a new image."""
    cfg = info["Config"]
    env = [e for e in cfg.get("Env") or [] if not e.startswith("POS_AGENT_SELF_IMAGE=")]
    env.append(f"POS_AGENT_SELF_IMAGE={image}")
    host = {
        k: v
        for k, v in info["HostConfig"].items()
        if k
        in (
            "Binds",
            "Mounts",
            "RestartPolicy",
            "NetworkMode",
            "LogConfig",
            "Init",
            "PortBindings",
            "ExtraHosts",
        )
        and v
    }
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    out: dict[str, Any] = {
        "Image": image,
        "Env": env,
        "Labels": {**(cfg.get("Labels") or {}), "org.price-of-size.image": image},
        "HostConfig": host,
    }
    if cfg.get("Cmd"):
        out["Cmd"] = cfg["Cmd"]
    if networks:
        name, ep = next(iter(networks.items()))
        out["NetworkingConfig"] = {"EndpointsConfig": {name: {"Aliases": ep.get("Aliases")}}}
    return out


async def replace(
    docker: Docker,
    container: str,
    image: str,
    version: str,
    data_dir,
    health_timeout_s: float = 120.0,
    stop_timeout_s: int = 60,
) -> bool:
    result_path = data_dir / "agent-update.json"
    health_path = data_dir / "agent-health.json"
    nxt = f"{container}-next"
    info = await docker.inspect(container)
    if info is None:
        log.error("agent container %s not found", container)
        return False
    await docker.remove(nxt)
    await docker.create(nxt, clone_config(info, image))
    since = time.time()
    await docker.stop(container, stop_timeout_s)
    ok, error = False, None
    try:
        await docker.start(nxt)
        deadline = time.monotonic() + health_timeout_s
        while time.monotonic() < deadline:
            h = exchange.read_json(health_path)
            if isinstance(h, dict) and h.get("image") == image and h.get("ok_at", 0) >= since:
                ok = True
                break
            await asyncio.sleep(2)
        if not ok:
            error = "new agent did not report healthy in time"
    except Exception as e:  # noqa: BLE001 - any failure means roll back
        error = f"{type(e).__name__}: {e}"

    if ok:
        await docker.remove(container)
        await docker.rename(nxt, container)
        exchange.write_json(result_path, {"version": version, "result": "ok"})
        return True
    log.error("agent update failed (%s); restoring the old agent", error)
    await docker.remove(nxt)
    await docker.start(container)
    exchange.write_json(result_path, {"version": version, "result": "failed", "error": error})
    return False
