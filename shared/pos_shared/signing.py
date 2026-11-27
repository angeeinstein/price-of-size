"""Ed25519 signatures over canonical JSON.

The coordinator signs release manifests (and later parser artifacts). Nodes pin
the coordinator's public key at enrollment and refuse anything unsigned or
signed by another key. Canonical form: UTF-8 JSON, sorted keys, no whitespace.
"""

import base64
import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64u_encode(raw)


def key_id(public_key_b64_: str) -> str:
    """Short, stable fingerprint of a public key (first 16 hex chars of its SHA-256)."""
    return hashlib.sha256(b64u_decode(public_key_b64_)).hexdigest()[:16]


def sign(key: Ed25519PrivateKey, obj: Any) -> str:
    return b64u_encode(key.sign(canonical_json(obj)))


def verify(public_key_b64_: str, obj: Any, signature_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64u_decode(public_key_b64_))
        pub.verify(b64u_decode(signature_b64), canonical_json(obj))
    except (InvalidSignature, ValueError):
        return False
    return True
