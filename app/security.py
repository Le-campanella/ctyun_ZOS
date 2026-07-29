from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken


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
