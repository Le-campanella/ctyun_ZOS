from __future__ import annotations

import base64
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep
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
from app.security import hash_delete_token

ADMIN_AUTH = "Bearer test-admin-key-000000000000000000"


class FakeProvider(StorageProvider):
    provider_id = "fake"
    schema_version = 1
    instances: list["FakeProvider"] = []
    objects: dict[str, bytes] = {}
    upload_started = Event()
    upload_release = Event()
    delete_started = Event()
    delete_release = Event()
    delete_requests: list[tuple[str, str | None]] = []
    etags: dict[str, str] = {}
    version_ids: dict[str, str | None] = {}
    head_requests = 0
    last_upload_file = None

    def __init__(self, config, credentials, _settings):
        self.config, self.credentials = self.validate(config, credentials)
        self.fail_upload = self.config.get("fail_upload", False)
        self.delete_calls: list[tuple[str, str | None]] = []
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
        self.__class__.last_upload_file = fileobj
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
        self.__class__.head_requests += 1
        if delay := self.config.get("head_delay"):
            sleep(delay)
        if self.config.get("head_timeout"):
            raise ProviderError("STORAGE_TIMEOUT", "timeout", uncertain=True)
        if self.config.get("head_missing"):
            return None
        value = self.__class__.objects.get(object_key)
        if value is None:
            return None
        return ObjectMetadata(
            size_bytes=len(value) + self.config.get("head_size_delta", 0),
            etag=None
            if self.config.get("head_missing_etag")
            else self.__class__.etags.get(object_key, '"fake-etag"'),
            version_id=self.__class__.version_ids.get(
                object_key, version_id or self.config.get("version_id")
            ),
            content_type="application/octet-stream",
            last_modified="2026-07-31T00:00:00Z",
        )

    def delete_object(self, object_key: str, version_id: str | None = None) -> None:
        self.delete_calls.append((object_key, version_id))
        self.__class__.delete_requests.append((object_key, version_id))
        if self.config.get("block_delete"):
            self.__class__.delete_started.set()
            self.__class__.delete_release.wait(3)
        if self.config.get("fail_delete"):
            raise ProviderError("DELETE_FAILED", "failed")
        if self.config.get("uncertain_delete"):
            if self.config.get("delete_before_timeout"):
                self.__class__.objects.pop(object_key, None)
            raise ProviderError("DELETE_PENDING", "pending", uncertain=True)
        self.__class__.objects.pop(object_key, None)

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
    FakeProvider.delete_started.clear()
    FakeProvider.delete_release.clear()
    FakeProvider.delete_requests.clear()
    FakeProvider.etags.clear()
    FakeProvider.version_ids.clear()
    FakeProvider.head_requests = 0
    FakeProvider.last_upload_file = None
    registry = ProviderRegistry()
    registry.register(FakeProvider)
    return registry


