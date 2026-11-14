"""Provider session material, encrypted at rest with SPE_SECRET_KEY (Fernet: AES-128-CBC + HMAC-SHA256)."""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def seal(secret: str, data: dict) -> bytes:
    return _fernet(secret).encrypt(json.dumps(data).encode())


def unseal(secret: str, blob: bytes | None) -> dict | None:
    """None when there is no session or SPE_SECRET_KEY changed (the owner signs in again)."""
    if not blob:
        return None
    try:
        return json.loads(_fernet(secret).decrypt(blob))
    except InvalidToken:
        return None
