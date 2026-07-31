from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from threading import Event
from typing import BinaryIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import utc_now
from app.main import create_app
from app.providers import (
    ObjectMetadata,
    ProviderError,
    ProviderRegistry,
    StorageProvider,
)


class FakeProvider(StorageProvider):
    provider_id = "fake"
    schema_version = 1
    instances: list["FakeProvider"] = []
    objects: dict[str, bytes] = {}
    upload_started = Event()
    upload_release = Event()

    def __init__(self, config, credentials, _settings):
        self.config, self.credentials = self.validate(config, credentials)
        self.fail_upload = self.config.get("fail_upload", False)
        self.__class__.instances.append(self)

    @classmethod
    def settings_schema(cls):
        return {
            "id": cls.provider_id,
            "display_name": "Fake",
            "schema_version": 1,
            "config_fields": [],
            "credential_fields": [],
        }

    @classmethod
    def validate(cls, config, credentials):
        if not isinstance(config, dict) or not config.get("endpoint_url"):
            raise ProviderError("STORAGE_CONFIG_INVALID", "bad config")
        if not all(credentials.get(key) for key in ("access_key", "secret_key")):
            raise ProviderError("STORAGE_CREDENTIALS_REQUIRED", "missing credentials")
        return dict(config), dict(credentials)

    def test_connection(self):
        if self.config.get("fail_test"):
            raise ProviderError("STORAGE_ENDPOINT_UNREACHABLE", "unreachable")
        return 3

    def upload_file(
        self, fileobj: BinaryIO, object_key: str, content_type: str
    ) -> None:
        if self.config.get("block_upload"):
            self.__class__.upload_started.set()
            self.__class__.upload_release.wait(3)
        if self.config.get("uncertain_upload"):
            raise ProviderError("STORAGE_TIMEOUT", "timeout", uncertain=True)
        if self.fail_upload:
            raise ProviderError("UPLOAD_FAILED", "failed")
        self.__class__.objects[object_key] = fileobj.read()

    def head_object(
        self, object_key: str, version_id: str | None = None
    ) -> ObjectMetadata | None:
        if self.config.get("head_timeout"):
            raise ProviderError("STORAGE_TIMEOUT", "timeout", uncertain=True)
        if self.config.get("head_missing"):
            return None
        value = self.__class__.objects.get(object_key)
        if value is None:
            return None
        return ObjectMetadata(
            size_bytes=len(value) + self.config.get("head_size_delta", 0),
            etag='"fake-etag"',
            version_id=version_id or self.config.get("version_id"),
            content_type="application/octet-stream",
            last_modified="2026-07-31T00:00:00Z",
        )

    def build_public_url(self, object_key: str) -> str:
        return f"{self.config['public_base_url'].rstrip('/')}/{object_key}"

    def get_metrics(self, _from_time, _to_time):
        return {"enabled": False, "status": "disabled"}


@pytest.fixture
def registry():
    FakeProvider.instances.clear()
    FakeProvider.objects.clear()
    FakeProvider.upload_started.clear()
    FakeProvider.upload_release.clear()
    registry = ProviderRegistry()
    registry.register(FakeProvider)
    return registry