@pytest.fixture
def client(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.headers["Authorization"] = ADMIN_AUTH
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
            "endpoint_url": "https://192.0.2.10",
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
        headers={"X-Settings-Request": "true", "Authorization": ADMIN_AUTH},
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


def test_admin_routes_require_key_while_upload_data_plane_stays_open(client):
    authorization = client.headers.pop("Authorization")
    try:
        assert client.get("/healthz").status_code == 200
        assert (
            client.post(
                "/v1/uploads/validate", files={"file": ("test.bin", b"payload")}
            ).status_code
            == 200
        )
        for path in (
            "/v1/settings/storage",
            "/v1/upload-tasks",
            "/v1/dashboard/logs",
            "/dashboard",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"
        wrong = client.get(
            "/v1/settings/storage",
            headers={"Authorization": "Bearer " + "x" * 32},
        )
        assert wrong.status_code == 401
        basic = base64.b64encode(b"admin:test-admin-key-000000000000000000").decode()
        assert (
            client.get(
                "/v1/settings/storage", headers={"Authorization": f"Basic {basic}"}
            ).status_code
            == 200
        )
    finally:
        client.headers["Authorization"] = authorization


def test_client_auth_scopes_idempotency_and_enforces_quota(
    settings, database, registry
):
    client_key_a = "a" * 32
    client_key_b = "b" * 32
    limited = replace(
        settings,
        client_api_keys=(("service-a", client_key_a), ("service-b", client_key_b)),
        client_max_objects=1,
        client_max_bytes=100,
        upload_rate_limit_per_minute=10,
    )
    app = create_app(
        settings=limited, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        activate(test_client)
        missing = test_client.post(
            "/v1/uploads/validate", files={"file": ("a.bin", b"a")}
        )
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "CLIENT_AUTH_REQUIRED"

        def headers(client_id, key):
            return {
                "X-Client-ID": client_id,
                "X-Client-Key": key,
                "Idempotency-Key": "shared-job",
            }

        first_a = test_client.post(
            "/v1/uploads",
            headers=headers("service-a", client_key_a),
            files={"file": ("a.bin", b"first")},
        )
        first_b = test_client.post(
            "/v1/uploads",
            headers=headers("service-b", client_key_b),
            files={"file": ("b.bin", b"second")},
        )
        quota = test_client.post(
            "/v1/uploads",
            headers={
                "X-Client-ID": "service-a",
                "X-Client-Key": client_key_a,
                "Idempotency-Key": "another-job",
            },
            files={"file": ("c.bin", b"third")},
        )

        assert first_a.status_code == first_b.status_code == 201
        assert first_a.json()["task_id"] != first_b.json()["task_id"]
        assert quota.status_code == 429
        assert quota.json()["error"]["code"] == "CLIENT_QUOTA_EXCEEDED"
        assert quota.headers["Retry-After"] == "3600"
        detail = test_client.get(
            f"/v1/upload-tasks/{first_a.json()['task_id']}",
            headers={"Authorization": ADMIN_AUTH},
        ).json()
        assert detail["client_id"] == "service-a"
        summary = test_client.get(
            "/v1/dashboard/summary", headers={"Authorization": ADMIN_AUTH}
        ).json()
        assert summary["uploads"]["quota"]["warning"] is True


def test_source_ip_rate_limit_returns_retry_after(settings, database, registry):
    limited = replace(settings, upload_rate_limit_per_minute=2)
    app = create_app(
        settings=limited, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        for _ in range(2):
            assert (
                test_client.post(
                    "/v1/uploads/validate", files={"file": ("a.bin", b"a")}
                ).status_code
                == 200
            )
        blocked = test_client.post(
            "/v1/uploads/validate", files={"file": ("a.bin", b"a")}
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "UPLOAD_RATE_LIMITED"
        assert int(blocked.headers["Retry-After"]) >= 1


def test_storage_endpoint_allowlist_rejects_loopback_and_unlisted_private(client):
    for endpoint in ("https://127.0.0.1", "https://10.0.0.1"):
        response = client.put(
            "/v1/settings/storage",
            headers={"X-Settings-Request": "true"},
            json=storage_payload(endpoint_url=endpoint),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "STORAGE_ENDPOINT_FORBIDDEN"
    assert client.get("/v1/settings/storage").json()["revision"] == 0
    assert client.get("/dashboard").status_code == 200
    upload = client.post("/v1/uploads", files={"file": ("a.txt", b"a")})
    assert upload.status_code == 503
    assert upload.json()["error"]["code"] == "STORAGE_DEFAULT_NOT_CONFIGURED"


def test_openapi_and_first_upload_response_contract(client, database):
    contract = client.get("/openapi.json").json()
    schemas = contract["components"]["schemas"]
    assert set(contract["components"]["securitySchemes"]) == {
        "AdminBearer",
        "AdminBasic",
        "AdminKeyHeader",
        "ClientIdHeader",
        "ClientKeyHeader",
    }
    assert "security" in contract["paths"]["/v1/settings/storage"]["get"]
    assert {
        "ClientIdHeader": [],
        "ClientKeyHeader": [],
    } in contract["paths"]["/v1/uploads"]["post"]["security"]
    assert "UploadResponse" in schemas
    upload_schema = schemas["UploadResponse"]
    assert set(upload_schema["required"]) == {
        "task_id",
        "storage_preset",
        "key",
        "url",
        "size_bytes",
        "content_type",
        "etag",
        "version_id",
        "delete_capability_available",
        "delete_token",
    }
    assert upload_schema["additionalProperties"] is False

    activate(client)
    response = client.post(
        "/v1/uploads",
        files={"file": ("contract.txt", b"contract", "text/plain")},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == set(upload_schema["required"])
    assert body["storage_preset"] == "default"
    assert body["size_bytes"] == 8
    assert body["content_type"] == "text/plain"
    assert body["etag"] == '"fake-etag"'
    assert body["version_id"] is None
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["delete_token"])
    assert body["delete_capability_available"] is True
    assert response.headers["Cache-Control"] == "no-store"
    with database.connect() as connection:
        stored_hash = connection.execute(
            "SELECT delete_token_hash FROM upload_tasks WHERE id=?",
            (body["task_id"],),
        ).fetchone()[0]
    assert stored_hash == hash_delete_token(body["delete_token"])
    assert body["delete_token"].encode() not in stored_hash
    serialized = response.text.lower()
    for secret_name in (
        "access_key",
        "secret_key",
        "settings_encryption_key",
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
    assert (
        dashboard_js.status_code == settings_js.status_code == chart.status_code == 200
    )
    assert 'src="/static/chart.umd.min.js"' in dashboard.text
    assert "https://" not in dashboard.text
    assert 'type="password"' in settings.text
    assert "test-ak" not in settings.text and "test-sk" not in settings.text
    assert "innerHTML" not in dashboard_js.text
    assert "innerHTML" not in settings_js.text
    assert "localStorage" not in settings_js.text
    assert "sessionStorage" not in settings_js.text
    assert 'id="receive-test-file"' in dashboard.text
    assert (
        'id="receive-test-real-upload" type="checkbox" role="switch"' in dashboard.text
    )
    assert 'id="receive-test-preset"' in dashboard.text
    assert 'id="preset-list"' in settings.text
    assert 'id="preset-key"' in settings.text
    assert 'id="state-revision"' in settings.text
    assert "/v1/uploads/validate" in dashboard_js.text
    assert 'real ? "/v1/uploads" : "/v1/uploads/validate"' in dashboard_js.text
    assert '"X-Storage-Preset": preset' in dashboard_js.text
    assert 'body.delete_token = "[REDACTED]"' in dashboard_js.text
    assert "/v1/settings/storage/presets" in settings_js.text
    assert "/v1/settings/storage/providers" in settings_js.text
    assert "/v1/settings/storage/default" in settings_js.text
    assert "expected_state_revision" in settings_js.text
    assert "provider: provider.id" in settings_js.text
    assert 'provider: "ctyun_zos"' not in settings_js.text
    assert "storage_preset" in dashboard_js.text
    assert "delete_error_code" in dashboard_js.text
    assert "X-Delete-Token" not in dashboard_js.text


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

    empty = client.post("/v1/uploads/validate", files={"file": ("empty.txt", b"")})
    oversized = client.post(
        "/v1/uploads/validate", files={"file": ("large.bin", b"x" * 101)}
    )
    assert empty.json()["error"]["code"] == "FILE_EMPTY"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_settings_activation_masks_credentials_and_preserves_them(client, database):
    assert (
        client.get("/v1/settings/storage/providers").json()["items"][0]["id"] == "fake"
    )
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


def test_runtime_keeps_independent_preset_snapshots(client):
    activate(client)
    runtime = client.app.state.runtime
    created = client.portal.call(
        runtime.create_storage_preset,
        "archive",
        "Archive",
        storage_payload(public_base_url="https://archive.example"),
    )
    assert created["revision"] == 1
    snapshots = runtime.snapshots()
    assert set(snapshots) == {"default", "archive"}
    assert runtime.active_snapshot().preset_key == "default"
    old_archive = snapshots["archive"]

    updated = storage_payload(
        revision=1,
        credentials=False,
        public_base_url="https://archive-v2.example",
    )
    client.portal.call(runtime.activate_storage, updated, "archive")
    assert runtime.active_snapshot("archive").revision == 2
    assert runtime.active_snapshot("default").revision == 1
    assert (
        runtime.provider_for_config(old_archive.storage_config_id)
        is old_archive.provider
    )

    client.portal.call(
        runtime.set_default_storage_preset,
        "archive",
        "default",
        1,
    )
    assert runtime.active_snapshot().preset_key == "archive"
    upload = client.post("/v1/uploads", files={"file": ("a.txt", b"archive")})
    assert upload.status_code == 201
    assert upload.json()["storage_preset"] == "archive"
    assert upload.json()["url"].startswith("https://archive-v2.example/")


def test_storage_preset_management_api_lifecycle(client):
    empty = client.get("/v1/settings/storage/presets")
    assert empty.json() == {"items": []}
    assert empty.headers["Cache-Control"] == "no-store"

    main = storage_payload()
    main.pop("expected_revision")
    main |= {"preset_key": "main", "display_name": "Main"}
    no_header = client.post("/v1/settings/storage/presets", json=main)
    assert no_header.status_code == 400

    created_main = client.post(
        "/v1/settings/storage/presets",
        headers={"X-Settings-Request": "true"},
        json=main,
    )
    assert created_main.status_code == 201
    assert created_main.headers["Cache-Control"] == "no-store"
    assert created_main.json()["is_default"] is True
    assert created_main.json()["state_revision"] == 1
    assert "test-ak" not in created_main.text

    archive = storage_payload(public_base_url="https://archive.example")
    archive.pop("expected_revision")
    archive |= {"preset_key": "archive", "display_name": "Archive"}
    created_archive = client.post(
        "/v1/settings/storage/presets",
        headers={"X-Settings-Request": "true"},
        json=archive,
    )
    assert created_archive.status_code == 201
    assert created_archive.json()["is_default"] is False

    duplicate = client.post(
        "/v1/settings/storage/presets",
        headers={"X-Settings-Request": "true"},
        json=archive,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PRESET_STATE_CONFLICT"
    invalid = client.post(
        "/v1/settings/storage/presets",
        headers={"X-Settings-Request": "true"},
        json=archive | {"preset_key": "Bad_Key"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "STORAGE_PRESET_INVALID"

    listed = client.get("/v1/settings/storage/presets").json()["items"]
    assert [item["preset_key"] for item in listed] == ["archive", "main"]
    assert listed[0]["config_revision"] == 1
    assert listed[0]["endpoint_host"] == "192.0.2.10"
    assert all("config" not in item and "credentials" not in item for item in listed)
    assert "test-ak" not in client.get("/v1/settings/storage/presets").text

    detail = client.get("/v1/settings/storage/presets/archive")
    assert detail.status_code == 200
    assert detail.json()["config"]["public_base_url"] == "https://archive.example"
    assert detail.json()["credentials"]["access_key_masked"] == "****t-ak"
    missing = client.get("/v1/settings/storage/presets/missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "STORAGE_PRESET_NOT_FOUND"

    candidate = storage_payload(credentials=False)
    candidate.pop("expected_revision")
    candidate["preset_key"] = "archive"
    tested = client.post(
        "/v1/settings/storage/test",
        headers={"X-Settings-Request": "true"},
        json=candidate,
    )
    assert tested.status_code == 200

    update = storage_payload(
        revision=1,
        credentials=False,
        public_base_url="https://archive-v2.example",
    )
    saved = client.put(
        "/v1/settings/storage/presets/archive",
        headers={"X-Settings-Request": "true"},
        json=update,
    )
    assert saved.status_code == 200
    assert saved.json()["previous_revision"] == 1
    assert saved.json()["revision"] == 2
    stale_config = client.put(
        "/v1/settings/storage/presets/archive",
        headers={"X-Settings-Request": "true"},
        json=update,
    )
    assert stale_config.status_code == 409
    assert stale_config.json()["error"]["code"] == "CONFIG_REVISION_CONFLICT"

    stale_state = client.patch(
        "/v1/settings/storage/presets/archive",
        headers={"X-Settings-Request": "true"},
        json={"expected_state_revision": 99, "display_name": "Cold"},
    )
    assert stale_state.status_code == 409
    renamed = client.patch(
        "/v1/settings/storage/presets/archive",
        headers={"X-Settings-Request": "true"},
        json={
            "expected_state_revision": 1,
            "display_name": "Cold",
            "enabled": False,
        },
    )
    assert renamed.json()["state_revision"] == 2
    disabled_default = client.put(
        "/v1/settings/storage/default",
        headers={"X-Settings-Request": "true"},
        json={
            "preset_key": "archive",
            "expected_default_preset": "main",
            "expected_state_revision": 2,
        },
    )
    assert disabled_default.status_code == 409
    assert disabled_default.json()["error"]["code"] == "DEFAULT_PRESET_CONFLICT"

    enabled = client.patch(
        "/v1/settings/storage/presets/archive",
        headers={"X-Settings-Request": "true"},
        json={"expected_state_revision": 2, "enabled": True},
    )
    assert enabled.json()["state_revision"] == 3
    wrong_default = client.put(
        "/v1/settings/storage/default",
        headers={"X-Settings-Request": "true"},
        json={
            "preset_key": "archive",
            "expected_default_preset": "wrong",
            "expected_state_revision": 3,
        },
    )
    assert wrong_default.status_code == 409

    switched = client.put(
        "/v1/settings/storage/default",
        headers={"X-Settings-Request": "true"},
        json={
            "preset_key": "archive",
            "expected_default_preset": "main",
            "expected_state_revision": 3,
        },
    )
    assert switched.status_code == 200
    assert switched.json()["is_default"] is True
    assert client.get("/v1/settings/storage").json()["preset_key"] == "archive"
    cannot_disable_default = client.patch(
        "/v1/settings/storage/presets/archive",
        headers={"X-Settings-Request": "true"},
        json={
            "expected_state_revision": switched.json()["state_revision"],
            "enabled": False,
        },
    )
    assert cannot_disable_default.status_code == 409
    assert cannot_disable_default.json()["error"]["code"] == "PRESET_STATE_CONFLICT"

    upload = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "main"},
        files={"file": ("a.txt", b"still-default")},
    )
    assert upload.status_code == 201
    assert upload.json()["storage_preset"] == "main"
    assert upload.json()["url"].startswith("https://files.example/")


def test_upload_routes_by_preset_and_scopes_idempotency(client):
    activate(client)
    runtime = client.app.state.runtime
    client.portal.call(
        runtime.create_storage_preset,
        "archive",
        "Archive",
        storage_payload(public_base_url="https://archive.example"),
    )

    headers = {
        "X-Storage-Preset": "archive",
        "Idempotency-Key": "archive-job",
    }
    first = client.post(
        "/v1/uploads", headers=headers, files={"file": ("a.txt", b"archive")}
    )
    replay = client.post(
        "/v1/uploads",
        headers={"Idempotency-Key": "archive-job"},
        files={"file": ("ignored.txt", b"ignored")},
    )
    same_scope = client.post(
        "/v1/uploads", headers=headers, files={"file": ("ignored.txt", b"ignored")}
    )
    mismatch = client.post(
        "/v1/uploads",
        headers={
            "X-Storage-Preset": "default",
            "Idempotency-Key": "archive-job",
        },
        files={"file": ("ignored.txt", b"ignored")},
    )

    assert first.status_code == 201
    assert first.json()["storage_preset"] == "archive"
    assert first.json()["url"].startswith("https://archive.example/")
    assert replay.status_code == same_scope.status_code == 200
    assert (
        replay.json()["task_id"]
        == same_scope.json()["task_id"]
        == first.json()["task_id"]
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_SCOPE_MISMATCH"
    assert mismatch.json()["task_id"] == first.json()["task_id"]

    runtime.update_storage_preset("archive", 1, enabled=False)
    disabled_replay = client.post(
        "/v1/uploads",
        headers=headers,
        files={"file": ("ignored.txt", b"ignored")},
    )
    disabled_new = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "archive"},
        files={"file": ("new.txt", b"new")},
    )
    missing = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "missing"},
        files={"file": ("new.txt", b"new")},
    )
    invalid = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "Bad_Key"},
        files={"file": ("new.txt", b"new")},
    )

    assert disabled_replay.status_code == 200
    assert disabled_new.status_code == 409
    assert disabled_new.json()["error"]["code"] == "STORAGE_PRESET_DISABLED"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "STORAGE_PRESET_NOT_FOUND"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "STORAGE_PRESET_INVALID"

    runtime.update_storage_preset("archive", 2, enabled=True)
    with runtime.database.transaction() as connection:
        connection.execute(
            """
            UPDATE storage_configs SET status='inactive'
            WHERE preset_id=(
                SELECT id FROM storage_presets WHERE preset_key='archive'
            )
            """
        )
    runtime._load_active()
    not_configured = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "archive"},
        files={"file": ("new.txt", b"new")},
    )
    assert not_configured.status_code == 503
    assert not_configured.json()["error"]["code"] == "STORAGE_NOT_CONFIGURED"

    client.portal.call(
        runtime.create_storage_preset,
        "broken",
        "Broken",
        storage_payload(fail_upload=True),
    )
    failed = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "broken"},
        files={"file": ("failed.txt", b"failed")},
    )
    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "UPLOAD_FAILED"
    detail = client.get(f"/v1/upload-tasks/{failed.json()['task_id']}").json()
    assert detail["storage_preset"] == "broken"
    tasks = client.get("/v1/upload-tasks").json()["items"]
    assert {item["storage_preset"] for item in tasks} == {"archive", "broken"}


