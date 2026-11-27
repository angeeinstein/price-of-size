"""The worker agent: heartbeat/control loop, job loop, result flushing.

Desired state arrives with every heartbeat and the node reconciles towards it,
so a lost message does no harm. One-off commands run at most once (ids are
remembered in the spool) and are acknowledged with the next heartbeat.
"""

import asyncio
import collections
import contextlib
import logging
import socket
import time
import traceback
import uuid
from dataclasses import dataclass

import httpx

from pos_shared import api, exchange, signing
from pos_shared.version import API_VERSION, __version__, is_compatible
from pos_worker import cgroup, identity
from pos_worker.client import CoordinatorClient, CoordinatorError, IncompatibleCoordinator
from pos_worker.config import WorkerSettings
from pos_worker.handlers import HANDLERS, Handler, JobContext
from pos_worker.spool import Spool

log = logging.getLogger(__name__)

EXIT_RESTART = 75  # tells the supervisor (Docker restart policy, systemd) to start us again


@dataclass
class ActiveJob:
    job: api.LeasedJob
    task: asyncio.Task
    renew_at: float  # time.monotonic() at which the lease should be renewed


def _renew_after(lease_seconds: float) -> float:
    """Renew once a third of the lease has passed: two more tries before it runs out."""
    return max(lease_seconds / 3, 1.0)