@pytest.fixture
def client(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def storage_payload(
    revision: int = 0,
    *,
    credentials: bool = True,
    fail_upload: bool = False,
    fail_test: bool = False,
    **config,
):
    payload = {
        "provider": "fake",
        "provider_schema_version": 1,
        "expected_revision": revision,
        "config": {
            "endpoint_url": "https://storage.internal",
            "public_base_url": "https://files.example",
            "fail_upload": fail_upload,
            "fail_test": fail_test,
            **config,
        },
    }
    if credentials:
        payload["credentials"] = {"access_key": "test-ak", "secret_key": "test-sk"}
    return payload


def activate(client: TestClient, **kwargs):
    response = client.put(
        "/v1/settings/storage",
        headers={"X-Settings-Request": "true"},
        json=storage_payload(**kwargs),
    )
    assert response.status_code == 200, response.text
    return response


def test_unconfigured_service_still_serves_health_settings_and_dashboard(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["error"]["code"] == "STORAGE_NOT_CONFIGURED"
    assert client.get("/v1/settings/storage").json()["configured"] is False
    assert client.get("/dashboard").status_code == 200
    upload = client.post("/v1/uploads", files={"file": ("a.txt", b"a")})
    assert upload.status_code == 503
    assert upload.json()["error"]["code"] == "STORAGE_NOT_CONFIGURED"


def test_openapi_and_upload_response_freeze_current_v1_contract(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "UploadResponseV1" in schemas
    upload_schema = schemas["UploadResponseV1"]
    assert set(upload_schema["required"]) == {"task_id", "key", "url"}
    assert upload_schema["additionalProperties"] is False

    activate(client)
    response = client.post(
        "/v1/uploads",
        files={"file": ("contract.txt", b"contract", "text/plain")},
    )
    assert response.status_code == 201
    assert set(response.json()) == {"task_id", "key", "url"}
    serialized = response.text.lower()
    for secret_name in (
        "access_key",
        "secret_key",
        "settings_encryption_key",
        "delete_token",
        "token_hash",
    ):
        assert secret_name not in serialized


def test_dashboard_is_local_static_and_never_embeds_credentials(client):
    activate(client)
    dashboard = client.get("/dashboard")
    settings = client.get("/dashboard/settings")
    dashboard_js = client.get("/static/dashboard.js")
    settings_js = client.get("/static/settings.js")
    chart = client.get("/static/chart.umd.min.js")

    assert dashboard.status_code == settings.status_code == 200
    assert dashboard_js.status_code == settings_js.status_code == chart.status_code == 200
    assert 'src="/static/chart.umd.min.js"' in dashboard.text
    assert "https://" not in dashboard.text
    assert 'type="password"' in settings.text
    assert "test-ak" not in settings.text and "test-sk" not in settings.text
    assert "innerHTML" not in dashboard_js.text
    assert "innerHTML" not in settings_js.text
    assert "localStorage" not in settings_js.text
    assert 'id="receive-test-file"' in dashboard.text
    assert 'id="receive-test-real-upload" type="checkbox" role="switch"' in dashboard.text
    assert "/v1/uploads/validate" in dashboard_js.text
    assert 'real ? "/v1/uploads" : "/v1/uploads/validate"' in dashboard_js.text


def test_receive_validation_works_unconfigured_without_task_or_storage(client):
    response = client.post(
        "/v1/uploads/validate",
        headers={"X-Request-ID": "lan-test-1"},
        files={"file": ("../测试.pdf", b"lan-payload", "application/pdf")},
    )
    assert response.status_code == 200
    assert response.json() == {
        "received": True,
        "uploaded_to_storage": False,
        "recorded_as_task": False,
        "filename": "测试.pdf",
        "content_type": "application/pdf",
        "size_bytes": 11,
        "request_id": "lan-test-1",
    }
    assert client.get("/v1/upload-tasks").json()["items"] == []
    assert FakeProvider.objects == {}

    empty = client.post(
        "/v1/uploads/validate", files={"file": ("empty.txt", b"")}
    )
    oversized = client.post(
        "/v1/uploads/validate", files={"file": ("large.bin", b"x" * 101)}
    )
    assert empty.json()["error"]["code"] == "FILE_EMPTY"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_settings_activation_masks_credentials_and_preserves_them(client, database):
    assert client.get("/v1/settings/storage/providers").json()["items"][0]["id"] == "fake"
    result = activate(client).json()
    assert result["revision"] == 1
    assert result["credentials"]["access_key_masked"] == "****t-ak"
    serialized = client.get("/v1/settings/storage").text
    assert "test-ak" not in serialized
    assert "test-sk" not in serialized

    updated = client.put(
        "/v1/settings/storage",
        headers={"X-Settings-Request": "true"},
        json=storage_payload(revision=1, credentials=False),
    )
    assert updated.status_code == 200
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT credentials_ciphertext FROM storage_configs WHERE status='active'"
        ).fetchone()[0]
    assert b"test-ak" not in stored and b"test-sk" not in stored


def test_settings_require_header_and_revision_and_keep_old_on_probe_failure(client):
    no_header = client.put("/v1/settings/storage", json=storage_payload())
    assert no_header.status_code == 400
    activate(client)
    conflict = client.put(
        "/v1/settings/storage",
        headers={"X-Settings-Request": "true"},
        json=storage_payload(revision=0),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFIG_REVISION_CONFLICT"

    failed = client.put(
        "/v1/settings/storage",
        headers={"X-Settings-Request": "true"},
        json=storage_payload(revision=1, credentials=False, fail_test=True),
    )
    assert failed.status_code == 502
    assert client.get("/v1/settings/storage").json()["revision"] == 1


def test_upload_success_task_list_detail_stats_and_logs(client):
    activate(client, version_id="version-1")
    response = client.post(
        "/v1/uploads",
        headers={"X-Request-ID": "request-1", "Idempotency-Key": "job-1"},
        files={"file": ("../../Report.PDF", b"hello", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["key"].endswith(f"/{result['task_id']}.pdf")
    assert result["url"].endswith(result["key"])
    assert FakeProvider.objects[result["key"]] == b"hello"

    tasks = client.get("/v1/upload-tasks?status=succeeded").json()["items"]
    assert tasks[0]["filename"] == "Report.PDF"
    assert tasks[0]["size_bytes"] == 5
    assert tasks[0]["etag"] == '"fake-etag"'
    assert tasks[0]["version_id"] == "version-1"
    assert tasks[0]["object_status"] == "present"
    assert tasks[0]["request_id"] == "request-1"
    detail = client.get(f"/v1/upload-tasks/{result['task_id']}").json()
    assert detail["idempotency_key"] == "job-1"
    summary = client.get("/v1/dashboard/summary").json()["uploads"]
    assert summary["success_count"] == 1
    assert summary["successful_upload_bytes"] == 5
    traffic = client.get("/v1/dashboard/traffic?interval=hour").json()
    assert sum(point["success_count"] for point in traffic["points"]) == 1
    logs = client.get("/v1/dashboard/logs?event=upload_succeeded").json()["items"]
    assert logs[0]["task_id"] == result["task_id"]


def test_idempotent_success_replay_does_not_upload_twice(client):
    activate(client)
    headers = {"Idempotency-Key": "same-job"}
    first = client.post(
        "/v1/uploads", headers=headers, files={"file": ("a.txt", b"one")}
    )
    second = client.post(
        "/v1/uploads", headers=headers, files={"file": ("different.txt", b"two")}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json() == first.json()
    assert len(FakeProvider.objects) == 1


def test_empty_oversized_multiple_files_and_failed_idempotency(client):
    activate(client)
    empty = client.post(
        "/v1/uploads",
        headers={"Idempotency-Key": "empty"},
        files={"file": ("empty.txt", b"")},
    )
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "FILE_EMPTY"
    empty_task = client.get(f"/v1/upload-tasks/{empty.json()['task_id']}").json()
    assert empty_task["status"] == "failed"

    oversized = client.post(
        "/v1/uploads", files={"file": ("large.bin", b"x" * 101)}
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "FILE_TOO_LARGE"

    duplicate = client.post(
        "/v1/uploads",
        files=[("file", ("a.txt", b"a")), ("file", ("b.txt", b"b"))],
    )
    assert duplicate.status_code == 400

    retry = client.post(
        "/v1/uploads",
        headers={"Idempotency-Key": "empty"},
        files={"file": ("empty.txt", b"now-valid")},
    )
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_upload_provider_failure_is_persisted(client):
    activate(client, fail_upload=True)
    response = client.post(
        "/v1/uploads", files={"file": ("a.bin", b"payload")}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPLOAD_FAILED"
    task = client.get(f"/v1/upload-tasks/{response.json()['task_id']}").json()
    assert task["status"] == "failed"
    assert task["public_url"] is None


def test_uncertain_upload_is_recoverable_and_hides_url(client):
    activate(client, uncertain_upload=True)
    response = client.post("/v1/uploads", files={"file": ("a.bin", b"payload")})
    assert response.status_code == 502
    task = client.get(f"/v1/upload-tasks/{response.json()['task_id']}").json()
    assert task["status"] == "unknown"
    assert task["error_code"] == "STORAGE_TIMEOUT"
    assert task["public_url"] is None


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"head_missing": True}, "UPLOAD_CONFIRMATION_PENDING"),
        ({"head_timeout": True}, "STORAGE_TIMEOUT"),
        ({"head_size_delta": 1}, "OBJECT_SIZE_MISMATCH"),
    ],
)
def test_upload_requires_matching_remote_metadata(client, config, code):
    activate(client, **config)
    response = client.post("/v1/uploads", files={"file": ("a.bin", b"payload")})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == code
    task = client.get(f"/v1/upload-tasks/{response.json()['task_id']}").json()
    assert task["status"] == "unknown"
    assert task["object_status"] == "pending"
    assert task["public_url"] is None


def test_capacity_rejects_second_upload_but_queries_stay_available(client):
    activate(client, block_upload=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            client.post, "/v1/uploads", files={"file": ("first.bin", b"one")}
        )
        assert FakeProvider.upload_started.wait(2)
        second = client.post("/v1/uploads", files={"file": ("second.bin", b"two")})
        health = client.get("/healthz")
        FakeProvider.upload_release.set()
        assert first.result(timeout=3).status_code == 201
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "UPLOAD_CAPACITY_EXCEEDED"
    assert second.headers["Retry-After"] == "5"
    assert health.status_code == 200


def test_filename_content_type_and_multipart_edges(client):
    activate(client)
    response = client.post(
        "/v1/uploads",
        files={"file": ("../<script>alert(1)</script>.TOO_LONG_EXT", b"x", "")},
    )
    assert response.status_code == 201
    assert response.json()["key"].count(".") == 0
    task = client.get(f"/v1/upload-tasks/{response.json()['task_id']}").json()
    assert task["filename"] == "script>.TOO_LONG_EXT"
    assert task["content_type"] == "application/octet-stream"
    assert "<script>" not in client.get("/dashboard").text

    missing = client.post("/v1/uploads", files={"other": ("a", b"x")})
    malformed = client.post(
        "/v1/uploads",
        content=b"not multipart",
        headers={"Content-Type": "multipart/form-data; boundary=broken"},
    )
    assert missing.json()["error"]["code"] == "FILE_REQUIRED"
    assert malformed.status_code == 400


def test_settings_reject_cross_origin_and_endpoint_change_without_new_keys(client):
    activate(client)
    cross_origin = client.post(
        "/v1/settings/storage/test",
        headers={
            "X-Settings-Request": "true",
            "Origin": "https://attacker.example",
        },
        json=storage_payload(revision=1, credentials=False),
    )
    assert cross_origin.status_code == 400

    changed = storage_payload(revision=1, credentials=False)
    changed["config"]["endpoint_url"] = "https://other.internal"
    response = client.put(
        "/v1/settings/storage",
        headers={"X-Settings-Request": "true"},
        json=changed,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "STORAGE_CREDENTIALS_REQUIRED"
    assert client.get("/v1/settings/storage").json()["revision"] == 1


def test_bad_headers_queries_and_request_body_limit(client):
    bad_request_id = client.get("/healthz", headers={"X-Request-ID": "x" * 129})
    assert bad_request_id.status_code == 400
    assert bad_request_id.headers["X-Request-ID"] != "x" * 129

    assert client.get("/v1/upload-tasks/not-a-uuid").status_code == 400
    assert client.get("/v1/upload-tasks?limit=0").status_code == 400
    too_large = client.post(
        "/v1/uploads",
        headers={"Content-Length": "999999"},
        content=b"",
    )
    assert too_large.status_code == 413

    activate(client)
    no_content_length = client.post(
        "/v1/uploads",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": "10",
        },
        content=(
            b"--x\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"a.bin\"\r\n\r\n" + b"x" * 2_100 + b"\r\n--x--\r\n"
        ),
    )
    assert no_content_length.status_code == 413


def test_ready_rejects_stale_storage_probe(client):
    activate(client)
    client.app.state.runtime.last_probe["last_checked_at"] = (
        datetime.now(UTC) - timedelta(minutes=5)
    ).isoformat()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"]["status"] == "degraded"
    assert response.json()["checks"]["storage"]["error_code"] == "STORAGE_PROBE_STALE"


def test_recovery_resolves_existing_and_missing_objects(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as first:
        activate(first)
        runtime = first.app.state.runtime
        storage = runtime.database.active_storage()
        for exists in (True, False):
            task_id = str(uuid4())
            key = f"2026/07/29/{task_id}.txt"
            runtime.database.create_task(
                {
                    "id": task_id,
                    "request_id": task_id,
                    "idempotency_key": None,
                    "storage_config_id": storage["id"],
                    "filename": "a.txt",
                    "content_type": "text/plain",
                    "object_key": key,
                    "public_url": f"https://files.example/{key}",
                    "status": "unknown",
                    "size_bytes": None,
                    "error_code": "RECOVERY_PENDING",
                    "created_at": utc_now(),
                    "finished_at": None,
                    "duration_ms": None,
                }
            )
            if exists:
                FakeProvider.objects[key] = b"restored"

    with TestClient(app, raise_server_exceptions=False) as restarted:
        items = restarted.get("/v1/upload-tasks").json()["items"]
        states = {item["object_key"]: item["status"] for item in items}
        assert any(value == "succeeded" for value in states.values())
        assert any(value == "failed" for value in states.values())
        object_states = {item["object_key"]: item["object_status"] for item in items}
        assert "present" in object_states.values()
        assert "absent" in object_states.values()


def test_recovery_keeps_size_mismatch_unknown(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        activate(client)
        runtime = client.app.state.runtime
        storage = runtime.database.active_storage()
        task_id = str(uuid4())
        key = f"2026/07/29/{task_id}.txt"
        runtime.database.create_task(
            {
                "id": task_id,
                "request_id": task_id,
                "idempotency_key": None,
                "storage_config_id": storage["id"],
                "filename": "a.txt",
                "content_type": "text/plain",
                "object_key": key,
                "public_url": f"https://files.example/{key}",
                "status": "unknown",
                "size_bytes": 3,
                "error_code": "RECOVERY_PENDING",
                "created_at": utc_now(),
                "finished_at": None,
                "duration_ms": None,
            }
        )
        FakeProvider.objects[key] = b"too-long"

    with TestClient(app, raise_server_exceptions=False) as restarted:
        task = restarted.get(f"/v1/upload-tasks/{task_id}").json()
        assert task["status"] == "unknown"
        assert task["object_status"] == "pending"
        assert task["error_code"] == "OBJECT_SIZE_MISMATCH"
