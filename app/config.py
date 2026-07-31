from __future__ import annotations

import os
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


@dataclass(frozen=True)
class Settings:
    encryption_key: str
    database_path: Path = Path("/data/zos-upload.db")
    temp_dir: Path = Path("/data/tmp")
    app_timezone: str = "Asia/Shanghai"
    max_upload_bytes: int = 209_715_200
    max_request_body_bytes: int = 213_909_504
    max_concurrent_uploads: int = 4
    upload_read_chunk_bytes: int = 1_048_576
    upload_spool_threshold_bytes: int = 8_388_608
    temp_min_free_bytes: int = 1_073_741_824
    sqlite_busy_timeout_ms: int = 5_000
    request_timeout_seconds: int = 600
    recovery_retry_seconds: int = 60
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
    dashboard_enabled: bool = True
    bootstrap_storage_from_env: bool = True

    def __post_init__(self) -> None:
        if not self.encryption_key:
            raise ValueError("SETTINGS_ENCRYPTION_KEY is required")
        if self.max_request_body_bytes <= self.max_upload_bytes:
            raise ValueError("MAX_REQUEST_BODY_BYTES must exceed MAX_UPLOAD_BYTES")
        ZoneInfo(self.app_timezone)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            encryption_key=os.getenv("SETTINGS_ENCRYPTION_KEY", ""),
            database_path=Path(os.getenv("DATABASE_PATH", "/data/zos-upload.db")),
            temp_dir=Path(os.getenv("TEMP_DIR", "/data/tmp")),
            app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            max_upload_bytes=_integer("MAX_UPLOAD_BYTES", 209_715_200, 1),
            max_request_body_bytes=_integer("MAX_REQUEST_BODY_BYTES", 213_909_504, 1),
            max_concurrent_uploads=_integer("MAX_CONCURRENT_UPLOADS", 4, 1),
            upload_read_chunk_bytes=_integer("UPLOAD_READ_CHUNK_BYTES", 1_048_576, 1),
            upload_spool_threshold_bytes=_integer(
                "UPLOAD_SPOOL_THRESHOLD_BYTES", 8_388_608, 1
            ),
            temp_min_free_bytes=_integer("TEMP_MIN_FREE_BYTES", 1_073_741_824),
            sqlite_busy_timeout_ms=_integer("SQLITE_BUSY_TIMEOUT_MS", 5_000, 1),
            request_timeout_seconds=_integer("REQUEST_TIMEOUT_SECONDS", 600, 1),
            recovery_retry_seconds=_integer("RECOVERY_RETRY_SECONDS", 60, 1),
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
            s3_transfer_max_concurrency=_integer(
                "S3_TRANSFER_MAX_CONCURRENCY", 2, 1
            ),
            dashboard_enabled=_boolean("DASHBOARD_ENABLED", True),
            bootstrap_storage_from_env=_boolean("BOOTSTRAP_STORAGE_FROM_ENV", True),
        )
