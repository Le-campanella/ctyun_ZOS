from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.database import Database, RevisionConflict, SCHEMA_VERSION, utc_now
from app.eventlog import EventLogger, NOTIFY
from app.providers import CtyunZosProvider, ObjectMetadata, ProviderError
from app.security import CredentialCipher, hash_delete_token, issue_delete_token


def storage_record(ciphertext: bytes, *, item_id: str | None = None) -> dict:
    now = utc_now()
    return {
        "id": item_id or str(uuid4()),
        "provider": "ctyun_zos",
        "provider_schema_version": 1,
        "config_json": json.dumps({"bucket": "bucket-1"}),
        "credentials_ciphertext": ciphertext,
        "created_at": now,
        "activated_at": now,
        "last_tested_at": now,
        "last_test_latency_ms": 12,
    }


def task_record(config_id: str, *, created_at: str, status: str = "uploading") -> dict:
    task_id = str(uuid4())
    return {
        "id": task_id,
        "request_id": str(uuid4()),
        "idempotency_key": None,
        "storage_config_id": config_id,
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "object_key": f"2026/07/29/{task_id}.pdf",
        "public_url": None,
        "status": status,
        "size_bytes": None,
        "error_code": None,
        "created_at": created_at,
        "finished_at": None,
        "duration_ms": None,
    }


def test_cipher_round_trip_and_wrong_key(settings):
    cipher = CredentialCipher(settings.encryption_key)
    encrypted = cipher.encrypt({"access_key": "ak", "secret_key": "sk"})

    assert b"ak" not in encrypted
    assert cipher.decrypt(encrypted) == {"access_key": "ak", "secret_key": "sk"}

    other = CredentialCipher(Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="cannot be decrypted"):
        other.decrypt(encrypted)


def test_delete_tokens_have_256_bits_and_only_hashes_need_persisting():
    first, first_hash = issue_delete_token()
    second, second_hash = issue_delete_token()

    assert first != second
    assert len(first_hash) == len(second_hash) == 32
    assert first_hash == hash_delete_token(first)
    assert second_hash == hash_delete_token(second)
    assert first.encode() not in first_hash


def test_database_schema_activation_and_revision_conflict(database: Database, settings):
    cipher = CredentialCipher(settings.encryption_key)
    first = database.activate_storage(
        storage_record(cipher.encrypt({"access_key": "ak", "secret_key": "sk"})), 0
    )
    second = database.activate_storage(
        storage_record(cipher.encrypt({"access_key": "ak2", "secret_key": "sk2"})), 1
    )

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert database.active_storage()["id"] == second["id"]
    assert database.storage_by_id(first["id"])["status"] == "inactive"
    with pytest.raises(RevisionConflict) as conflict:
        database.activate_storage(storage_record(b"x"), 1)
    assert conflict.value.current_revision == 2

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000


def test_task_queries_are_stable_and_filterable(database: Database, settings):
    config = database.activate_storage(storage_record(b"ciphertext"), 0)
    older = task_record(config["id"], created_at="2026-07-29T01:00:00Z")
    newer = task_record(
        config["id"], created_at="2026-07-29T02:00:00Z", status="failed"
    )
    database.create_task(older)
    database.create_task(newer)
    database.update_task(
        newer["id"],
        error_code="UPLOAD_FAILED",
        finished_at="2026-07-29T02:00:01Z",
        duration_ms=1_000,
    )

    items = database.list_tasks(limit=10, offset=0)
    assert [item["id"] for item in items] == [newer["id"], older["id"]]
    assert database.list_tasks(limit=10, offset=0, status="failed")[0]["id"] == newer["id"]
    assert database.task_by_id(newer["id"])["storage_config_revision"] == 1


def test_idempotency_index_is_unique(database: Database):
    config = database.activate_storage(storage_record(b"ciphertext"), 0)
    first = task_record(config["id"], created_at=utc_now())
    second = task_record(config["id"], created_at=utc_now())
    first["idempotency_key"] = second["idempotency_key"] = "same"
    database.create_task(first)

    with pytest.raises(sqlite3.IntegrityError):
        database.create_task(second)


def test_retention_never_removes_a_task_that_may_still_have_an_object(
    database: Database,
):
    config = database.activate_storage(storage_record(b"ciphertext"), 0)
    present = task_record(
        config["id"], created_at="2020-01-01T00:00:00Z", status="succeeded"
    )
    absent = task_record(
        config["id"], created_at="2020-01-01T00:00:00Z", status="failed"
    )
    database.create_task(present)
    database.create_task(absent)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE upload_tasks SET object_status='present' WHERE id=?",
            (present["id"],),
        )
        connection.execute(
            "UPDATE upload_tasks SET object_status='absent' WHERE id=?",
            (absent["id"],),
        )

    database.maintain(task_retention_days=1, log_retention_days=1, log_max_rows=10)

    assert database.task_by_id(present["id"]) is not None
    assert database.task_by_id(absent["id"]) is None


def test_event_logger_redacts_secrets_and_persists_notify(database: Database, capsys):
    logger = EventLogger(database)
    logger.emit(
        logging.INFO,
        "debug_event",
        "not persisted",
        details={"access_key": "visible-no", "safe": "yes"},
    )
    logger.emit(
        NOTIFY,
        "config_event",
        "persisted",
        details={
            "secret_key": "visible-no",
            "delete_token": "visible-no",
            "token_hash": "visible-no",
            "safe": "yes",
        },
    )

    rows = database.list_logs(min_level=NOTIFY, limit=10)
    assert len(rows) == 1
    assert rows[0]["details"] == {
        "secret_key": "[REDACTED]",
        "delete_token": "[REDACTED]",
        "token_hash": "[REDACTED]",
        "safe": "yes",
    }
    assert "visible-no" not in capsys.readouterr().out