def test_upload_freezes_selected_preset_revision(client):
    activate(client)
    runtime = client.app.state.runtime
    client.portal.call(
        runtime.create_storage_preset,
        "archive",
        "Archive",
        storage_payload(
            public_base_url="https://archive-v1.example",
            block_upload=True,
        ),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            client.post,
            "/v1/uploads",
            headers={"X-Storage-Preset": "archive"},
            files={"file": ("first.bin", b"one")},
        )
        assert FakeProvider.upload_started.wait(2)
        client.portal.call(
            runtime.activate_storage,
            storage_payload(
                revision=1,
                credentials=False,
                public_base_url="https://archive-v2.example",
            ),
            "archive",
        )
        FakeProvider.upload_release.set()
        first_response = first.result(timeout=3)

    second = client.post(
        "/v1/uploads",
        headers={"X-Storage-Preset": "archive"},
        files={"file": ("second.bin", b"two")},
    )
    assert first_response.status_code == second.status_code == 201
    assert first_response.json()["url"].startswith("https://archive-v1.example/")
    assert second.json()["url"].startswith("https://archive-v2.example/")
    first_task = client.get(
        f"/v1/upload-tasks/{first_response.json()['task_id']}"
    ).json()
    second_task = client.get(f"/v1/upload-tasks/{second.json()['task_id']}").json()
    assert first_task["storage_config_revision"] == 1
    assert second_task["storage_config_revision"] == 2


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
    summary_body = client.get("/v1/dashboard/summary").json()
    assert summary_body["service"]["checks"]["config"]["preset_key"] == "default"
    summary = summary_body["uploads"]
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
    assert second.headers["Cache-Control"] == "no-store"
    assert first.json()["delete_token"] is not None
    assert second.json() == first.json() | {"delete_token": None}
    assert len(FakeProvider.objects) == 1


