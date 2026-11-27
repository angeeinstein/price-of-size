"""The coordinator's Ed25519 signing key (release manifests, parser artifacts).

Created on first start with mode 0600. Losing it means every node must be
re-enrolled to pin a new key, so it belongs in backups (see docs/operations.md).
"""

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def load_or_create(path: Path) -> Ed25519PrivateKey:
    path = Path(path)
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 private key")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
        f.flush()
        os.fsync(f.fileno())
    return key
