from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _integer(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    )


def _client_keys(name: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for item in _csv(name):
        client_id, separator, key = item.partition(":")
        if (
            not separator
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", client_id)
            or len(key) < 32
        ):
            raise ValueError(
                f"{name} must contain client_id:secret pairs with 32+ character secrets"
            )
        pairs.append((client_id, key))
    if len({client_id for client_id, _ in pairs}) != len(pairs):
        raise ValueError(f"{name} client IDs must be unique")
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    encryption_key: str
    admin_api_keys: tuple[str, ...] = ()
    client_api_keys: tuple[tuple[str, str], ...] = ()
    storage_endpoint_allowlist: tuple[str, ...] = (".zos.ctyun.cn",)
    allow_insecure_storage_http: bool = False
    database_path: Path = Path("/data/db/zos-upload.db")
    temp_dir: Path = Path("/data/tmp")
    app_timezone: str = "Asia/Shanghai"
    max_upload_bytes: int = 209_715_200
    max_request_body_bytes: int = 213_909_504
    max_concurrent_uploads: int = 4
    upload_rate_limit_per_minute: int = 60
    client_max_objects: int = 10_000
    client_max_bytes: int = 1_099_511_627_776
    temp_min_free_bytes: int = 1_073_741_824
    sqlite_busy_timeout_ms: int = 5_000
    recovery_retry_seconds: int = 60
    recovery_initial_budget_seconds: int = 5
    recovery_batch_size: int = 25
    recovery_max_concurrency: int = 4
    recovery_connect_timeout_seconds: int = 3
    recovery_read_timeout_seconds: int = 10
    recovery_max_attempts: int = 1
    stale_upload_seconds: int = 900
    stale_delete_seconds: int = 900
    storage_probe_interval_seconds: int = 30
    storage_probe_max_age_seconds: int = 60
    task_retention_days: int = 180
    log_retention_days: int = 30
    log_max_rows: int = 100_000
    s3_multipart_threshold_bytes: int = 16_777_216
    s3_multipart_chunk_bytes: int = 16_777_216
    s3_transfer_max_concurrency: int = 2
    provider_cache_max_entries: int = 128
    dashboard_enabled: bool = True
    bootstrap_storage_from_env: bool = True

    def __post_init__(self) -> None:
        if not self.encryption_key:
            raise ValueError("SETTINGS_ENCRYPTION_KEY is required")
        if not self.admin_api_keys:
            raise ValueError("ADMIN_API_KEYS must contain at least one key")
        if len(set(self.admin_api_keys)) != len(self.admin_api_keys) or any(
            len(key) < 32 for key in self.admin_api_keys
        ):
            raise ValueError(
                "ADMIN_API_KEYS must contain unique keys of at least 32 characters"
            )
        if len({item[0] for item in self.client_api_keys}) != len(
            self.client_api_keys
        ) or any(
            len(key) < 32
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", client_id)
            for client_id, key in self.client_api_keys
        ):
            raise ValueError("CLIENT_API_KEYS contains invalid or duplicate entries")
        if not self.storage_endpoint_allowlist:
            raise ValueError("STORAGE_ENDPOINT_ALLOWLIST must not be empty")
        if self.max_request_body_bytes <= self.max_upload_bytes:
            raise ValueError("MAX_REQUEST_BODY_BYTES must exceed MAX_UPLOAD_BYTES")
        ZoneInfo(self.app_timezone)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            encryption_key=os.getenv("SETTINGS_ENCRYPTION_KEY", ""),
            admin_api_keys=_csv("ADMIN_API_KEYS"),
            client_api_keys=_client_keys("CLIENT_API_KEYS"),
            storage_endpoint_allowlist=_csv(
                "STORAGE_ENDPOINT_ALLOWLIST", ".zos.ctyun.cn"
            ),
            allow_insecure_storage_http=_boolean("ALLOW_INSECURE_STORAGE_HTTP", False),
            database_path=Path(os.getenv("DATABASE_PATH", "/data/db/zos-upload.db")),
            temp_dir=Path(os.getenv("TEMP_DIR", "/data/tmp")),
            app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            max_upload_bytes=_integer("MAX_UPLOAD_BYTES", 209_715_200, 1),
            max_request_body_bytes=_integer("MAX_REQUEST_BODY_BYTES", 213_909_504, 1),
            max_concurrent_uploads=_integer("MAX_CONCURRENT_UPLOADS", 4, 1),
            upload_rate_limit_per_minute=_integer(
                "UPLOAD_RATE_LIMIT_PER_MINUTE", 60, 1
            ),
            client_max_objects=_integer("CLIENT_MAX_OBJECTS", 10_000),
            client_max_bytes=_integer("CLIENT_MAX_BYTES", 1_099_511_627_776),
            temp_min_free_bytes=_integer("TEMP_MIN_FREE_BYTES", 1_073_741_824),
            sqlite_busy_timeout_ms=_integer("SQLITE_BUSY_TIMEOUT_MS", 5_000, 1),
            recovery_retry_seconds=_integer("RECOVERY_RETRY_SECONDS", 60, 1),
            recovery_initial_budget_seconds=_integer(
                "RECOVERY_INITIAL_BUDGET_SECONDS", 5, 1
            ),
            recovery_batch_size=_integer("RECOVERY_BATCH_SIZE", 25, 1),
            recovery_max_concurrency=_integer("RECOVERY_MAX_CONCURRENCY", 4, 1),
            recovery_connect_timeout_seconds=_integer(
                "RECOVERY_CONNECT_TIMEOUT_SECONDS", 3, 1
            ),
            recovery_read_timeout_seconds=_integer(
                "RECOVERY_READ_TIMEOUT_SECONDS", 10, 1
            ),
            recovery_max_attempts=_integer("RECOVERY_MAX_ATTEMPTS", 1, 1),
            stale_upload_seconds=_integer("STALE_UPLOAD_SECONDS", 900, 1),
            stale_delete_seconds=_integer("STALE_DELETE_SECONDS", 900, 1),
            storage_probe_interval_seconds=_integer(
                "STORAGE_PROBE_INTERVAL_SECONDS", 30, 1
            ),
            storage_probe_max_age_seconds=_integer(
                "STORAGE_PROBE_MAX_AGE_SECONDS", 60, 1
            ),
            task_retention_days=_integer("TASK_RETENTION_DAYS", 180, 1),
            log_retention_days=_integer("LOG_RETENTION_DAYS", 30, 1),
            log_max_rows=_integer("LOG_MAX_ROWS", 100_000, 1),
            s3_multipart_threshold_bytes=_integer(
                "S3_MULTIPART_THRESHOLD_BYTES", 16_777_216, 5_242_880
            ),
            s3_multipart_chunk_bytes=_integer(
                "S3_MULTIPART_CHUNK_BYTES", 16_777_216, 5_242_880
            ),
            s3_transfer_max_concurrency=_integer("S3_TRANSFER_MAX_CONCURRENCY", 2, 1),
            provider_cache_max_entries=_integer("PROVIDER_CACHE_MAX_ENTRIES", 128, 1),
            dashboard_enabled=_boolean("DASHBOARD_ENABLED", True),
            bootstrap_storage_from_env=_boolean("BOOTSTRAP_STORAGE_FROM_ENV", True),
        )
