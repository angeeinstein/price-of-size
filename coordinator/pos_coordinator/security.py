"""Token, code and signature helpers. Secrets are stored only as SHA-256 hashes."""

import base64
import hashlib
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_secret(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def new_enrollment_code() -> str:
    """Human-typeable one-time code, e.g. 'K7QF-2MXD-9RTA-W3PE'."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def normalize_code(code: str) -> str:
    return code.strip().upper().replace(" ", "")


def parse_public_key(b64: str) -> bytes:
    raw = b64u_decode(b64)
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    Ed25519PublicKey.from_public_bytes(raw)  # validates
    return raw


def verify_signature(public_key: bytes, message: bytes, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(b64u_decode(signature_b64), message)
    except (InvalidSignature, ValueError):
        return False
    return True
