"""Updates of the whole installation from this project's GitHub releases.

`check()` asks GitHub for the latest release and reads its `release.json` (every
image pinned by digest). `start()` registers and signs that release, as an admin
would by hand on the Releases page, points the coordinator's own agent at it and
records a rollout. `advance()` (maintenance loop) moves the rollout along:

    coordinator   the coordinator's agent pulls, backs up, migrates and switches;
                  this step ends when the coordinator answering is the new version
    canary        one online node updates (its agent drains, switches, checks)
    nodes         the other nodes update; done when every online node runs it

A step that fails ends the rollout ("failed"); the agents have rolled back by then,
and an agent tries a version that failed again only after a while (6 hours). With
the coordinator already on the version, starting again rolls it out to the nodes.
Nodes that are offline keep the new target and update when they are back. The same
code serves the panel's Update button and `install.sh --update`.
"""

import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pos_coordinator import runtime_settings
from pos_coordinator.models import AuditLog, Node, Release, UpdateRollout
from pos_shared import exchange
from pos_shared.api import VERSION_PATTERN, ReleaseManifest
from pos_shared.version import __version__

log = logging.getLogger(__name__)

REQUIRED_IMAGES = ("coordinator", "worker", "agent")
ACTIVE = ("coordinator", "canary", "nodes")
MAX_RELEASE_JSON = 64 * 1024
GITHUB_API = "https://api.github.com"


class UpdateError(Exception):
    """An update that cannot be checked or started; the message is for people."""


