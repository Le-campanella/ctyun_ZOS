from __future__ import annotations

import json
import secrets
from hashlib import sha256
from hmac import compare_digest

from cryptography.fernet import Fernet, InvalidToken


def hash_delete_token(token: str) -> bytes:
    return sha256(token.encode("ascii")).digest()


def issue_delete_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)
    return token, hash_delete_token(token)


def matches_delete_token(token: str | None, expected_hash: bytes | None) -> bool:
    if (
        token is None
        or expected_hash is None
        or not 1 <= len(token) <= 256
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        return False
    return compare_digest(hash_delete_token(token), expected_hash)


class CredentialCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("SETTINGS_ENCRYPTION_KEY must be a Fernet key") from exc

    def encrypt(self, credentials: dict[str, str]) -> bytes:
        payload = json.dumps(
            credentials, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return self._fernet.encrypt(payload)

    def decrypt(self, ciphertext: bytes) -> dict[str, str]:
        try:
            value = json.loads(self._fernet.decrypt(ciphertext))
        except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("stored credentials cannot be decrypted") from exc
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("stored credentials are invalid")
        return value
