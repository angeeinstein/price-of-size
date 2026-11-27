"""Node identity: an Ed25519 key pair generated locally; the private key never leaves the node."""

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_FILE = "node_key.pem"
IDENTITY_FILE = "identity.json"
SIGNING_KEY_FILE = "coordinator_signing_key.pub"


def _b64u(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class Identity:
    private_key: Ed25519PrivateKey
    node_id: uuid.UUID | None
    coordinator_signing_key: str | None = None  # pinned at enrollment

    @property
    def public_key_b64(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return _b64u(raw)

    def sign(self, message: bytes) -> str:
        return _b64u(self.private_key.sign(message))


def load_or_create(state_dir: Path) -> Identity:
    state_dir.mkdir(parents=True, exist_ok=True)
    key_path = state_dir / KEY_FILE
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(key, Ed25519PrivateKey)
    else:
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    node_id = signing_key = None
    id_path = state_dir / IDENTITY_FILE
    if id_path.exists():
        data = json.loads(id_path.read_text())
        node_id = uuid.UUID(data["node_id"])
        signing_key = data.get("coordinator_signing_key")
    return Identity(private_key=key, node_id=node_id, coordinator_signing_key=signing_key)


def save_enrollment(state_dir: Path, node_id: uuid.UUID, coordinator_signing_key: str) -> None:
    path = state_dir / IDENTITY_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"node_id": str(node_id), "coordinator_signing_key": coordinator_signing_key})
    )
    os.replace(tmp, path)