def version_key(version: str) -> tuple:
    """Order of release versions: 0.1.10 after 0.1.9; 1.0.0-rc1 before 1.0.0."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", version)
    if m is None:
        return (-1,)
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


@dataclass
class Available:
    version: str
    tag: str
    url: str | None
    notes: str
    published_at: str | None
    images: dict[str, str]


@dataclass
class CheckResult:
    checked_at: datetime
    repository: str
    latest: Available | None = None
    error: str | None = None

    def view(self) -> dict[str, Any]:
        out = asdict(self)
        if self.latest is not None:
            out["latest"].pop("images")
        return out


def client(timeout_s: float = 20.0, **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout_s,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"price-of-size-coordinator/{__version__}",
        },
        **kw,
    )


async def fetch_release(http: httpx.AsyncClient, repository: str, tag: str | None = None):
    """The latest (or the tagged) release and its release.json, checked."""
    path = f"releases/tags/{tag}" if tag else "releases/latest"
    try:
        r = await http.get(f"{GITHUB_API}/repos/{repository}/{path}")
    except httpx.HTTPError as e:
        raise UpdateError(f"GitHub not reachable: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise UpdateError(f"no {'release ' + tag if tag else 'release'} in {repository}")
    if r.status_code != 200:
        raise UpdateError(f"GitHub answered {r.status_code} for {repository}")
    data = r.json()
    tag_name = str(data.get("tag_name") or "")
    version = tag_name.removeprefix("v")
    if not re.fullmatch(VERSION_PATTERN, version):
        raise UpdateError(f"release tag {tag_name!r} is not a version (vX.Y.Z)")
    asset = next((a for a in data.get("assets") or [] if a.get("name") == "release.json"), None)
    if asset is None:
        raise UpdateError(
            f"release {tag_name} has no release.json yet (its images may still be building)"
        )
    try:
        rj = await http.get(asset["browser_download_url"], follow_redirects=True)
    except httpx.HTTPError as e:
        raise UpdateError(f"release.json of {tag_name} not downloadable: {e}") from e
    if rj.status_code != 200 or len(rj.content) > MAX_RELEASE_JSON:
        raise UpdateError(f"release.json of {tag_name}: HTTP {rj.status_code}")
    try:
        body = rj.json()
        images = dict(body["images"])
        if body.get("version") != version:
            raise UpdateError(f"release.json says {body.get('version')!r}, the tag {tag_name!r}")
        ReleaseManifest(version=version, images=images, created_at=datetime.now(UTC))
    except UpdateError:
        raise
    except (ValueError, KeyError, TypeError) as e:
        raise UpdateError(f"release.json of {tag_name} is not valid: {e}") from e
    missing = [c for c in REQUIRED_IMAGES if c not in images]
    if missing:
        raise UpdateError(f"release.json of {tag_name} has no image for {', '.join(missing)}")
    return Available(
        version=version,
        tag=tag_name,
        url=data.get("html_url"),
        notes=str(data.get("body") or "")[:10000],
        published_at=data.get("published_at"),
        images=images,
    )


async def check(repository: str, http: httpx.AsyncClient | None = None) -> CheckResult:
    result = CheckResult(checked_at=datetime.now(UTC), repository=repository)
    if not repository:
        result.error = "update checks are off (POS_UPDATE_REPOSITORY is empty)"
        return result
    own = http is None
    http = http or client()
    try:
        result.latest = await fetch_release(http, repository)
    except UpdateError as e:
        result.error = str(e)
    finally:
        if own:
            await http.aclose()
    return result


def _repository(ref: str) -> str:
    return ref.split("@", 1)[0]


async def _reference_images(session: AsyncSession) -> dict[str, str]:
    """The images of the release running now (if it was registered)."""
    release = await session.get(Release, __version__)
    return dict(release.manifest.get("images", {})) if release else {}


def check_repositories(images: dict[str, str], reference: dict[str, str]) -> None:
    """An update never moves a component to another image repository on its own: a
    changed repository is a decision for a person (register it by hand)."""
    for component, ref in images.items():
        old = reference.get(component)
        if old is not None and _repository(old) != _repository(ref):
            raise UpdateError(
                f"the {component} image would move from {_repository(old)} to "
                f"{_repository(ref)}; if that is intended, register the release by hand "
                "on the Releases page"
            )


async def active_rollout(session: AsyncSession, lock: bool = False) -> UpdateRollout | None:
    q = select(UpdateRollout).where(UpdateRollout.status.in_(ACTIVE))
    if lock:
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one_or_none()


async def start(
    session: AsyncSession,
    *,
    key: Any,
    agent_dir: Path | None,
    repository: str,
    version: str,
    actor: str,
    http: httpx.AsyncClient | None = None,
) -> UpdateRollout:
    """Begin updating everything to `version` (the caller commits)."""
    from pos_coordinator.api.releases import register

    if agent_dir is None:
        raise UpdateError(
            "this coordinator is not run by its agent, so it cannot update itself: "
            "update it with install.sh"
        )
    if await active_rollout(session, lock=True) is not None:
        raise UpdateError("an update is already running")
    if version_key(version) < version_key(__version__):
        raise UpdateError(f"{version} is older than the running {__version__}")
    if version == __version__ and not [
        n for n in await _managed_nodes(session) if n.version != version
    ]:
        raise UpdateError(f"everything already runs {version}")
    release = await session.get(Release, version)
    if release is None:
        own = http is None
        http = http or client()
        try:
            available = await fetch_release(http, repository, tag=f"v{version}")
        finally:
            if own:
                await http.aclose()
        check_repositories(available.images, await _reference_images(session))
        try:
            register(session, key, version, available.images, available.notes or None, actor)
        except ValueError as e:
            raise UpdateError(f"release {version} is not valid: {e}") from e
    await runtime_settings.put(session, "coordinator.target_version", version, actor)
    rollout = UpdateRollout(
        version=version, from_version=__version__, status="coordinator", started_by=actor
    )
    session.add(rollout)
    session.add(AuditLog(actor=actor, action="update.start", target=version,
                         details={"from": __version__}))  # fmt: skip
    await session.flush()
    return rollout


async def cancel(session: AsyncSession, actor: str) -> UpdateRollout | None:
    """Stop the rollout: nodes not given the new version yet stay where they are.
    A coordinator or node already switching finishes its own step."""
    r = await active_rollout(session, lock=True)
    if r is not None:
        _finish(r, "cancelled", f"cancelled by {actor}")
        session.add(AuditLog(actor=actor, action="update.cancel", target=r.version, details={}))
    return r


# --- the rollout ---------------------------------------------------------------


def _online(n: Node, offline_after_s: float, now: datetime) -> bool:
    return (
        n.last_heartbeat_at is not None
        and (now - n.last_heartbeat_at).total_seconds() < offline_after_s
    )


def _failed(n: Node, version: str) -> str | None:
    a = n.agent_status or {}
    if a.get("update_state") == "failed" and a.get("update_target") == version:
        return str(a.get("update_error") or "update failed")
    return None


def _finish(r: UpdateRollout, status: str, error: str | None = None) -> None:
    now = datetime.now(UTC)
    r.status, r.error, r.finished_at, r.updated_at = status, error, now, now
    log.info("update to %s: %s%s", r.version, status, f" ({error})" if error else "")


async def _managed_nodes(session: AsyncSession) -> list[Node]:
    """Nodes whose agent follows release targets (a node in simple mode has none)."""
    nodes = (
        await session.execute(
            select(Node).where(Node.revoked_at.is_(None)).order_by(Node.name, Node.id)
        )
    ).scalars()
    return [n for n in nodes if n.agent_status is not None]


async def advance(
    session: AsyncSession,
    agent_dir: Path | None,
    offline_after_s: float,
    own_version: str = __version__,
) -> str | None:
    """Move the active rollout one step on, if it can. Returns its status."""
    r = await active_rollout(session, lock=True)
    if r is None:
        return None
    now = datetime.now(UTC)
    nodes = await _managed_nodes(session)

    def pending(n: Node) -> bool:
        return n.version != r.version

    async def to_nodes() -> None:
        for n in nodes:
            if pending(n) and n.desired_target_version != r.version:
                n.desired_target_version = r.version
        r.status = "nodes"

    async def to_canary() -> None:
        candidates = [n for n in nodes if pending(n) and _online(n, offline_after_s, now)]
        if not candidates:
            await to_nodes()  # nothing online to try it on: the rest get it directly
            return
        canary = candidates[0]
        canary.desired_target_version = r.version
        r.canary_node_id, r.status = canary.id, "canary"

    if r.status == "coordinator":
        st = exchange.read_json(agent_dir / exchange.STATUS) if agent_dir else None
        # on: this process is the new version, and its agent has recorded the switch
        # (not just started it: the switch could still be rolled back)
        switched = not isinstance(st, dict) or st.get("running_version") in (None, r.version)
        if own_version == r.version and switched:
            await to_canary()
        else:
            if (
                isinstance(st, dict)
                and st.get("update_state") == "failed"
                and st.get("update_target") == r.version
            ):
                _finish(r, "failed", f"coordinator: {st.get('update_error') or 'update failed'}")
                return r.status
    if r.status == "canary":
        canary = next((n for n in nodes if n.id == r.canary_node_id), None)
        if canary is None:
            await to_canary()  # revoked or gone meanwhile: another one
        elif not pending(canary):
            await to_nodes()
        elif error := _failed(canary, r.version):
            _finish(r, "failed", f"{canary.name}: {error}")
            return r.status
    if r.status == "nodes":
        waiting = [
            n
            for n in nodes
            if pending(n) and _online(n, offline_after_s, now) and not _failed(n, r.version)
        ]
        if not waiting:
            failed = [
                f"{n.name}: {e}" for n in nodes if pending(n) and (e := _failed(n, r.version))
            ]
            if failed:
                _finish(r, "failed", "; ".join(failed)[:2000])
            else:
                _finish(r, "done")
            return r.status
    r.updated_at = now
    return r.status


# --- what the panel shows ------------------------------------------------------


def _rollout_view(r: UpdateRollout) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "version": r.version,
        "from_version": r.from_version,
        "status": r.status,
        "canary_node_id": str(r.canary_node_id) if r.canary_node_id else None,
        "error": r.error,
        "started_by": r.started_by,
        "started_at": r.started_at,
        "updated_at": r.updated_at,
        "finished_at": r.finished_at,
    }


async def overview(
    session: AsyncSession,
    *,
    agent_dir: Path | None,
    offline_after_s: float,
    last_check: CheckResult | None,
    repository: str,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    agent = exchange.read_json(agent_dir / exchange.STATUS) if agent_dir else None
    latest = last_check.latest if last_check else None
    rollouts = (
        (
            await session.execute(
                select(UpdateRollout).order_by(UpdateRollout.started_at.desc()).limit(6)
            )
        )
        .scalars()
        .all()
    )
    nodes = (
        (
            await session.execute(
                select(Node).where(Node.revoked_at.is_(None)).order_by(Node.name, Node.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "current": __version__,
        "repository": repository,
        "self_update": agent_dir is not None,
        "check": last_check.view() if last_check else None,
        "update_available": bool(latest and version_key(latest.version) > version_key(__version__)),
        "coordinator_agent": (
            {
                k: agent.get(k)
                for k in (
                    "running_version",
                    "agent_version",
                    "update_state",
                    "update_target",
                    "update_error",
                    "updated_at",
                )
            }  # fmt: skip
            if isinstance(agent, dict)
            else None
        ),
        "rollouts": [_rollout_view(r) for r in rollouts],
        "nodes": [
            {
                "id": str(n.id),
                "name": n.name,
                "online": _online(n, offline_after_s, now),
                "managed": n.agent_status is not None,
                "version": n.version,
                "target": n.desired_target_version,
                "update_state": (n.agent_status or {}).get("update_state"),
                "update_target": (n.agent_status or {}).get("update_target"),
                "update_error": (n.agent_status or {}).get("update_error"),
                "agent_version": (n.agent_status or {}).get("agent_version"),
            }
            for n in nodes
        ],
    }