def test_verified_delete_is_version_exact_and_idempotent(client):
    activate(client, version_id="version-1")
    upload = client.post(
        "/v1/uploads", files={"file": ("delete.txt", b"delete-me")}
    ).json()
    runtime = client.app.state.runtime
    task = runtime.database.task_by_id(upload["task_id"])
    original_provider = runtime.provider_for_config(task["storage_config_id"])
    activate(
        client,
        revision=1,
        credentials=False,
        public_base_url="https://new-revision.example",
        version_id="version-1",
    )
    headers = {"X-Delete-Token": upload["delete_token"]}

    deleted = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object", headers=headers
    )
    repeated = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object", headers=headers
    )

    assert deleted.status_code == repeated.status_code == 200
    assert deleted.json()["object_status"] == "deleted"
    assert deleted.json()["already_deleted"] is False
    assert deleted.json()["already_absent"] is False
    assert repeated.json()["already_deleted"] is True
    assert len(FakeProvider.delete_requests) == 1
    assert FakeProvider.delete_requests[0] == (upload["key"], "version-1")
    assert original_provider.delete_calls == [(upload["key"], "version-1")]
    assert runtime.active_snapshot().provider.delete_calls == []
    detail = client.get(f"/v1/upload-tasks/{upload['task_id']}").json()
    assert detail["object_status"] == "deleted"
    assert detail["deleted_at"] == deleted.json()["deleted_at"]
    assert detail["delete_error_code"] is None
    assert detail["delete_started_at"] is not None
    assert upload["delete_token"] not in client.get("/v1/upload-tasks").text
    assert upload["delete_token"] not in client.get("/dashboard").text
    started = client.get("/v1/dashboard/logs?event=object_delete_started").json()[
        "items"
    ]
    succeeded = client.get("/v1/dashboard/logs?event=object_delete_succeeded").json()[
        "items"
    ]
    assert started[0]["details"]["to_status"] == "deleting"
    assert succeeded[0]["details"]["provider_result"] == "confirmed_absent"
    assert upload["delete_token"] not in str(started + succeeded)