def test_summary_percentile_log_pagination_and_retention(database: Database):
    config = database.activate_storage(storage_record(b"ciphertext"), 0)
    now = datetime.now(UTC)
    for index, duration in enumerate((100, 200, 300, 400, 500)):
        task = task_record(
            config["id"],
            created_at=(now - timedelta(minutes=index)).isoformat(),
            status="failed" if index == 4 else "succeeded",
        )
        task["size_bytes"] = 10
        task["duration_ms"] = duration
        task["finished_at"] = task["created_at"]
        database.create_task(task)
    summary = database.summary(
        (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat()
    )
    assert summary == {
        "attempt_count": 5,
        "success_count": 4,
        "failure_count": 1,
        "uploading_count": 0,
        "unknown_count": 0,
        "success_rate": 0.8,
        "successful_upload_bytes": 40,
        "average_duration_ms": 300,
        "p95_duration_ms": 500,
    }

    for index in range(5):
        database.write_log(
            {
                "created_at": (now + timedelta(seconds=index)).isoformat(),
                "level_no": NOTIFY,
                "level_name": "NOTIFY",
                "event": "test",
                "message": str(index),
                "request_id": "keep" if index % 2 else "other",
            }
        )
    first = database.list_logs(min_level=NOTIFY, limit=2)
    second = database.list_logs(
        min_level=NOTIFY, limit=2, before_id=first[-1]["id"]
    )
    assert not {item["id"] for item in first} & {item["id"] for item in second}
    assert len(
        database.list_logs(
            min_level=NOTIFY, limit=10, filters={"request_id": "keep"}
        )
    ) == 2
    with pytest.raises(ValueError, match="invalid log filter"):
        database.list_logs(
            min_level=NOTIFY, limit=10, filters={"unsafe_column": "value"}
        )
    database.maintain(task_retention_days=180, log_retention_days=30, log_max_rows=3)
    assert len(database.list_logs(min_level=NOTIFY, limit=10)) == 3


class FakeS3:
    def __init__(self):
        self.uploaded = None
        self.objects = {}
        self.last_head_request = None

    def head_bucket(self, **_kwargs):
        return {}

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs, Config):
        self.uploaded = (bucket, key, ExtraArgs, fileobj.read(), Config)
        self.objects[key] = self.uploaded[3]

    def head_object(self, **request):
        self.last_head_request = request
        Key = request["Key"]
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {
            "ContentLength": len(self.objects[Key]),
            "ETag": '"fake-etag"',
            "VersionId": "version-1",
            "ContentType": "application/pdf",
            "LastModified": datetime(2026, 7, 31, tzinfo=UTC),
        }


def valid_config() -> dict:
    return {
        "endpoint_url": "https://jiangsu-10.zos.ctyun.cn/",
        "bucket": "bucket-1",
        "public_base_url": "https://bucket-1.example.com/base/",
        "connect_timeout_seconds": 5,
        "read_timeout_seconds": 300,
        "max_attempts": 2,
        "verify_tls": True,
        "enable_bucket_metrics": False,
    }


def test_zos_provider_validation_upload_url_and_head(settings):
    fake = FakeS3()
    provider = CtyunZosProvider(
        valid_config(),
        {"access_key": "ak", "secret_key": "sk"},
        settings,
        client=fake,
    )
    payload = BytesIO(b"hello")
    provider.upload_file(payload, "2026/07/29/a b.pdf", "application/pdf")

    assert fake.uploaded[:3] == (
        "bucket-1",
        "2026/07/29/a b.pdf",
        {"ContentType": "application/pdf", "ACL": "public-read"},
    )
    assert (
        provider.build_public_url("2026/07/29/a b.pdf")
        == "https://bucket-1.example.com/base/2026/07/29/a%20b.pdf"
    )
    assert provider.head_object("2026/07/29/a b.pdf", "version-1") == ObjectMetadata(
        size_bytes=5,
        etag='"fake-etag"',
        version_id="version-1",
        content_type="application/pdf",
        last_modified="2026-07-31T00:00:00+00:00",
    )
    assert fake.last_head_request["VersionId"] == "version-1"
    assert provider.head_object("missing") is None


def test_zos_client_uses_compatible_checksum_policy(settings, monkeypatch):
    captured = {}

    def create_client(*_args, **kwargs):
        captured.update(kwargs)
        return FakeS3()

    monkeypatch.setattr("app.providers.boto3.client", create_client)
    CtyunZosProvider(
        valid_config(),
        {"access_key": "ak", "secret_key": "sk"},
        settings,
    )

    config = captured["config"]
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"bucket": "INVALID"}, "STORAGE_CONFIG_INVALID"),
        ({"endpoint_url": "file:///tmp/object"}, "STORAGE_CONFIG_INVALID"),
        ({"public_base_url": "https://user:pass@example.com"}, "STORAGE_CONFIG_INVALID"),
        ({"read_timeout_seconds": 0}, "STORAGE_CONFIG_INVALID"),
    ],
)
def test_zos_provider_rejects_invalid_config(settings, change, code):
    config = valid_config() | change
    with pytest.raises(ProviderError) as error:
        CtyunZosProvider(
            config,
            {"access_key": "ak", "secret_key": "sk"},
            settings,
            client=FakeS3(),
        )
    assert error.value.code == code
