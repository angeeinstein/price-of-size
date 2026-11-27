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


def open_signed_manifest(raw: Any, public_key: str | None):
    """Verify a signed release manifest exactly as received, then parse it.

    The signature covers the manifest as the coordinator signed it, so it is
    checked over the received JSON, not over a re-serialization of the parsed
    model: a node that does not know a field a newer coordinator added must
    still accept the release (and never accept a modified one).
    Returns (manifest, None) or (None, reason).
    """
    from pos_shared.api import ReleaseManifest

    if not isinstance(raw, dict) or not isinstance(raw.get("manifest"), dict):
        return None, "malformed signed manifest"
    signature = raw.get("signature")
    if public_key is None:
        return None, "no pinned coordinator key"
    if not isinstance(signature, str) or not verify(public_key, raw["manifest"], signature):
        return None, "not signed by the pinned key"
    try:
        return ReleaseManifest.model_validate(raw["manifest"]), None
    except ValueError as e:
        return None, f"malformed manifest: {e}"
