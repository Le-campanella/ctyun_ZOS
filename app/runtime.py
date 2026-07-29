from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import uuid4

import anyio

from .config import Settings
from .database import Database, RevisionConflict, utc_now
from .eventlog import EventLogger, NOTIFY
from .providers import ProviderError, ProviderRegistry, StorageProvider
from .security import CredentialCipher


class Runtime:
    def __init__(
        self,
        settings: Settings,
        registry: ProviderRegistry,
        database: Database | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.database = database or Database(
            settings.database_path, settings.sqlite_busy_timeout_ms
        )
        self.cipher = CredentialCipher(settings.encryption_key)
        self.log = EventLogger(self.database)
        self._active_lock = threading.RLock()
        self._active_record: dict[str, Any] | None = None
        self._active_provider: StorageProvider | None = None
        self.schema_ready = False
        self.recovery_complete = False
        self.last_probe: dict[str, Any] = {
            "status": "pending",
            "last_checked_at": None,
            "error_code": None,
        }
        self._stop = asyncio.Event()
        self._background: list[asyncio.Task] = []

    async def start(self, background: bool = True) -> None:
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.database.initialize()
        self.schema_ready = True
        await self._bootstrap_storage()
        self._load_active()
        await self.recover()
        await self.probe()
        self.log.emit(NOTIFY, "service_started", "服务已启动")
        if background:
            self._background = [
                asyncio.create_task(self._probe_loop()),
                asyncio.create_task(self._recovery_loop()),
                asyncio.create_task(self._maintenance_loop()),
            ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._background:
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def _load_active(self) -> None:
        record = self.database.active_storage()
        if record is None:
            return
        provider = self.provider_for_record(record)
        with self._active_lock:
            self._active_record, self._active_provider = record, provider

    def provider_for_record(self, record: dict[str, Any]) -> StorageProvider:
        provider_type = self.registry.get(
            record["provider"], record["provider_schema_version"]
        )
        return provider_type(
            json.loads(record["config_json"]),
            self.cipher.decrypt(record["credentials_ciphertext"]),
            self.settings,
        )

    def active_snapshot(self) -> tuple[dict[str, Any], StorageProvider] | None:
        with self._active_lock:
            if self._active_record is None or self._active_provider is None:
                return None
            return dict(self._active_record), self._active_provider

    async def _bootstrap_storage(self) -> None:
        if not self.settings.bootstrap_storage_from_env:
            return
        if self.database.active_storage() is not None:
            return
        names = (
            "ZOS_ENDPOINT",
            "ZOS_BUCKET",
            "ZOS_PUBLIC_BASE_URL",
            "ZOS_ACCESS_KEY",
            "ZOS_SECRET_KEY",
        )
        values = {name: os.getenv(name) for name in names}
        if not all(values.values()):
            return
        payload = {
            "provider": "ctyun_zos",
            "provider_schema_version": 1,
            "expected_revision": 0,
            "config": {
                "endpoint_url": values["ZOS_ENDPOINT"],
                "bucket": values["ZOS_BUCKET"],
                "public_base_url": values["ZOS_PUBLIC_BASE_URL"],
                "connect_timeout_seconds": int(
                    os.getenv("ZOS_CONNECT_TIMEOUT_SECONDS", "5")
                ),
                "read_timeout_seconds": int(
                    os.getenv("ZOS_READ_TIMEOUT_SECONDS", "300")
                ),
                "max_attempts": int(os.getenv("ZOS_MAX_ATTEMPTS", "2")),
                "verify_tls": os.getenv("ZOS_VERIFY_TLS", "true").lower() == "true",
                "enable_bucket_metrics": os.getenv(
                    "ENABLE_ZOS_BUCKET_METRICS", "false"
                ).lower()
                == "true",
            },
            "credentials": {
                "access_key": values["ZOS_ACCESS_KEY"],
                "secret_key": values["ZOS_SECRET_KEY"],
            },
        }
        await self.activate_storage(payload)

    def current_storage(self) -> dict[str, Any]:
        record = self.database.active_storage()
        if record is None:
            return {
                "configured": False,
                "provider": None,
                "provider_schema_version": None,
                "revision": 0,
                "config": None,
                "credentials": {
                    "access_key_configured": False,
                    "access_key_masked": None,
                    "secret_key_configured": False,
                },
                "last_connection_test": None,
                "activated_at": None,
            }
        credentials = self.cipher.decrypt(record["credentials_ciphertext"])
        access_key = credentials["access_key"]
        return {
            "configured": True,
            "provider": record["provider"],
            "provider_schema_version": record["provider_schema_version"],
            "revision": record["revision"],
            "config": json.loads(record["config_json"]),
            "credentials": {
                "access_key_configured": True,
                "access_key_masked": f"****{access_key[-4:]}",
                "secret_key_configured": True,
            },
            "last_connection_test": {
                "status": "ok",
                "tested_at": record["last_tested_at"],
                "latency_ms": record["last_test_latency_ms"],
            },
            "activated_at": record["activated_at"],
        }

    def _candidate(
        self, payload: dict[str, Any]
    ) -> tuple[str, int, dict, dict, StorageProvider]:
        if not isinstance(payload, dict):
            raise ProviderError("STORAGE_CONFIG_INVALID", "设置请求格式不合法")
        provider_id = payload.get("provider")
        schema_version = payload.get("provider_schema_version")
        if not isinstance(provider_id, str) or not isinstance(schema_version, int):
            raise ProviderError("STORAGE_CONFIG_INVALID", "Provider 信息不完整")
        provider_type = self.registry.get(provider_id, schema_version)
        config = payload.get("config")
        submitted = payload.get("credentials")
        if submitted is not None and not isinstance(submitted, dict):
            raise ProviderError("STORAGE_CONFIG_INVALID", "credentials 格式不合法")
        credentials = dict(submitted or {})
        active = self.database.active_storage()
        if active:
            old_config = json.loads(active["config_json"])
            same_identity = (
                active["provider"] == provider_id
                and active["provider_schema_version"] == schema_version
                and isinstance(config, dict)
                and old_config.get("endpoint_url") == config.get("endpoint_url")
            )
            if same_identity:
                old_credentials = self.cipher.decrypt(
                    active["credentials_ciphertext"]
                )
                for name in ("access_key", "secret_key"):
                    if name not in credentials:
                        credentials[name] = old_credentials[name]
        normalized_config, normalized_credentials = provider_type.validate(
            config, credentials
        )
        provider = provider_type(
            normalized_config, normalized_credentials, self.settings
        )
        return (
            provider_id,
            schema_version,
            normalized_config,
            normalized_credentials,
            provider,
        )

    async def test_storage(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_id, schema_version, _, _, provider = self._candidate(payload)
        started = monotonic()
        latency = await anyio.to_thread.run_sync(provider.test_connection)
        return {
            "status": "ok",
            "provider": provider_id,
            "provider_schema_version": schema_version,
            "tested_at": utc_now(),
            "latency_ms": latency or round((monotonic() - started) * 1_000),
            "checks": {
                "schema": {"status": "ok"},
                "client": {"status": "ok"},
                "head_bucket": {"status": "ok"},
            },
        }

    async def activate_storage(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = payload.get("expected_revision")
        if not isinstance(expected, int) or expected < 0:
            raise ProviderError(
                "STORAGE_CONFIG_INVALID", "expected_revision 不合法"
            )
        active = self.database.active_storage()
        current_revision = active["revision"] if active else 0
        if expected != current_revision:
            raise RevisionConflict(current_revision)
        provider_id, schema_version, config, credentials, provider = self._candidate(
            payload
        )
        latency = await anyio.to_thread.run_sync(provider.test_connection)
        now = utc_now()
        record = self.database.activate_storage(
            {
                "id": str(uuid4()),
                "provider": provider_id,
                "provider_schema_version": schema_version,
                "config_json": json.dumps(
                    config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "credentials_ciphertext": self.cipher.encrypt(credentials),
                "created_at": now,
                "activated_at": now,
                "last_tested_at": now,
                "last_test_latency_ms": latency,
            },
            expected,
        )
        with self._active_lock:
            self._active_record, self._active_provider = record, provider
        self.last_probe = {
            "status": "ok",
            "last_checked_at": now,
            "error_code": None,
        }
        self.log.emit(
            NOTIFY,
            "storage_config_activated",
            "存储配置已激活",
            details={
                "provider": provider_id,
                "revision": record["revision"],
            },
        )
        response = self.current_storage()
        response["previous_revision"] = expected
        return response

    async def probe(self) -> None:
        snapshot = self.active_snapshot()
        if snapshot is None:
            self.last_probe = {
                "status": "error",
                "last_checked_at": utc_now(),
                "error_code": "STORAGE_NOT_CONFIGURED",
            }
            return
        try:
            await anyio.to_thread.run_sync(snapshot[1].test_connection)
        except ProviderError as exc:
            self.last_probe = {
                "status": "error",
                "last_checked_at": utc_now(),
                "error_code": exc.code,
            }
        else:
            self.last_probe = {
                "status": "ok",
                "last_checked_at": utc_now(),
                "error_code": None,
            }

    async def recover(self) -> None:
        self.recovery_complete = False
        stale = (
            datetime.now(UTC) - timedelta(seconds=self.settings.stale_upload_seconds)
        ).isoformat().replace("+00:00", "Z")
        tasks = self.database.pending_tasks(stale)
        for task in tasks:
            try:
                record = self.database.storage_by_id(task["storage_config_id"])
                if record is None:
                    raise ProviderError(
                        "RECOVERY_PENDING", "任务对应的存储配置不存在", uncertain=True
                    )
                provider = self.provider_for_record(record)
                result = await anyio.to_thread.run_sync(
                    provider.head_object, task["object_key"]
                )
                if result is None:
                    self.database.update_task(
                        task["id"],
                        status="failed",
                        error_code="SERVICE_RESTARTED_OBJECT_NOT_FOUND",
                        finished_at=utc_now(),
                    )
                else:
                    self.database.update_task(
                        task["id"],
                        status="succeeded",
                        size_bytes=result.get("size_bytes"),
                        error_code=None,
                        finished_at=utc_now(),
                    )
            except Exception:
                self.database.update_task(
                    task["id"], status="unknown", error_code="RECOVERY_PENDING"
                )
        self.recovery_complete = True

    def ready_checks(self) -> tuple[bool, dict[str, Any], str]:
        snapshot = self.active_snapshot()
        try:
            self.database.check_writable()
            database = {"status": "ok"}
        except Exception:
            database = {"status": "error"}
        try:
            free = shutil.disk_usage(self.settings.temp_dir).free
            writable = os.access(self.settings.temp_dir, os.W_OK)
        except OSError:
            free, writable = 0, False
        temp_ok = writable and free >= self.settings.temp_min_free_bytes
        storage = dict(self.last_probe)
        last_checked = storage.get("last_checked_at")
        try:
            age_seconds = max(
                0,
                round(
                    (
                        datetime.now(UTC)
                        - datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
                    ).total_seconds()
                ),
            )
        except (AttributeError, TypeError, ValueError):
            age_seconds = None
        storage["age_seconds"] = age_seconds
        probe_ok = (
            storage["status"] == "ok"
            and age_seconds is not None
            and age_seconds <= self.settings.storage_probe_max_age_seconds
        )
        if storage["status"] == "ok" and not probe_ok:
            storage["status"] = "degraded"
            storage["error_code"] = "STORAGE_PROBE_STALE"
        checks = {
            "config": {
                "status": "ok" if snapshot else "error",
                "configured": snapshot is not None,
                "provider": snapshot[0]["provider"] if snapshot else None,
                "provider_schema_version": snapshot[0]["provider_schema_version"]
                if snapshot
                else None,
                "revision": snapshot[0]["revision"] if snapshot else 0,
            },
            "database": database,
            "temp_dir": {
                "status": "ok" if temp_ok else "error",
                "free_bytes": free,
                "required_free_bytes": self.settings.temp_min_free_bytes,
            },
            "schema": {"status": "ok" if self.schema_ready else "pending"},
            "recovery": {
                "status": "ok" if self.recovery_complete else "pending",
                "completed": self.recovery_complete,
                "pending_tasks": len(self.database.pending_tasks()),
            },
            "storage": storage,
        }
        ready = (
            snapshot is not None
            and database["status"] == "ok"
            and temp_ok
            and self.schema_ready
            and self.recovery_complete
            and probe_ok
        )
        code = "STORAGE_NOT_CONFIGURED" if snapshot is None else "NOT_READY"
        return ready, checks, code

    async def _probe_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.storage_probe_interval_seconds)
            await self.probe()

    async def _recovery_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.recovery_retry_seconds)
            await self.recover()

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            await anyio.to_thread.run_sync(
                self.database.maintain,
                self.settings.task_retention_days,
                self.settings.log_retention_days,
                self.settings.log_max_rows,
            )
            await asyncio.sleep(86_400)
