from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

from app.backup import BackupError, build_backup, create, verify, verify_backup


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.last_put = None

    def put_object(self, **request):
        self.last_put = request
        self.objects[request["Key"]] = {
            "body": request["Body"],
            "metadata": request["Metadata"],
        }

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objects[Key]["body"])}

    def get_object(self, *, Bucket, Key):
        item = self.objects[Key]
        return {
            "Body": io.BytesIO(item["body"]),
            "Metadata": item["metadata"],
        }


def environment(settings):
    return {
        "BACKUP_ZOS_ENDPOINT": "https://backup.example.com",
        "BACKUP_ZOS_BUCKET": "private-backups",
        "BACKUP_ZOS_ACCESS_KEY": "backup-ak",
        "BACKUP_ZOS_SECRET_KEY": "backup-sk",
        "BACKUP_PASSPHRASE": "x" * 48,
        "BACKUP_PREFIX": "zos-service",
        "SETTINGS_ENCRYPTION_KEY": settings.encryption_key,
        "DATABASE_PATH": str(settings.database_path),
    }


def test_private_encrypted_backup_round_trip(settings, database, tmp_path):
    storage = FakeStorage()
    result = create(environment(settings), storage)

    assert result["status"] == "ok"
    assert result["schema_version"] == 4
    assert result["object_key"].startswith("zos-service/")
    assert storage.last_put["ACL"] == "private"
    blob = storage.last_put["Body"]
    assert settings.encryption_key.encode() not in blob
    assert str(settings.database_path).encode() not in blob

    restored = verify(environment(settings), result["object_key"], storage)
    assert restored["integrity"] == "ok"
    assert restored["settings_encryption_key"] == "present"
    assert restored["task_count"] == 0

    restore_directory = tmp_path / "restored"
    restored = verify_backup(
        blob,
        environment(settings)["BACKUP_PASSPHRASE"],
        restore_directory=restore_directory,
    )
    assert restored["restored_to"] == str(restore_directory)
    assert (restore_directory / "zos-upload.db").stat().st_mode & 0o777 == 0o600
    key_file = restore_directory / "settings-encryption-key.env"
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert settings.encryption_key in key_file.read_text()
    with pytest.raises(BackupError, match="拒绝覆盖"):
        verify_backup(
            blob,
            environment(settings)["BACKUP_PASSPHRASE"],
            restore_directory=restore_directory,
        )


def test_backup_rejects_wrong_password_tampering_and_insecure_endpoint(
    settings, database
):
    blob, _ = build_backup(
        settings.database_path,
        settings.encryption_key,
        "correct-passphrase-" * 3,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    with pytest.raises(BackupError, match="密码错误或备份已损坏"):
        verify_backup(blob, "wrong-passphrase-" * 3)
    with pytest.raises(BackupError, match="密码错误或备份已损坏"):
        verify_backup(blob[:-1] + bytes([blob[-1] ^ 1]), "correct-passphrase-" * 3)

    insecure = environment(settings)
    insecure["BACKUP_ZOS_ENDPOINT"] = "http://backup.example.com"
    with pytest.raises(BackupError, match="HTTPS"):
        create(insecure, FakeStorage())
