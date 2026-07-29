from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_configs (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL UNIQUE CHECK (revision >= 1),
    provider TEXT NOT NULL,
    provider_schema_version INTEGER NOT NULL CHECK (provider_schema_version >= 1),
    config_json TEXT NOT NULL,
    credentials_ciphertext BLOB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    last_tested_at TEXT NOT NULL,
    last_test_latency_ms INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_configs_active
ON storage_configs(status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_storage_configs_revision
ON storage_configs(revision DESC);

CREATE TABLE IF NOT EXISTS upload_tasks (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    idempotency_key TEXT,
    storage_config_id TEXT NOT NULL REFERENCES storage_configs(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    public_url TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('uploading', 'unknown', 'succeeded', 'failed')
    ),
    size_bytes INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_upload_tasks_idempotency_key
ON upload_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_upload_tasks_created_at_id
ON upload_tasks(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_upload_tasks_status_created_at
ON upload_tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_upload_tasks_request_id
ON upload_tasks(request_id);
CREATE INDEX IF NOT EXISTS idx_upload_tasks_storage_config_id
ON upload_tasks(storage_config_id);

CREATE TABLE IF NOT EXISTS service_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level_no INTEGER NOT NULL,
    level_name TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    request_id TEXT,
    task_id TEXT,
    error_code TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_service_logs_created_at_id
ON service_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_service_logs_level_created_at
ON service_logs(level_no, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_service_logs_request_id
ON service_logs(request_id);
CREATE INDEX IF NOT EXISTS idx_service_logs_task_id
ON service_logs(task_id);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path, busy_timeout_ms: int = 5_000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1_000, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"unsupported database schema version: {version}")
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def active_storage(self) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM storage_configs WHERE status='active'"
            ).fetchone()
        return dict(row) if row else None

    def storage_by_id(self, config_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM storage_configs WHERE id=?", (config_id,)
            ).fetchone()
        return dict(row) if row else None

    def activate_storage(self, record: dict[str, Any], expected_revision: int) -> dict:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT revision FROM storage_configs WHERE status='active'"
            ).fetchone()
            current_revision = current["revision"] if current else 0
            if current_revision != expected_revision:
                raise RevisionConflict(current_revision)
            revision = current_revision + 1
            connection.execute(
                "UPDATE storage_configs SET status='inactive' WHERE status='active'"
            )
            connection.execute(
                """
                INSERT INTO storage_configs (
                    id, revision, provider, provider_schema_version, config_json,
                    credentials_ciphertext, status, created_at, activated_at,
                    last_tested_at, last_test_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    revision,
                    record["provider"],
                    record["provider_schema_version"],
                    record["config_json"],
                    record["credentials_ciphertext"],
                    record["created_at"],
                    record["activated_at"],
                    record["last_tested_at"],
                    record.get("last_test_latency_ms"),
                ),
            )
        record.update(revision=revision, status="active")
        return record

    def create_task(self, record: dict[str, Any]) -> None:
        columns = (
            "id",
            "request_id",
            "idempotency_key",
            "storage_config_id",
            "filename",
            "content_type",
            "object_key",
            "public_url",
            "status",
            "size_bytes",
            "error_code",
            "created_at",
            "finished_at",
            "duration_ms",
        )
        with self.transaction() as connection:
            connection.execute(
                f"INSERT INTO upload_tasks ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(record.get(column) for column in columns),
            )

    def update_task(self, task_id: str, **changes: Any) -> None:
        allowed = {
            "public_url",
            "status",
            "size_bytes",
            "error_code",
            "finished_at",
            "duration_ms",
        }
        if not changes or not set(changes) <= allowed:
            raise ValueError("invalid task update")
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE upload_tasks SET {assignments} WHERE id=?",
                (*changes.values(), task_id),
            )

    def task_by_id(self, task_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT t.*, s.provider AS storage_provider,
                       s.revision AS storage_config_revision
                FROM upload_tasks t
                JOIN storage_configs s ON s.id=t.storage_config_id
                WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def task_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM upload_tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> list[dict[str, Any]]:
        where, values = [], []
        if status:
            where.append("t.status=?")
            values.append(status)
        if from_time:
            where.append("t.created_at>=?")
            values.append(from_time)
        if to_time:
            where.append("t.created_at<?")
            values.append(to_time)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT t.*, s.provider AS storage_provider,
                       s.revision AS storage_config_revision
                FROM upload_tasks t
                JOIN storage_configs s ON s.id=t.storage_config_id
                {clause}
                ORDER BY t.created_at DESC, t.id DESC
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_tasks(self, stale_before: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM upload_tasks WHERE status='unknown'"
        values: tuple[Any, ...] = ()
        if stale_before:
            query += " OR (status='uploading' AND created_at<?)"
            values = (stale_before,)
        with closing(self.connect()) as connection:
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def write_log(self, record: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO service_logs (
                    created_at, level_no, level_name, event, message,
                    request_id, task_id, error_code, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["created_at"],
                    record["level_no"],
                    record["level_name"],
                    record["event"],
                    record["message"],
                    record.get("request_id"),
                    record.get("task_id"),
                    record.get("error_code"),
                    json.dumps(
                        record.get("details"), ensure_ascii=False, separators=(",", ":")
                    )
                    if record.get("details") is not None
                    else None,
                ),
            )

    def list_logs(
        self,
        *,
        min_level: int,
        limit: int,
        before_id: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where, values = ["level_no>=?"], [min_level]
        if before_id is not None:
            where.append("id<?")
            values.append(before_id)
        for key, value in (filters or {}).items():
            if value is not None:
                where.append(f"{key}=?")
                values.append(value)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM service_logs
                WHERE {' AND '.join(where)}
                ORDER BY id DESC LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            details_json = item.pop("details_json")
            item["details"] = json.loads(details_json) if details_json else None
            items.append(item)
        return items

    def check_writable(self) -> None:
        with self.transaction() as connection:
            connection.execute("CREATE TEMP TABLE IF NOT EXISTS healthcheck (value INT)")

    def maintain(
        self, task_retention_days: int, log_retention_days: int, log_max_rows: int
    ) -> None:
        now = datetime.now(UTC)
        task_cutoff = (now - timedelta(days=task_retention_days)).isoformat()
        log_cutoff = (now - timedelta(days=log_retention_days)).isoformat()
        with self.transaction() as connection:
            connection.execute(
                """
                DELETE FROM upload_tasks
                WHERE status IN ('succeeded','failed') AND created_at<?
                """,
                (task_cutoff,),
            )
            connection.execute(
                "DELETE FROM service_logs WHERE created_at<?", (log_cutoff,)
            )
            connection.execute(
                """
                DELETE FROM service_logs WHERE id IN (
                    SELECT id FROM service_logs ORDER BY id DESC LIMIT -1 OFFSET ?
                )
                """,
                (log_max_rows,),
            )
            connection.execute(
                """
                DELETE FROM storage_configs
                WHERE status='inactive'
                AND activated_at<?
                AND id NOT IN (SELECT DISTINCT storage_config_id FROM upload_tasks)
                """,
                (task_cutoff,),
            )
            connection.execute("PRAGMA optimize")


class RevisionConflict(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
