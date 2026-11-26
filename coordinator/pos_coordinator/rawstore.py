"""Content-addressed raw object store on the filesystem (the NAS mount in production).

Objects are written once under `<root>/<kind>/ab/cd/<sha256>` via a temp file
and atomic rename. Postgres keeps only the hash (`raw_object`, `raw_fetch`).
"""

import hashlib
import os
import re
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("snippets", "html", "dataset")


class HashMismatch(ValueError):
    pass


class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, kind: str, sha256: str) -> Path:
        if kind not in KINDS or not SHA256_RE.match(sha256):
            raise ValueError("bad kind or hash")
        return self.root / kind / sha256[:2] / sha256[2:4] / sha256

    def exists(self, kind: str, sha256: str) -> bool:
        return self.path_for(kind, sha256).exists()

    def put(self, kind: str, sha256: str, data: bytes) -> bool:
        """Store bytes whose SHA-256 must equal `sha256`. Returns False if already present."""
        if hashlib.sha256(data).hexdigest() != sha256:
            raise HashMismatch("content does not match its hash")
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
        return self.path_for(kind, sha256).read_bytes()
