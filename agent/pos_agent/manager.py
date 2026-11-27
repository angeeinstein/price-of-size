"""The agent's reconcile loop: keep the service running, update it to signed
releases in A/B slots with health checks and rollback, and update the agent
itself through a helper container.

Invariants:
* At most one update runs at a time, and the service and the agent are never
  updated together: the agent updates itself only once the service runs the
  desired version.
* Nothing is stopped before the new version passed its self-test (and, for
  the coordinator, its backup and migrations).
* If the new version does not become healthy, the old one is started again;
  the failed version is not retried for `failed_retry_after_s`.
* The previous version stays in the other slot until the next update, so a
  rollback is starting a container that already exists.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pos_agent.config import AgentSettings
from pos_agent.docker import Docker, DockerError
from pos_agent.services import Service, labels, make_service
from pos_agent.state import AgentState, Slot, other
from pos_shared import exchange, signing
from pos_shared.api import ReleaseManifest, SignedManifest
from pos_shared.version import __version__

log = logging.getLogger(__name__)

STATE_FILE = "state.json"
PINNED_KEY_FILE = "signing_key.pub"
AGENT_HEALTH_FILE = "agent-health.json"
AGENT_UPDATE_RESULT_FILE = "agent-update.json"
HEALTH_POLL_S = 2.0


class UpdateFailed(Exception):
    pass


class Manager:
    def __init__(
        self,
        settings: AgentSettings,
        docker: Docker,
        service: Service | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        self.docker = docker
        self.service = service or make_service(settings, docker)
        self.clock = clock
        self.sleep = sleep
        self.data = settings.data_dir
        self.data.mkdir(parents=True, exist_ok=True)
        self.problem: str | None = None  # last non-update problem, shown in status

    # --- trust ---------------------------------------------------------------------

    def pinned_key(self) -> str | None:
        """The coordinator signing key, pinned on first sight (trust on first use).

        It is taken from the exchange volume once and then kept in the agent's
        private volume, which the service cannot write.
        """
        path = self.data / PINNED_KEY_FILE
        if path.exists():
            return path.read_text().strip() or None
        offered = self.settings.exchange_dir / exchange.PINNED_KEY
        if not offered.exists():
            return None
        key = offered.read_text().strip()
        if not key:
            return None
        path.write_text(key + "\n")
        log.info("pinned coordinator signing key %s", signing.key_id(key))
        return key

    def desired_manifest(self) -> ReleaseManifest | None:
        raw = exchange.read_json(self.settings.exchange_dir / exchange.DESIRED)
        if raw is None:
            return None
        try:
            signed = SignedManifest.model_validate(raw)
        except ValueError as e:
            self.problem = f"desired release is malformed: {e}"
            return None
        key = self.pinned_key()
        payload = signed.manifest.model_dump(mode="json")
        if key is None or not signing.verify(key, payload, signed.signature):
            self.problem = (
                f"desired release {signed.manifest.version} is not signed by the pinned key"
            )
            log.error(self.problem)
            return None
        return signed.manifest

    def bootstrap_images(self) -> tuple[str | None, dict[str, str]]:
        raw = exchange.read_json(self.settings.bootstrap_release)
        if not isinstance(raw, dict):
            return None, {}
        return raw.get("version"), dict(raw.get("images", {}))

    # --- loop ------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception:
                log.exception("reconcile round failed")
            await self.sleep(self.settings.check_interval_s)

    async def reconcile_once(self) -> AgentState:
        st = AgentState.load(self.data / STATE_FILE)
        self.problem = None
        self.pinned_key()  # pin as early as possible: the trust-on-first-use window
        self._consume_agent_update_result(st)
        self._apply_extra_images(st)
        await self.service.prepare()
        if st.active is None:
            await self._bootstrap(st)
        else:
            await self._ensure_running(st)

        desired = self.desired_manifest()
        active = st.active_slot
        if desired is not None and active is not None:
            if desired.version != active.version:
                if st.may_try(desired.version, self.settings.failed_retry_after_s, self.clock()):
                    await self.update(st, desired)
            else:
                await self._maybe_update_self(st, desired)
        self._save(st)
        self._write_agent_health()
        return st

    def _save(self, st: AgentState) -> None:
        st.save(self.data / STATE_FILE)
        active = st.active_slot
        exchange.write_json(
            self.settings.exchange_dir / exchange.STATUS,
            {
                "agent_version": __version__,
                "agent_image": self.settings.self_image,
                "role": self.settings.role,
                "active_slot": st.active,
                "running_version": active.version if active else None,
                "update_state": st.update_state,
                "update_target": st.update_target,
                "update_error": st.update_error or self.problem,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def _write_agent_health(self) -> None:
        exchange.write_json(
            self.data / AGENT_HEALTH_FILE,
            {"version": __version__, "image": self.settings.self_image, "ok_at": self.clock()},
        )

    def _apply_extra_images(self, st: AgentState) -> None:
        """Images besides the service itself (Caddy for the coordinator role)."""
        caddy = st.extra_images.get("caddy")
        if caddy is None and st.active is None:
            caddy = self.bootstrap_images()[1].get("caddy")
        if hasattr(self.service, "caddy_image"):
            self.service.caddy_image = caddy

    # --- service lifecycle -------------------------------------------------------------

    async def _ensure_image(self, image: str) -> None:
        if not await self.docker.image_exists(image):
            log.info("pulling %s", image)
            await self.docker.pull(image)

    async def _start_slot(self, slot: str, image: str, version: str | None) -> None:
        name = self.service.container_name(slot)
        await self.docker.remove(name)
        await self.docker.create(name, self.service.container_config(image, slot, version))
        await self.docker.start(name)

    async def _bootstrap(self, st: AgentState) -> None:
        version, images = self.bootstrap_images()
        image = images.get(self.service.component)
        if image is None:
            self.problem = f"no bootstrap image for {self.service.component}"
            log.error(self.problem)
            return
        log.info("first start: %s %s in slot a", self.service.component, version)
        await self._ensure_image(image)
        await self.service.pre_switch(image, version or "bootstrap", backup=False)
        await self._start_slot("a", image, version)
        st.active = "a"
        st.slots["a"] = Slot(version=version, image=image)
        st.extra_images = {k: v for k, v in images.items() if k == "caddy"}
        self._apply_extra_images(st)

    async def _ensure_running(self, st: AgentState) -> None:
        slot = st.active
        assert slot is not None
        current = st.slots[slot]
        name = self.service.container_name(slot)
        info = await self.docker.inspect(name)
        if info is None:
            if current.image is None:
                self.problem = f"active slot {slot} has no image recorded"
                return
            log.warning("%s is missing; recreating", name)
            await self._ensure_image(current.image)
            await self._start_slot(slot, current.image, current.version)
        elif not info["State"].get("Running") and not info["State"].get("Restarting"):
            log.warning("%s is not running (%s); starting", name, info["State"].get("Status"))
            await self.docker.start(name)
        # Never two workers at once: the inactive slot must stay stopped.
        idle = await self.docker.inspect(self.service.container_name(other(slot)))
        if idle is not None and idle["State"].get("Running"):
            log.warning("inactive slot %s was running; stopping it", other(slot))
            await self.docker.stop(self.service.container_name(other(slot)), 30)

    async def update(self, st: AgentState, manifest: ReleaseManifest) -> None:
        version = manifest.version
        component = self.service.component
        image = manifest.images.get(component)
        if image is None:
            st.record_failure(
                version, f"release {version} has no image for {component}", self.clock()
            )
            return
        old_slot = st.active
        assert old_slot is not None
        new_slot = other(old_slot)
        old_name = self.service.container_name(old_slot)
        new_name = self.service.container_name(new_slot)
        log.info(
            "updating %s %s -> %s (slot %s -> %s)",
            component,
            st.slots[old_slot].version,
            version,
            old_slot,
            new_slot,
        )
        old_stopped = new_started = False

        def phase(name: str) -> None:
            st.update_state, st.update_target, st.update_error = name, version, None
            self._save(st)

        try:
            phase("pulling")
            await self.docker.pull(image)
            for extra in self.service.extra_components:
                if extra in manifest.images:
                    await self.docker.pull(manifest.images[extra])

            phase("selftest")
            await self.service.run_once(
                f"{self.settings.name_prefix}-{component}-selftest",
                self.service.selftest_config(image),
                self.settings.selftest_timeout_s,
            )

            phase("preparing")
            await self.service.pre_switch(image, version)

            if self.service.stop_old_first:
                phase("draining")
                await self.docker.stop(old_name, self.settings.drain_timeout_s)
                old_stopped = True

            phase("starting")
            since = self.clock()
            await self._start_slot(new_slot, image, version)
            new_started = True

            phase("health_check")
            if not await self._wait_healthy(new_slot, version, since):
                raise UpdateFailed(f"{new_name} did not become healthy in time")

            if not self.service.stop_old_first:
                await self.sleep(self.settings.switch_grace_s)  # let in-flight requests finish
                await self.docker.stop(old_name, 30)
        except Exception as e:
            log.error("update to %s failed: %s", version, e)
            await self._rollback(old_name, new_name, old_stopped, new_started)
            st.slots[new_slot] = Slot()
            st.record_failure(version, f"{type(e).__name__}: {e}", self.clock())
            return

        st.active = new_slot
        st.slots[new_slot] = Slot(version=version, image=image)
        if "caddy" in self.service.extra_components and "caddy" in manifest.images:
            st.extra_images["caddy"] = manifest.images["caddy"]
            self._apply_extra_images(st)
            await self.service.prepare()
        st.failed.pop(version, None)
        st.update_state, st.update_target, st.update_error = "idle", None, None
        log.info("now running %s %s in slot %s", component, version, new_slot)

    async def _wait_healthy(self, slot: str, version: str, since: float) -> bool:
        deadline = self.clock() + self.settings.health_timeout_s
        while self.clock() < deadline:
            if await self.service.is_healthy(slot, version, since):
                return True
            await self.sleep(HEALTH_POLL_S)
        return False

    async def _rollback(self, old_name: str, new_name: str, old_stopped: bool, new_started: bool):
        try:
            if new_started:
                await self.docker.stop(new_name, 30)
                await self.docker.remove(new_name)
            info = await self.docker.inspect(old_name)
            if info is not None and (old_stopped or not info["State"].get("Running")):
                await self.docker.start(old_name)
        except DockerError as e:
            # The next reconcile round retries via _ensure_running.
            log.error("rollback incomplete: %s", e)

    # --- agent self-update -------------------------------------------------------------

    def _consume_agent_update_result(self, st: AgentState) -> None:
        path = self.data / AGENT_UPDATE_RESULT_FILE
        result = exchange.read_json(path)
        if not isinstance(result, dict):
            return
        key = f"agent:{result.get('version')}"
        if result.get("result") == "ok":
            st.failed.pop(key, None)
            log.info("agent updated to %s", result.get("version"))
        else:
            st.failed[key] = {"at": self.clock(), "error": str(result.get("error"))[:2000]}
            log.error("agent update to %s failed: %s", result.get("version"), result.get("error"))
        path.unlink(missing_ok=True)

    async def _maybe_update_self(self, st: AgentState, manifest: ReleaseManifest) -> None:
        target = manifest.images.get("agent")
        if target is None or self.settings.self_image in (None, target):
            return
        if not st.may_try(
            f"agent:{manifest.version}", self.settings.failed_retry_after_s, self.clock()
        ):
            return
        log.info("updating the agent itself to %s", manifest.version)
        st.update_state, st.update_target = "agent_update", manifest.version
        self._save(st)
        await self.docker.pull(target)
        helper = f"{self.settings.container_name}-updater"
        await self.docker.remove(helper)
        await self.docker.create(helper, self._helper_config(target, manifest.version))
        await self.docker.start(helper)  # the helper stops this container next

    def _helper_config(self, image: str, version: str) -> dict[str, Any]:
        return {
            "Image": image,
            "Cmd": [
                "pos-agent",
                "replace",
                "--container",
                self.settings.container_name,
                "--image",
                image,
                "--version",
                version,
            ],
            "Env": [f"POS_AGENT_DATA_DIR={self.data}"],
            "Labels": labels(self.settings, "agent-updater", None, version),
            "HostConfig": {
                "AutoRemove": True,
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": self.settings.docker_socket,
                        "Target": "/var/run/docker.sock",
                    },
                    {
                        "Type": "volume",
                        "Source": self.settings.agent_data_volume,
                        "Target": str(self.data),
                    },
                ],
            },
        }
