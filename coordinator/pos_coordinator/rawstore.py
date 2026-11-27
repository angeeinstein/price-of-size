"""Content-addressed raw object store on the filesystem (the NAS mount in production).

Objects are written once under `<root>/<kind>/ab/cd/<sha256>` via a temp file
and atomic rename. Postgres keeps only the hash (`raw_object`, `raw_fetch`).

The store carries an identity: a marker file whose id the database records on
first use. An unmounted NAS leaves an empty mount point behind, which would
otherwise look like "every object is missing" to readers and silently take new
objects onto the local disk, to vanish under the NAS when it is mounted again.
While the marker does not match, the store is unavailable: writes fail (nodes
retry later) and readers wait instead of skipping.
"""

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("snippets", "html", "dataset")
MARKER = ".pos-rawstore"
SETTING_KEY = "_raw_store_id"  # internal row in the setting table (not a runtime switch)


class HashMismatch(ValueError):
    pass


class StoreUnavailable(RuntimeError):
    """The raw store is not the one this database belongs to (e.g. NAS not mounted)."""


class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.expected_id: str | None = None  # set by bind(); unbound stores (tests) trust root

    def _marker(self) -> str | None:
        try:
            return (self.root / MARKER).read_text().strip() or None
        except OSError:
            return None

    def bind(self, known_id: str | None) -> str:
        """Tie the store to the database's record. With no record yet (first start),
        the store's existing marker is adopted, or a new one written. Returns the id
        the database should hold."""
        current = self._marker()
        if known_id is None and current is None:
            self.root.mkdir(parents=True, exist_ok=True)
            new = str(uuid.uuid4())
            try:
                with open(self.root / MARKER, "x") as f:  # two starting slots: one wins
                    f.write(new + "\n")
            except FileExistsError:
                pass
            current = self._marker() or new
            log.info("raw store %s initialised (id %s)", self.root, current)
        self.expected_id = known_id or current
        if not self.available():
            log.error(
                "raw store %s is not this installation's store (expected id %s, found %s):"
                " is the NAS mounted? Uploads and derivation wait until it is.",
                self.root,
                self.expected_id,
                current,
            )
        return self.expected_id

    def available(self) -> bool:
        return self.expected_id is None or self._marker() == self.expected_id

    def _require(self) -> None:
        if not self.available():
            raise StoreUnavailable(f"raw store {self.root} is not available")

    def path_for(self, kind: str, sha256: str) -> Path:
        if kind not in KINDS or not SHA256_RE.match(sha256):
            raise ValueError("bad kind or hash")
        return self.root / kind / sha256[:2] / sha256[2:4] / sha256

    def exists(self, kind: str, sha256: str) -> bool:
        self._require()
        return self.path_for(kind, sha256).exists()

    def put(self, kind: str, sha256: str, data: bytes) -> bool:
        """Store bytes whose SHA-256 must equal `sha256`. Returns False if already present."""
        if hashlib.sha256(data).hexdigest() != sha256:
            raise HashMismatch("content does not match its hash")
        self._require()
        path = self.path_for(kind, sha256)
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{sha256}.{os.getpid()}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True

    def get(self, kind: str, sha256: str) -> bytes:
        self._require()
        return self.path_for(kind, sha256).read_bytes()


async def bind_to_database(sm, store: RawStore, adopt: bool = False) -> str:
    """Record the raw store's identity on first start; afterwards insist on it.

    `adopt` (operator command after moving the store) makes the database take the
    identity of whatever store is mounted now.
    """
    import asyncio

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from pos_coordinator.models import Setting

    async with sm() as session:
        row = await session.get(Setting, SETTING_KEY)
        known = None if adopt or row is None else row.value
        store_id = await asyncio.to_thread(store.bind, known)
        if known is None:
            stmt = pg_insert(Setting).values(key=SETTING_KEY, value=store_id, updated_by="system")
            if adopt:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"], set_={"value": store_id, "updated_by": "cli"}
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=["key"])
            await session.execute(stmt)
            await session.commit()
        return store_id
