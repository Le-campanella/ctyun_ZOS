from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import anyio

from .config import Settings
from .database import (
    SCHEMA_VERSION,
    Database,
    PresetNotFound,
    RevisionConflict,
    utc_now,
)
from .eventlog import EventLogger, NOTIFY
from .providers import (
    ProviderError,
    ProviderRegistry,
    StorageProvider,
    matches_object_metadata,
    require_upload_metadata,
)
from .security import CredentialCipher


@dataclass(frozen=True)
class StorageSnapshot:
    preset_id: str
    preset_key: str
    enabled: bool
    is_default: bool
    storage_config_id: str
    revision: int
    provider_id: str
    provider_schema_version: int
    provider: StorageProvider


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
        self._snapshots_by_key: dict[str, StorageSnapshot] = {}
        self._providers_by_config_id: dict[str, StorageProvider] = {}
        self._default_preset_key: str | None = None
        self.schema_ready = False
        self.recovery_complete = False
        self.last_probe: dict[str, Any] = {
            "status": "pending",
            "last_checked_at": None,
            "error_code": None,
        }
        self._stop = asyncio.Event()
        self._background: list[asyncio.Task] = []
        self.background_enabled = False
        self.background_status = {
            name: {
                "status": "pending",
                "last_success_at": None,
                "last_error_at": None,
                "error_code": None,
            }
            for name in ("probe", "recovery", "maintenance")
        }

    async def start(self, background: bool = True) -> None:
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        migration_backup = self.database.initialize()
        self.schema_ready = True
        self.log.emit(
            NOTIFY,
            "database_schema_ready",
            "数据库 schema 已就绪",
            details={
                "schema_version": SCHEMA_VERSION,
                "migration_backup_created": migration_backup is not None,
                "migration_backup_name": migration_backup.name
                if migration_backup
                else None,
            },
        )
        await self._bootstrap_storage()
        self._load_active()
        await self.recover()
        self._background_succeeded("recovery")
        await self.probe()
        self._background_succeeded("probe")
        self.log.emit(NOTIFY, "service_started", "服务已启动")
        if background:
            self.background_enabled = True
            self._background = [
                asyncio.create_task(
                    self._supervise(
                        "probe",
                        self.probe,
                        self.settings.storage_probe_interval_seconds,
                    ),
                    name="storage-probe",
                ),
                asyncio.create_task(
                    self._supervise(
                        "recovery",
                        self.recover,
                        self.settings.recovery_retry_seconds,
                    ),
                    name="task-recovery",
                ),
                asyncio.create_task(
                    self._supervise("maintenance", self._maintain, 86_400),
                    name="database-maintenance",
                ),
            ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._background:
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def _load_active(self) -> None:
        snapshots = {
            record["preset_key"]: self._snapshot(
                record, self.provider_for_record(record)
            )
            for record in self.database.active_storages()
        }
        default = next(
            (key for key, snapshot in snapshots.items() if snapshot.is_default),
            None,
        )
        with self._active_lock:
            self._snapshots_by_key = snapshots
            self._default_preset_key = default

    @staticmethod
    def _snapshot(
        record: dict[str, Any], provider: StorageProvider
    ) -> StorageSnapshot:
        return StorageSnapshot(
            preset_id=record["preset_id"],
            preset_key=record["preset_key"],
            enabled=bool(record["enabled"]),
            is_default=bool(record["is_default"]),
            storage_config_id=record["id"],
            revision=record["revision"],
            provider_id=record["provider"],
            provider_schema_version=record["provider_schema_version"],
            provider=provider,
        )

    def _install_snapshot(
        self, record: dict[str, Any], provider: StorageProvider
    ) -> StorageSnapshot:
        snapshot = self._snapshot(record, provider)
        with self._active_lock:
            self._providers_by_config_id[record["id"]] = provider
            self._snapshots_by_key = self._snapshots_by_key | {
                snapshot.preset_key: snapshot
            }
            if snapshot.is_default:
                self._default_preset_key = snapshot.preset_key
        return snapshot

    def provider_for_record(self, record: dict[str, Any]) -> StorageProvider:
        with self._active_lock:
            cached = self._providers_by_config_id.get(record["id"])
        if cached is not None:
            return cached
        provider_type = self.registry.get(
            record["provider"], record["provider_schema_version"]
        )
        provider = provider_type(
            json.loads(record["config_json"]),
            self.cipher.decrypt(record["credentials_ciphertext"]),
            self.settings,
        )
        with self._active_lock:
            self._providers_by_config_id[record["id"]] = provider
        return provider

    def provider_for_config(self, config_id: str) -> StorageProvider:
        with self._active_lock:
            cached = self._providers_by_config_id.get(config_id)
        if cached is not None:
            return cached
        record = self.database.storage_by_id(config_id)
        if record is None:
            raise ProviderError(
                "RECOVERY_PENDING", "任务对应的存储配置不存在", uncertain=True
            )
        return self.provider_for_record(record)

    def active_snapshot(self, preset_key: str | None = None) -> StorageSnapshot | None:
        with self._active_lock:
            key = preset_key if preset_key is not None else self._default_preset_key
            return self._snapshots_by_key.get(key) if key is not None else None

    def resolve_upload_snapshot(
        self, preset_key: str | None
    ) -> StorageSnapshot:
        if preset_key is None:
            snapshot = self.active_snapshot()
            if snapshot is None:
                raise ProviderError(
                    "STORAGE_DEFAULT_NOT_CONFIGURED",
                    "默认存储预设尚未配置",
                )
            return snapshot
        preset = self.database.storage_preset_by_key(preset_key)
        if preset is None:
            raise ProviderError("STORAGE_PRESET_NOT_FOUND", "存储预设不存在")
        if not preset["enabled"]:
            raise ProviderError("STORAGE_PRESET_DISABLED", "存储预设已禁用")
        snapshot = self.active_snapshot(preset_key)
        if snapshot is None:
            raise ProviderError("STORAGE_NOT_CONFIGURED", "存储预设尚未配置")
        return snapshot

    def snapshots(self) -> dict[str, StorageSnapshot]:
        with self._active_lock:
            return dict(self._snapshots_by_key)

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

    def current_storage(self, preset_key: str | None = None) -> dict[str, Any]:
        record = self.database.active_storage(preset_key)
        if record is None:
            return {
                "configured": False,
                "preset_key": None,
                "display_name": None,
                "enabled": None,
                "is_default": None,
                "state_revision": None,
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
            "preset_key": record["preset_key"],
            "display_name": record["display_name"],
            "enabled": bool(record["enabled"]),
            "is_default": bool(record["is_default"]),
            "state_revision": record["state_revision"],
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

    def storage_presets(self) -> list[dict[str, Any]]:
        items = []
        for preset in self.database.list_storage_presets():
            config_record = self.database.active_storage(preset["preset_key"])
            config = (
                json.loads(config_record["config_json"]) if config_record else {}
            )
            items.append(
                {
                    "preset_key": preset["preset_key"],
                    "display_name": preset["display_name"],
                    "enabled": bool(preset["enabled"]),
                    "is_default": bool(preset["is_default"]),
                    "state_revision": preset["state_revision"],
                    "provider": preset["provider"],
                    "provider_schema_version": preset["provider_schema_version"],
                    "config_revision": preset["revision"],
                    "endpoint_host": urlsplit(config.get("endpoint_url", "")).hostname,
                    "bucket": config.get("bucket"),
                    "last_connection_test": {
                        "status": "ok",
                        "tested_at": preset["last_tested_at"],
                        "latency_ms": preset["last_test_latency_ms"],
                    }
                    if preset["last_tested_at"]
                    else None,
                    "created_at": preset["created_at"],
                    "updated_at": preset["updated_at"],
                }
            )
        return items

    def storage_preset_detail(self, preset_key: str) -> dict[str, Any]:
        preset = self.database.storage_preset_by_key(preset_key)
        if preset is None:
            raise PresetNotFound(preset_key)
        return self.current_storage(preset_key) | {
            "preset_key": preset["preset_key"],
            "display_name": preset["display_name"],
            "enabled": bool(preset["enabled"]),
            "is_default": bool(preset["is_default"]),
            "state_revision": preset["state_revision"],
            "created_at": preset["created_at"],
            "updated_at": preset["updated_at"],
        }

    def _candidate(
        self,
        payload: dict[str, Any],
        preset_key: str | None = None,
        *,
        inherit_credentials: bool = True,
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
        active = (
            self.database.active_storage(preset_key)
            if inherit_credentials
            else None
        )
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

    async def test_storage(
        self, payload: dict[str, Any], preset_key: str | None = None
    ) -> dict[str, Any]:
        if (
            preset_key is not None
            and self.database.storage_preset_by_key(preset_key) is None
        ):
            raise PresetNotFound(preset_key)
        provider_id, schema_version, _, _, provider = self._candidate(
            payload, preset_key
        )
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

    async def create_storage_preset(
        self, preset_key: str, display_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        provider_id, schema_version, config, credentials, provider = self._candidate(
            payload, inherit_credentials=False
        )
        latency = await anyio.to_thread.run_sync(provider.test_connection)
        now = utc_now()
        record = self.database.create_storage_preset(
            {
                "id": str(uuid4()),
                "preset_key": preset_key,
                "display_name": display_name,
                "created_at": now,
                "updated_at": now,
            },
            self._storage_record(
                provider_id,
                schema_version,
                config,
                credentials,
                latency,
                now,
            ),
        )
        self._install_snapshot(record, provider)
        self.log.emit(
            NOTIFY,
            "storage_preset_created",
            "存储预设已创建",
            details={"preset_key": preset_key, "provider": provider_id, "revision": 1},
        )
        return self.storage_preset_detail(preset_key)

    def _storage_record(
        self,
        provider_id: str,
        schema_version: int,
        config: dict[str, Any],
        credentials: dict[str, Any],
        latency: int,
        now: str,
    ) -> dict[str, Any]:
        return {
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
        }

    async def activate_storage(
        self, payload: dict[str, Any], preset_key: str | None = None
    ) -> dict[str, Any]:
        if (
            preset_key is not None
            and self.database.storage_preset_by_key(preset_key) is None
        ):
            raise PresetNotFound(preset_key)
        expected = payload.get("expected_revision")
        if not isinstance(expected, int) or expected < 0:
            raise ProviderError(
                "STORAGE_CONFIG_INVALID", "expected_revision 不合法"
            )
        active = self.database.active_storage(preset_key)
        current_revision = active["revision"] if active else 0
        if expected != current_revision:
            raise RevisionConflict(current_revision)
        provider_id, schema_version, config, credentials, provider = self._candidate(
            payload, preset_key
        )
        latency = await anyio.to_thread.run_sync(provider.test_connection)
        now = utc_now()
        record = self.database.activate_storage(
            self._storage_record(
                provider_id,
                schema_version,
                config,
                credentials,
                latency,
                now,
            ),
            expected,
            preset_key,
        )
        snapshot = self._install_snapshot(record, provider)
        if snapshot.is_default:
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
                "preset_key": record["preset_key"],
                "revision": record["revision"],
            },
        )
        response = self.current_storage(record["preset_key"])
        response["previous_revision"] = expected
        return response

    def update_storage_preset(
        self,
        preset_key: str,
        expected_state_revision: int,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        preset = self.database.update_storage_preset(
            preset_key,
            expected_state_revision,
            display_name=display_name,
            enabled=enabled,
        )
        self._load_active()
        self.log.emit(
            NOTIFY,
            "preset_enabled"
            if enabled is True
            else "preset_disabled"
            if enabled is False
            else "preset_updated",
            "存储预设状态已更新",
            details={"preset_key": preset_key},
        )
        return preset

    async def set_default_storage_preset(
        self,
        preset_key: str,
        expected_default_preset: str,
        expected_state_revision: int,
    ) -> dict[str, Any]:
        preset = self.database.set_default_storage_preset(
            preset_key, expected_default_preset, expected_state_revision
        )
        self._load_active()
        await self.probe()
        self.log.emit(
            NOTIFY,
            "default_preset_changed",
            "默认存储预设已切换",
            details={"preset_key": preset_key},
        )
        return preset

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
            await anyio.to_thread.run_sync(snapshot.provider.test_connection)
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
        stale_upload = (
            datetime.now(UTC) - timedelta(seconds=self.settings.stale_upload_seconds)
        ).isoformat().replace("+00:00", "Z")
        tasks = self.database.pending_tasks(stale_upload)
        for task in tasks:
            try:
                provider = self.provider_for_config(task["storage_config_id"])
                metadata = await anyio.to_thread.run_sync(
                    provider.head_object, task["object_key"]
                )
                if metadata is None:
                    self.database.update_task(
                        task["id"],
                        status="failed",
                        object_status="absent",
                        error_code="SERVICE_RESTARTED_OBJECT_NOT_FOUND",
                        finished_at=utc_now(),
                    )
                else:
                    if task["size_bytes"] is not None:
                        require_upload_metadata(metadata, task["size_bytes"])
                    object_status = (
                        "present"
                        if task.get("delete_token_hash") is not None
                        else "present_unclaimed"
                    )
                    self.database.update_task(
                        task["id"],
                        status="succeeded",
                        size_bytes=metadata.size_bytes,
                        etag=metadata.etag,
                        version_id=metadata.version_id,
                        object_status=object_status,
                        error_code=None,
                        finished_at=utc_now(),
                    )
            except ProviderError as exc:
                self.database.update_task(
                    task["id"],
                    status="unknown",
                    object_status="pending",
                    error_code=exc.code
                    if exc.code == "OBJECT_SIZE_MISMATCH"
                    else "RECOVERY_PENDING",
                )
            except Exception:
                self.database.update_task(
                    task["id"],
                    status="unknown",
                    object_status="pending",
                    error_code="RECOVERY_PENDING",
                )
        stale_delete = (
            datetime.now(UTC) - timedelta(seconds=self.settings.stale_delete_seconds)
        ).isoformat().replace("+00:00", "Z")
        for task in self.database.pending_deletions(stale_delete):
            await self._recover_deletion(task)
        self.recovery_complete = True

    async def _recover_deletion(self, task: dict[str, Any]) -> None:
        from_status = task["object_status"]
        try:
            provider = self.provider_for_config(task["storage_config_id"])
            metadata = await anyio.to_thread.run_sync(
                provider.head_object, task["object_key"], task["version_id"]
            )
        except ProviderError as exc:
            to_status = "delete_unknown"
            error_code = (
                "STORAGE_CONFIG_UNAVAILABLE"
                if exc.code == "RECOVERY_PENDING"
                else "DELETE_PENDING"
            )
            provider_result = exc.code
        except Exception as exc:
            to_status = "delete_unknown"
            error_code = "STORAGE_CONFIG_UNAVAILABLE"
            provider_result = type(exc).__name__
        else:
            if metadata is None:
                to_status = "deleted"
                error_code = None
                provider_result = "confirmed_absent"
            elif matches_object_metadata(
                metadata,
                task["size_bytes"],
                task["etag"],
                task["version_id"],
            ):
                to_status = "present"
                error_code = "DELETE_FAILED"
                provider_result = "original_present"
            else:
                to_status = "present"
                error_code = "OBJECT_CHANGED"
                provider_result = "metadata_mismatch"
        if (
            task["object_status"] == to_status
            and task["delete_error_code"] == error_code
        ):
            return
        changes: dict[str, Any] = {
            "object_status": to_status,
            "delete_error_code": error_code,
        }
        if to_status == "deleted":
            changes["deleted_at"] = utc_now()
        self.database.update_task(task["id"], **changes)
        self.log.emit(
            NOTIFY,
            "object_delete_recovered"
            if to_status in {"deleted", "present"}
            else "object_delete_recovery_pending",
            "对象删除恢复状态已更新",
            request_id=task["delete_request_id"],
            task_id=task["id"],
            error_code=error_code,
            details={
                "from_status": from_status,
                "to_status": to_status,
                "provider_result": provider_result,
                "object_key": task["object_key"],
                "size_bytes": task["size_bytes"],
                "etag": task["etag"],
                "version_id": task["version_id"],
                "storage_config_id": task["storage_config_id"],
            },
        )

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
                "preset_key": snapshot.preset_key if snapshot else None,
                "provider": snapshot.provider_id if snapshot else None,
                "provider_schema_version": snapshot.provider_schema_version
                if snapshot
                else None,
                "revision": snapshot.revision if snapshot else 0,
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
                "pending_tasks": len(self.database.pending_tasks())
                + len(self.database.pending_deletions(utc_now())),
            },
            "storage": storage,
        }
        if self.background_enabled:
            checks["background"] = self.background_status
        background_ok = not self.background_enabled or all(
            item["status"] == "ok" for item in self.background_status.values()
        )
        ready = (
            snapshot is not None
            and database["status"] == "ok"
            and temp_ok
            and self.schema_ready
            and self.recovery_complete
            and probe_ok
            and background_ok
        )
        code = "STORAGE_NOT_CONFIGURED" if snapshot is None else "NOT_READY"
        return ready, checks, code

    def _background_succeeded(self, name: str) -> None:
        self.background_status[name].update(
            status="ok",
            last_success_at=utc_now(),
            error_code=None,
        )

    async def _maintain(self) -> None:
        await anyio.to_thread.run_sync(
            self.database.maintain,
            self.settings.task_retention_days,
            self.settings.log_retention_days,
            self.settings.log_max_rows,
        )

    async def _supervise(self, name: str, operation: Any, interval: float) -> None:
        while not self._stop.is_set():
            delay = interval
            try:
                await operation()
                self._background_succeeded(name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.background_status[name].update(
                    status="error",
                    last_error_at=utc_now(),
                    error_code="BACKGROUND_TASK_FAILED",
                )
                self.log.emit(
                    logging.CRITICAL,
                    "background_task_failed",
                    f"后台任务 {name} 执行失败，将自动重试",
                    error_code="BACKGROUND_TASK_FAILED",
                    details={"task": name, "exception": type(exc).__name__},
                )
                delay = min(interval, 60)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