def test_delete_rejects_invalid_capability_body_and_legacy_task(client):
    activate(client)
    first = client.post("/v1/uploads", files={"file": ("first.txt", b"first")}).json()
    second = client.post(
        "/v1/uploads", files={"file": ("second.txt", b"second")}
    ).json()
    path = f"/v1/upload-tasks/{first['task_id']}/object"

    assert client.delete(path).status_code == 403
    assert (
        client.delete(path, headers={"X-Delete-Token": "x" * 257}).json()["error"][
            "code"
        ]
        == "DELETE_TOKEN_INVALID"
    )
    assert (
        client.delete(path, headers={"X-Delete-Token": "tampered"}).json()["error"][
            "code"
        ]
        == "DELETE_TOKEN_INVALID"
    )
    assert (
        client.delete(path, headers={"X-Delete-Token": second["delete_token"]}).json()[
            "error"
        ]["code"]
        == "DELETE_TOKEN_INVALID"
    )
    body = client.request(
        "DELETE",
        path,
        headers={"X-Delete-Token": first["delete_token"]},
        content=b"{}",
    )
    assert body.status_code == 400
    assert body.json()["error"]["code"] == "BAD_REQUEST"
    assert FakeProvider.delete_requests == []

    client.app.state.runtime.database.update_task(
        first["task_id"], object_status="legacy_unverified"
    )
    legacy = client.delete(path, headers={"X-Delete-Token": first["delete_token"]})
    assert legacy.status_code == 409
    assert legacy.json()["error"]["code"] == "OBJECT_NOT_DELETABLE"
    missing = client.delete(
        f"/v1/upload-tasks/{uuid4()}/object",
        headers={"X-Delete-Token": first["delete_token"]},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"


@pytest.mark.parametrize("change", ["size", "etag", "version"])
def test_delete_refuses_changed_remote_object(client, change):
    activate(client, version_id="version-1")
    upload = client.post(
        "/v1/uploads", files={"file": ("changed.txt", b"original")}
    ).json()
    if change == "size":
        FakeProvider.objects[upload["key"]] += b"x"
    elif change == "etag":
        FakeProvider.etags[upload["key"]] = '"changed-etag"'
    else:
        FakeProvider.version_ids[upload["key"]] = "version-2"

    response = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object",
        headers={"X-Delete-Token": upload["delete_token"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OBJECT_CHANGED"
    assert FakeProvider.delete_requests == []
    task = client.get(f"/v1/upload-tasks/{upload['task_id']}").json()
    assert task["object_status"] == "present"
    assert task["delete_error_code"] == "OBJECT_CHANGED"


def test_delete_marks_preexisting_absence_without_provider_delete(client):
    activate(client)
    upload = client.post("/v1/uploads", files={"file": ("absent.txt", b"gone")}).json()
    FakeProvider.objects.pop(upload["key"])

    response = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object",
        headers={"X-Delete-Token": upload["delete_token"]},
    )
    assert response.status_code == 200
    assert response.json()["already_absent"] is True
    assert response.json()["already_deleted"] is False
    assert FakeProvider.delete_requests == []


@pytest.mark.parametrize(
    ("config", "status", "code", "object_status"),
    [
        ({"fail_delete": True}, 502, "DELETE_FAILED", "present"),
        (
            {"uncertain_delete": True, "delete_before_timeout": True},
            202,
            "DELETE_PENDING",
            "delete_unknown",
        ),
    ],
)
def test_delete_provider_failure_is_never_reported_as_success(
    client, config, status, code, object_status
):
    activate(client, **config)
    upload = client.post(
        "/v1/uploads", files={"file": ("failure.txt", b"payload")}
    ).json()
    response = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object",
        headers={"X-Delete-Token": upload["delete_token"]},
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    task = client.get(f"/v1/upload-tasks/{upload['task_id']}").json()
    assert task["object_status"] == object_status
    assert task["delete_error_code"] == code


def test_restart_recovers_uncertain_delete_to_deleted(settings, database, registry):
    first_app = create_app(
        settings=settings,
        registry=registry,
        database=database,
        background=False,
    )
    with TestClient(first_app, raise_server_exceptions=False) as first_client:
        first_client.headers["Authorization"] = ADMIN_AUTH
        activate(
            first_client,
            uncertain_delete=True,
            delete_before_timeout=True,
        )
        upload = first_client.post(
            "/v1/uploads", files={"file": ("restart.txt", b"payload")}
        ).json()
        pending = first_client.delete(
            f"/v1/upload-tasks/{upload['task_id']}/object",
            headers={"X-Delete-Token": upload["delete_token"]},
        )
        assert pending.status_code == 202

    second_app = create_app(
        settings=settings,
        registry=registry,
        database=database,
        background=False,
    )
    with TestClient(second_app, raise_server_exceptions=False) as second_client:
        second_client.headers["Authorization"] = ADMIN_AUTH
        recovered = second_client.get(f"/v1/upload-tasks/{upload['task_id']}").json()
        assert recovered["object_status"] == "deleted"
        assert recovered["deleted_at"] is not None
        audit = second_client.get(
            "/v1/dashboard/logs?event=object_delete_recovered"
        ).json()["items"]
        assert audit[0]["task_id"] == upload["task_id"]
        assert audit[0]["details"]["from_status"] == "delete_unknown"
        assert audit[0]["details"]["to_status"] == "deleted"


@pytest.mark.parametrize(
    ("changed", "expected_status", "expected_error"),
    [
        (False, "present", "DELETE_FAILED"),
        (True, "present", "OBJECT_CHANGED"),
    ],
)
def test_delete_recovery_handles_present_object_conservatively(
    client, changed, expected_status, expected_error
):
    activate(client, uncertain_delete=True)
    upload = client.post(
        "/v1/uploads", files={"file": ("recover.txt", b"payload")}
    ).json()
    pending = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object",
        headers={"X-Delete-Token": upload["delete_token"]},
    )
    assert pending.status_code == 202
    if changed:
        FakeProvider.etags[upload["key"]] = '"changed-etag"'

    client.portal.call(client.app.state.runtime.recover)
    task = client.get(f"/v1/upload-tasks/{upload['task_id']}").json()
    assert task["object_status"] == expected_status
    assert task["delete_error_code"] == expected_error


@pytest.mark.parametrize(
    ("object_exists", "expected_status"),
    [(False, "deleted"), (True, "present")],
)
def test_recovery_claims_only_stale_deleting_tasks(
    client, object_exists, expected_status
):
    activate(client)
    upload = client.post(
        "/v1/uploads", files={"file": ("stale.txt", b"payload")}
    ).json()
    runtime = client.app.state.runtime
    runtime.database.update_task(
        upload["task_id"],
        object_status="deleting",
        delete_request_id="stale-delete",
        delete_started_at="2026-01-01T00:00:00Z",
    )
    if not object_exists:
        FakeProvider.objects.pop(upload["key"])
    recent = client.post(
        "/v1/uploads", files={"file": ("recent.txt", b"payload")}
    ).json()
    runtime.database.update_task(
        recent["task_id"],
        object_status="deleting",
        delete_request_id="recent-delete",
        delete_started_at=utc_now(),
    )

    client.portal.call(runtime.recover)
    task = client.get(f"/v1/upload-tasks/{upload['task_id']}").json()
    recent_task = client.get(f"/v1/upload-tasks/{recent['task_id']}").json()
    assert task["object_status"] == expected_status
    assert recent_task["object_status"] == "deleting"
    if expected_status == "deleted":
        assert task["deleted_at"] is not None
    else:
        assert task["delete_error_code"] == "DELETE_FAILED"


def test_concurrent_delete_has_one_provider_caller(client):
    activate(client, block_delete=True)
    upload = client.post(
        "/v1/uploads", files={"file": ("concurrent.txt", b"payload")}
    ).json()
    path = f"/v1/upload-tasks/{upload['task_id']}/object"
    headers = {"X-Delete-Token": upload["delete_token"]}

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.delete, path, headers=headers)
        assert FakeProvider.delete_started.wait(2)
        second = client.delete(path, headers=headers)
        FakeProvider.delete_release.set()
        first_response = first.result(timeout=3)

    assert first_response.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DELETE_IN_PROGRESS"
    assert len(FakeProvider.delete_requests) == 1