class RingLogHandler(logging.Handler):
    """Keeps the last N log lines for the `fetch_logs` command."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        handlers: dict[str, Handler] | None = None,
        log_buffer: RingLogHandler | None = None,
        open_spool: bool = True,
    ):
        self.settings = settings
        self.handlers = handlers if handlers is not None else dict(HANDLERS)
        self.log_buffer = log_buffer
        self.identity = identity.load_or_create(settings.state_dir)
        self.client = CoordinatorClient(settings, self.identity, transport=transport)
        # selftest runs beside the live worker and must not touch its spool
        spool_path = settings.spool_path or str(settings.state_dir / "spool.db")
        self.spool = Spool(spool_path if open_spool else ":memory:", settings.spool_max_results)
        self.agent_dir = settings.agent_dir

        self.desired = api.DesiredState()
        self.local_paused = False
        self.compatible = True
        self.heartbeat_interval = 15.0
        self.limits = cgroup.read_limits(settings.cgroup_root, settings.state_dir)
        self.slots = self._compute_slots()
        self.active: dict[uuid.UUID, ActiveJob] = {}
        self.exit_code = 0
        self._stop = asyncio.Event()
        self._capacity_freed = asyncio.Event()
        self._result_ready = asyncio.Event()

    # --- lifecycle ---------------------------------------------------------

    @property
    def state(self) -> api.RunState:
        if self._stop.is_set() or self.desired.run_state == api.RunState.draining:
            return api.RunState.draining
        if self.local_paused or self.desired.run_state == api.RunState.paused:
            return api.RunState.paused
        return api.RunState.running

    async def ensure_enrolled(self) -> None:
        if self.identity.node_id is not None:
            return
        if not self.settings.enrollment_code:
            raise SystemExit("not enrolled: set POS_WORKER_ENROLLMENT_CODE")
        name = self.settings.name or socket.gethostname()
        resp = await self.client.enroll(self.settings.enrollment_code, name, self.capabilities)
        if not is_compatible(API_VERSION, resp.coordinator_api_version):
            raise IncompatibleCoordinator(resp.coordinator_api_version)
        identity.save_enrollment(self.settings.state_dir, resp.node_id, resp.signing_public_key)
        self.identity.node_id = resp.node_id
        self.identity.coordinator_signing_key = resp.signing_public_key
        log.info("enrolled as node %s", resp.node_id)

    @property
    def capabilities(self) -> list[str]:
        return sorted(self.handlers)

    def stop(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self._stop.set()
        self._capacity_freed.set()

    async def run(self, drain_timeout_s: float = 60.0) -> int:
        await self.ensure_enrolled()
        loops = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._resource_loop(), name="resources"),
            asyncio.create_task(self._job_loop(), name="jobs"),
            asyncio.create_task(self._flush_loop(), name="flush"),
            asyncio.create_task(self._lease_renewal_loop(), name="lease-renewal"),
        ]
        try:
            await self._stop.wait()
        finally:
            # Graceful drain: no new leases, let running jobs finish, flush what we can.
            # Lease renewal keeps running while jobs drain.
            if self.active:
                await asyncio.wait([a.task for a in self.active.values()], timeout=drain_timeout_s)
            for a in self.active.values():
                a.task.cancel()
            for t in loops:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            try:
                await self.flush_once()
            except Exception:
                log.warning("final flush failed; results stay spooled for the next start")
            await self.client.aclose()
            self.spool.close()
        return self.exit_code

    # --- resources ---------------------------------------------------------

    def _compute_slots(self) -> cgroup.Slots:
        return cgroup.compute_slots(
            self.limits,
            local_max_http=self.settings.max_http_slots,
            local_max_browser=self.settings.max_browser_slots,
            remote_max_http=self.desired.limits.max_http_slots,
            remote_max_browser=self.desired.limits.max_browser_slots,
            browser_capable="fetch_browser" in self.handlers,
        )

    async def _resource_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.resource_poll_s)
            self.limits = cgroup.read_limits(self.settings.cgroup_root, self.settings.state_dir)
            new = self._compute_slots()
            if new != self.slots:
                log.info("concurrency %s -> %s", self.slots, new)
                self.slots = new
                self._capacity_freed.set()

    # --- heartbeat / control ----------------------------------------------

    def _heartbeat_body(self) -> api.Heartbeat:
        lim = self.limits
        return api.Heartbeat(
            version=__version__,
            state=self.state,
            resources=api.Resources(
                cpu_limit=lim.cpu,
                mem_limit_bytes=lim.effective_mem,
                mem_used_bytes=lim.mem_used,
                cpu_load=lim.load,
                disk_free_bytes=lim.disk_free,
            ),
            concurrency=api.Concurrency(
                http_slots=self.slots.http,
                browser_slots=self.slots.browser,
                embed_batch=self.slots.embed_batch,
            ),
            agent=self._agent_status(),
            active_jobs=len(self.active),
            spooled_results=self.spool.result_count(),
            command_results=[api.CommandResult(**c) for c in self.spool.unreported_commands()],
        )

    async def heartbeat_once(self) -> api.HeartbeatResponse:
        body = self._heartbeat_body()
        resp = await self.client.heartbeat(body)
        self.spool.mark_commands_reported([str(c.id) for c in body.command_results])
        if not is_compatible(API_VERSION, resp.coordinator_api_version):
            if self.compatible:
                log.error(
                    "coordinator API %s is incompatible; refusing jobs",
                    resp.coordinator_api_version,
                )
            self.compatible = False
        else:
            self.compatible = True
        self.heartbeat_interval = resp.heartbeat_interval_s
        if resp.desired != self.desired:
            log.info("desired state: %s", resp.desired.model_dump(mode="json"))
            self.desired = resp.desired
            self.slots = self._compute_slots()
            self._capacity_freed.set()
        for cmd in resp.commands:
            await self._run_command(cmd)
        if self.agent_dir is not None:
            await self._ensure_signing_key()
            self._offer_signing_key()
            exchange.write_health(self.agent_dir, __version__, node_id=str(self.identity.node_id))
            await self._reconcile_release(resp.desired.target_version)
        return resp

    # --- updates (through the node agent) ------------------------------------

    async def _ensure_signing_key(self) -> None:
        """Identities from before keys were part of enrollment get the key now, over the
        authenticated channel, so they can take part in signed updates."""
        if self.identity.coordinator_signing_key or self.identity.node_id is None:
            return
        key = await self.client.get_signing_key()
        identity.save_enrollment(self.settings.state_dir, self.identity.node_id, key)
        self.identity.coordinator_signing_key = key
        log.info("fetched the coordinator signing key for an existing identity")

    def _offer_signing_key(self) -> None:
        """The agent pins this once (trust on first use) in its own private volume."""
        key = self.identity.coordinator_signing_key
        assert self.agent_dir is not None
        path = self.agent_dir / exchange.PINNED_KEY
        if key and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(key + "\n")
            tmp.replace(path)

    def _agent_status(self) -> api.AgentStatus | None:
        if self.agent_dir is None:
            return None
        raw = exchange.read_json(self.agent_dir / exchange.STATUS)
        if not isinstance(raw, dict):
            return None
        try:
            return api.AgentStatus.model_validate(
                {k: v for k, v in raw.items() if k in api.AgentStatus.model_fields}
            )
        except ValueError:
            return None

    async def _reconcile_release(self, target: str | None) -> None:
        """Hand the signed manifest for the target version to the node agent.

        The agent verifies the signature itself against its own pinned key; the
        check here only avoids handing over something that will be refused.
        """
        if self.agent_dir is None:
            return
        path = self.agent_dir / exchange.DESIRED
        if target is None:
            # A cleared target cancels a pending update: the agent reads this file on
            # every round, so it must not outlive the coordinator's decision.
            path.unlink(missing_ok=True)
            return
        current = exchange.read_json(path)
        if isinstance(current, dict) and current.get("manifest", {}).get("version") == target:
            return
        signed = await self.client.get_release(target)
        manifest, problem = signing.open_signed_manifest(
            signed, self.identity.coordinator_signing_key
        )
        if manifest is None or manifest.version != target:
            log.error("release %s refused (%s); not handing it to the agent", target, problem)
            return
        exchange.write_json(path, signed)  # as received: the agent checks the same signature
        log.info("requested update to %s from the node agent", target)

    async def selftest(self) -> dict[str, object]:
        """Checks for `pos-worker selftest`. Values are True or an error description."""
        report: dict[str, object] = {}
        report["identity"] = (
            True if self.identity.node_id is not None else "not enrolled (no node id)"
        )
        report["handlers"] = True if self.handlers else "no job handlers"
        try:
            await self.client.check_auth()
            report["coordinator"] = True
        except Exception as e:  # noqa: BLE001 - the report shows whatever went wrong
            report["coordinator"] = f"{type(e).__name__}: {e}"
        return report

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self.heartbeat_once()
            except IncompatibleCoordinator:
                self.compatible = False
                log.error("coordinator refused our API version %s", API_VERSION)
            except (CoordinatorError, httpx.HTTPError) as e:
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(self.heartbeat_interval)

    async def _run_command(self, cmd: api.Command) -> None:
        cid = str(cmd.id)
        if self.spool.command_seen(cid):
            return
        ok, output = True, None
        try:
            if cmd.type == "restart":
                self.spool.record_command(cid, True, "restarting")
                log.info("restart requested by coordinator")
                self.stop(EXIT_RESTART)
                return
            elif cmd.type == "fetch_logs":
                n = int(cmd.args.get("lines", 200))
                output = list(self.log_buffer.lines)[-n:] if self.log_buffer else []
            elif cmd.type == "diagnostics":
                output = {
                    "limits": self.limits.__dict__,
                    "slots": self.slots.__dict__,
                    "active_jobs": len(self.active),
                    "spooled_results": self.spool.result_count(),
                    "state": self.state.value,
                    "capabilities": self.capabilities,
                }
            else:
                ok, output = False, f"unknown command {cmd.type!r}"
        except Exception as e:
            ok, output = False, repr(e)
        self.spool.record_command(cid, ok, output)

    # --- jobs ----------------------------------------------------------------

    def _free_slots(self) -> int:
        return max(self.slots.http - len(self.active), 0)

    def _job_types(self) -> list[str]:
        types = [t for t in self.handlers if t != "fetch_browser" or self.slots.browser > 0]
        if self.desired.job_types is not None:
            types = [t for t in types if t in self.desired.job_types]
        return types

    async def lease_once(self) -> int:
        if self.state != api.RunState.running or not self.compatible or self.spool.full:
            return 0
        free, types = self._free_slots(), self._job_types()
        if free <= 0 or not types:
            return 0
        requested_at = time.monotonic()  # conservative: the lease started no earlier
        jobs = await self.client.lease(types, free)
        for job in jobs:
            task = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
            renew_at = requested_at + _renew_after(job.lease_seconds)
            self.active[job.id] = ActiveJob(job=job, task=task, renew_at=renew_at)
        return len(jobs)

    async def _job_loop(self) -> None:
        while True:
            got = 0
            try:
                got = await self.lease_once()
            except IncompatibleCoordinator:
                self.compatible = False
            except (CoordinatorError, httpx.HTTPError) as e:
                log.warning("lease failed: %s", e)
            if got == 0:
                self._capacity_freed.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._capacity_freed.wait(), timeout=self.settings.lease_poll_s
                    )

    async def _execute(self, job: api.LeasedJob) -> None:
        ctx = JobContext(upload_raw=self.client.upload_raw)
        try:
            result = await self.handlers[job.type](job, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            result = api.JobResult(
                job_id=job.id,
                attempt=job.attempt,
                outcome=api.JobOutcome.failed,
                error=f"{e!r}\n{traceback.format_exc()}"[-20000:],
                retryable=True,
            )
        entry = self.active.get(job.id)
        if entry is not None and entry.job.attempt == job.attempt:
            # Spooled even if a renewal failed: the coordinator still accepts a late
            # result for the current attempt until the job is leased again.
            self.spool.add_result(str(job.id), job.attempt, result.model_dump(mode="json"))
            del self.active[job.id]
            self._result_ready.set()
            self._capacity_freed.set()

    # --- lease renewal ---------------------------------------------------------

    async def renew_leases_once(self, now: float | None = None) -> int:
        """Renew the leases that are due. Returns how many were renewed."""
        now = time.monotonic() if now is None else now
        due = [a for a in self.active.values() if a.renew_at <= now]
        if not due:
            return 0
        started = time.monotonic()
        resp = await self.client.extend(
            [api.LeaseRef(job_id=a.job.id, attempt=a.job.attempt) for a in due]
        )
        renewed = set(resp.extended)
        for a in due:
            if a.job.id in renewed and self.active.get(a.job.id) is a:
                a.renew_at = started + _renew_after(resp.lease_seconds)
        sent_attempts = {a.job.id: a.job.attempt for a in due}
        for job_id in resp.lost:
            entry = self.active.get(job_id)
            # The response is about the attempt we asked for; a newer attempt of the
            # same job leased meanwhile is a different, valid execution.
            if entry is None or entry.job.attempt != sent_attempts.get(job_id):
                continue
            del self.active[job_id]
            log.warning("lost lease on job %s (attempt %s); cancelling", job_id, entry.job.attempt)
            entry.task.cancel()
            self._capacity_freed.set()
        return len(renewed)

    async def _lease_renewal_loop(self) -> None:
        """Independent of the heartbeat, so no heartbeat interval can starve leases."""
        while True:
            try:
                await self.renew_leases_once()
            except (CoordinatorError, httpx.HTTPError) as e:
                log.warning("lease renewal failed (will retry): %s", e)
            await asyncio.sleep(self.settings.lease_check_s)

    # --- results -------------------------------------------------------------

    async def flush_once(self) -> int:
        sent = 0
        while batch := self.spool.peek_results(self.settings.result_batch_size):
            ack = await self.client.push_results(batch)
            sent_attempts = {r["job_id"]: r["attempt"] for r in batch}
            done = [str(i) for i in ack.accepted] + list(ack.rejected)
            for job_id, reason in ack.rejected.items():
                log.warning("result for job %s rejected: %s", job_id, reason)
            # Remove exactly what was sent: a newer attempt's result for the same job,
            # spooled while this batch was in flight, must survive.
            self.spool.remove_results([(j, sent_attempts[j]) for j in done if j in sent_attempts])
            sent += len(done)
            if not done:
                break
        return sent

    async def _flush_loop(self) -> None:
        while True:
            try:
                await self.flush_once()
            except (CoordinatorError, httpx.HTTPError) as e:
                log.warning("flush failed (results stay spooled): %s", e)
            self._result_ready.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._result_ready.wait(), timeout=self.settings.flush_interval_s
                )
