from __future__ import annotations

from io import BytesIO
from typing import Any, BinaryIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import utc_now
from app.main import create_app
from app.providers import (
    ProviderError,
    ProviderRegistry,
    StorageProvider,
)


class FakeProvider(StorageProvider):
    provider_id = "fake"
    schema_version = 1
    instances: list["FakeProvider"] = []
    objects: dict[str, bytes] = {}

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
        if self.fail_upload:
            raise ProviderError("UPLOAD_FAILED", "failed")
        self.__class__.objects[object_key] = fileobj.read()

    def head_object(self, object_key: str) -> dict[str, Any] | None:
        value = self.__class__.objects.get(object_key)
        return {"size_bytes": len(value)} if value is not None else None

    def build_public_url(self, object_key: str) -> str:
        return f"{self.config['public_base_url'].rstrip('/')}/{object_key}"

    def get_metrics(self, _from_time, _to_time):
        return {"enabled": False, "status": "disabled"}


@pytest.fixture
def registry():
    FakeProvider.instances.clear()
    FakeProvider.objects.clear()
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
    activate(client)
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