def test_delete_database_failure_does_not_claim_success(client, monkeypatch):
    activate(client)
    upload = client.post("/v1/uploads", files={"file": ("db.txt", b"payload")}).json()
    database = client.app.state.runtime.database
    original = database.update_task

    def fail_final_update(task_id, **changes):
        if changes.get("object_status") == "deleted":
            raise sqlite3.OperationalError("disk full")
        return original(task_id, **changes)

    monkeypatch.setattr(database, "update_task", fail_final_update)
    response = client.delete(
        f"/v1/upload-tasks/{upload['task_id']}/object",
        headers={"X-Delete-Token": upload["delete_token"]},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "DATABASE_ERROR"
    assert upload["key"] not in FakeProvider.objects
    assert database.task_by_id(upload["task_id"])["object_status"] == "deleting"


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
    assert empty_task["object_status"] == "absent"

    oversized = client.post("/v1/uploads", files={"file": ("large.bin", b"x" * 101)})
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
    response = client.post("/v1/uploads", files={"file": ("a.bin", b"payload")})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPLOAD_FAILED"
    task = client.get(f"/v1/upload-tasks/{response.json()['task_id']}").json()
    assert task["status"] == "failed"
    assert task["object_status"] == "absent"
    assert task["public_url"] is None


def test_received_size_is_persisted_before_remote_upload(client, monkeypatch):
    activate(client)
    provider = FakeProvider.instances[-1]
    original = provider.upload_file

    def checked_upload(fileobj, object_key, content_type):
        task = client.app.state.runtime.database.list_tasks(limit=1, offset=0)[0]
        assert task["status"] == "uploading"
        assert task["size_bytes"] == 7
        return original(fileobj, object_key, content_type)

    monkeypatch.setattr(provider, "upload_file", checked_upload)

    response = client.post("/v1/uploads", files={"file": ("a.bin", b"payload")})

    assert response.status_code == 201


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
        ({"head_missing_etag": True}, "UPLOAD_CONFIRMATION_FAILED"),
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


def test_database_write_wait_does_not_block_task_queries(client, database, monkeypatch):
    activate(client)
    started = Event()
    release = Event()
    original = database.create_task_with_quota

    def blocked(*args, **kwargs):
        started.set()
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "create_task_with_quota", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        uploading = pool.submit(
            client.post, "/v1/uploads", files={"file": ("a.bin", b"payload")}
        )
        assert started.wait(2)
        before = monotonic()
        listing = client.get("/v1/upload-tasks")
        elapsed = monotonic() - before
        release.set()
        assert uploading.result(timeout=3).status_code == 201

    assert listing.status_code == 200
    assert elapsed < 1


def test_upload_path_reuses_framework_spool_without_second_copy():
    source = (Path(__file__).resolve().parents[1] / "app/uploads.py").read_text()

    assert "SpooledTemporaryFile" not in source
    assert "_copy_upload" not in source
    assert "provider.upload_file, source.file" in source


def test_upload_form_closes_framework_file(client):
    activate(client)
    assert (
        client.post("/v1/uploads", files={"file": ("a.bin", b"payload")}).status_code
        == 201
    )
    assert FakeProvider.last_upload_file.closed is True


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
    candidate = storage_payload(revision=1, credentials=False)
    candidate.pop("expected_revision")
    cross_origin = client.post(
        "/v1/settings/storage/test",
        headers={
            "X-Settings-Request": "true",
            "Origin": "https://attacker.example",
        },
        json=candidate,
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
            b'--x\r\nContent-Disposition: form-data; name="file"; '
            b'filename="a.bin"\r\n\r\n' + b"x" * 2_100 + b"\r\n--x--\r\n"
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


def test_ready_reports_event_log_degradation(client):
    activate(client)
    runtime = client.app.state.runtime
    runtime.log.degraded = True
    runtime.log.last_failure_at = utc_now()

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["event_log"]["status"] == "degraded"


def test_dashboard_disabled_hides_pages_assets_and_dashboard_api(
    settings, database, registry
):
    app = create_app(
        settings=replace(settings, dashboard_enabled=False),
        registry=registry,
        database=database,
        background=False,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.headers["Authorization"] = ADMIN_AUTH
        assert test_client.get("/dashboard").status_code == 404
        assert test_client.get("/static/dashboard.js").status_code == 404
        assert test_client.get("/v1/dashboard/summary").status_code == 404
        assert test_client.get("/v1/settings/storage").status_code == 200


def test_recovery_resolves_existing_and_missing_objects(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as first:
        first.headers["Authorization"] = ADMIN_AUTH
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
        restarted.headers["Authorization"] = ADMIN_AUTH
        items = restarted.get("/v1/upload-tasks").json()["items"]
        states = {item["object_key"]: item["status"] for item in items}
        assert any(value == "succeeded" for value in states.values())
        assert any(value == "failed" for value in states.values())
        object_states = {item["object_key"]: item["object_status"] for item in items}
        assert "present_unclaimed" in object_states.values()
        assert "absent" in object_states.values()


def test_final_database_failure_recovers_as_present_unclaimed(client, monkeypatch):
    activate(client)
    database = client.app.state.runtime.database
    original = database.update_task

    def fail_final_update(task_id, **changes):
        if changes.get("status") == "succeeded":
            raise sqlite3.OperationalError("disk full")
        return original(task_id, **changes)

    monkeypatch.setattr(database, "update_task", fail_final_update)
    response = client.post("/v1/uploads", files={"file": ("orphan.bin", b"payload")})
    task_id = response.json()["task_id"]
    assert response.status_code == 500
    monkeypatch.setattr(database, "update_task", original)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE upload_tasks SET status='unknown', created_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", task_id),
        )

    client.portal.call(client.app.state.runtime.recover)

    task = database.task_by_id(task_id)
    assert task["object_status"] == "present_unclaimed"
    assert task["delete_token_hash"] is None
    detail = client.get(f"/v1/upload-tasks/{task_id}").json()
    assert detail["delete_capability_available"] is False


def test_admin_can_strictly_clean_present_unclaimed_object(client):
    activate(client)
    runtime = client.app.state.runtime
    storage = runtime.database.active_storage()
    task_id = str(uuid4())
    key = f"2026/08/03/{task_id}.bin"
    runtime.database.create_task(
        {
            "id": task_id,
            "request_id": task_id,
            "idempotency_key": None,
            "storage_config_id": storage["id"],
            "filename": "orphan.bin",
            "content_type": "application/octet-stream",
            "object_key": key,
            "public_url": f"https://files.example/{key}",
            "status": "unknown",
            "size_bytes": 7,
            "error_code": "RECOVERY_PENDING",
            "created_at": utc_now(),
            "finished_at": None,
            "duration_ms": None,
        }
    )
    FakeProvider.objects[key] = b"payload"
    runtime.recovery_complete = False
    client.portal.call(runtime.recover)
    assert runtime.database.task_by_id(task_id)["object_status"] == "present_unclaimed"

    response = client.delete(f"/v1/admin/upload-tasks/{task_id}/object")

    assert response.status_code == 200
    assert response.json()["object_status"] == "deleted"
    assert key not in FakeProvider.objects
    audit = runtime.database.list_logs(
        min_level=25,
        limit=10,
        filters={"event": "object_delete_succeeded"},
    )
    assert audit[0]["task_id"] == task_id


def test_local_failure_is_marked_absent_and_removed_by_retention(client):
    activate(client)
    response = client.post("/v1/uploads", files={"file": ("empty.bin", b"")})
    task_id = response.json()["task_id"]
    database = client.app.state.runtime.database
    with database.transaction() as connection:
        connection.execute(
            "UPDATE upload_tasks SET created_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", task_id),
        )

    database.maintain(task_retention_days=1, log_retention_days=30, log_max_rows=100)

    assert database.task_by_id(task_id) is None


def test_recovery_pass_is_bounded(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.headers["Authorization"] = ADMIN_AUTH
        activate(test_client, head_delay=0.02, head_timeout=True)
        storage = database.active_storage()
        for _ in range(100):
            task_id = str(uuid4())
            database.create_task(
                {
                    "id": task_id,
                    "request_id": task_id,
                    "idempotency_key": None,
                    "storage_config_id": storage["id"],
                    "filename": "pending.bin",
                    "content_type": "application/octet-stream",
                    "object_key": f"2026/08/03/{task_id}.bin",
                    "public_url": None,
                    "status": "unknown",
                    "size_bytes": 7,
                    "error_code": "RECOVERY_PENDING",
                    "created_at": utc_now(),
                    "finished_at": None,
                    "duration_ms": None,
                }
            )
        started = monotonic()

        test_client.portal.call(test_client.app.state.runtime.recover)

        assert monotonic() - started < 0.5
        assert database.pending_tasks()
        ready = test_client.get("/readyz").json()["checks"]["recovery"]
        assert FakeProvider.head_requests == 25
        assert ready["pending_uploads"] == 100
        assert ready["pending_tasks"] == 100
        assert ready["oldest_age_seconds"] is not None


def test_recovery_keeps_size_mismatch_unknown(settings, database, registry):
    app = create_app(
        settings=settings, registry=registry, database=database, background=False
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = ADMIN_AUTH
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
        restarted.headers["Authorization"] = ADMIN_AUTH
        task = restarted.get(f"/v1/upload-tasks/{task_id}").json()
        assert task["status"] == "unknown"
        assert task["object_status"] == "pending"
        assert task["error_code"] == "OBJECT_SIZE_MISMATCH"
