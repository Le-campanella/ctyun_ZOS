from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


SCHEMA_VERSION = 4
DEFAULT_PRESET_ID = "00000000-0000-0000-0000-000000000001"
SCHEMA_V1 = """
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

PRESET_SCHEMA_V3 = """
CREATE TABLE storage_presets (
    id TEXT PRIMARY KEY,
    preset_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_storage_presets_default
ON storage_presets(is_default) WHERE is_default = 1;
"""

STORAGE_CONFIG_SCHEMA_V3 = """
CREATE TABLE storage_configs (
    id TEXT PRIMARY KEY,
    preset_id TEXT NOT NULL REFERENCES storage_presets(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    provider TEXT NOT NULL,
    provider_schema_version INTEGER NOT NULL CHECK (provider_schema_version >= 1),
    config_json TEXT NOT NULL,
    credentials_ciphertext BLOB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    last_tested_at TEXT NOT NULL,
    last_test_latency_ms INTEGER,
    UNIQUE (preset_id, revision)
);
CREATE UNIQUE INDEX uq_storage_configs_active
ON storage_configs(preset_id) WHERE status = 'active';
CREATE INDEX idx_storage_configs_revision
ON storage_configs(preset_id, revision DESC);
CREATE INDEX idx_storage_configs_provider_revision
ON storage_configs(provider, revision DESC);
"""

UPLOAD_TASK_SCHEMA_V3 = """
CREATE TABLE upload_tasks (
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
    etag TEXT,
    version_id TEXT,
    delete_token_hash BLOB,
    object_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        object_status IN (
            'pending', 'present', 'absent', 'legacy_unverified',
            'deleting', 'deleted', 'delete_unknown'
        )
    ),
    delete_request_id TEXT,
    delete_error_code TEXT,
    delete_started_at TEXT,
    deleted_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE UNIQUE INDEX uq_upload_tasks_idempotency_key
ON upload_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_upload_tasks_created_at_id
ON upload_tasks(created_at DESC, id DESC);
CREATE INDEX idx_upload_tasks_status_created_at
ON upload_tasks(status, created_at DESC);
CREATE INDEX idx_upload_tasks_request_id
ON upload_tasks(request_id);
CREATE INDEX idx_upload_tasks_storage_config_id
ON upload_tasks(storage_config_id);
CREATE INDEX idx_upload_tasks_object_status
ON upload_tasks(object_status, created_at DESC);
"""

SERVICE_LOG_SCHEMA = """
CREATE TABLE service_logs (
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
CREATE INDEX idx_service_logs_created_at_id
ON service_logs(created_at DESC, id DESC);
CREATE INDEX idx_service_logs_level_created_at
ON service_logs(level_no, created_at DESC);
CREATE INDEX idx_service_logs_request_id
ON service_logs(request_id);
CREATE INDEX idx_service_logs_task_id
ON service_logs(task_id);
"""

SCHEMA_V3 = (
    PRESET_SCHEMA_V3
    + STORAGE_CONFIG_SCHEMA_V3
    + UPLOAD_TASK_SCHEMA_V3
    + SERVICE_LOG_SCHEMA
)

UPLOAD_TASK_SCHEMA_V4 = """
CREATE TABLE upload_tasks (
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
    etag TEXT,
    version_id TEXT,
    delete_token_hash BLOB,
    object_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        object_status IN (
            'pending', 'present', 'present_unclaimed', 'absent',
            'legacy_unverified', 'deleting', 'deleted', 'delete_unknown'
        )
    ),
    delete_request_id TEXT,
    delete_error_code TEXT,
    delete_started_at TEXT,
    deleted_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    CHECK (
        (status IN ('uploading', 'unknown') AND object_status='pending')
        OR (status='failed' AND object_status='absent')
        OR (
            status='succeeded' AND object_status IN (
                'present', 'present_unclaimed', 'legacy_unverified',
                'deleting', 'deleted', 'delete_unknown'
            )
        )
    )
);
CREATE UNIQUE INDEX uq_upload_tasks_idempotency_key
ON upload_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_upload_tasks_created_at_id
ON upload_tasks(created_at DESC, id DESC);
CREATE INDEX idx_upload_tasks_status_created_at
ON upload_tasks(status, created_at DESC);
CREATE INDEX idx_upload_tasks_request_id
ON upload_tasks(request_id);
CREATE INDEX idx_upload_tasks_storage_config_id
ON upload_tasks(storage_config_id);
CREATE INDEX idx_upload_tasks_object_status
ON upload_tasks(object_status, created_at DESC);
"""

SCHEMA_V4 = (
    PRESET_SCHEMA_V3
    + STORAGE_CONFIG_SCHEMA_V3
    + UPLOAD_TASK_SCHEMA_V4
    + SERVICE_LOG_SCHEMA
)


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


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
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def initialize(self) -> Path | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise RuntimeError(f"unsupported database schema version: {version}")
            backup = (
                self._backup_before_upgrade(connection)
                if version in (1, 2, 3)
                else None
            )
            if version == 0:
                self._run_migration(connection, 4, self._create_schema_v4)
            elif version == 1:
                self._run_migration(connection, 2, self._migrate_v1_to_v2)
                self._run_migration(
                    connection, 3, self._migrate_v2_to_v3, foreign_keys=False
                )
                self._run_migration(
                    connection, 4, self._migrate_v3_to_v4, foreign_keys=False
                )
            elif version == 2:
                self._run_migration(
                    connection, 3, self._migrate_v2_to_v3, foreign_keys=False
                )
                self._run_migration(
                    connection, 4, self._migrate_v3_to_v4, foreign_keys=False
                )
            elif version == 3:
                self._run_migration(
                    connection, 4, self._migrate_v3_to_v4, foreign_keys=False
                )
            self._verify_schema_v4(connection)
            return backup

    def _run_migration(
        self,
        connection: sqlite3.Connection,
        target_version: int,
        migration,
        *,
        foreign_keys: bool = True,
    ) -> None:
        if not foreign_keys:
            connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            migration(connection)
            connection.execute(f"PRAGMA user_version={target_version}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if not foreign_keys:
                connection.execute("PRAGMA foreign_keys=ON")

    def _create_schema_v4(self, connection: sqlite3.Connection) -> None:
        _execute_statements(connection, SCHEMA_V4)

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        additions = (
            "etag TEXT",
            "version_id TEXT",
            "delete_token_hash BLOB",
            """object_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                object_status IN (
                    'pending', 'present', 'absent', 'legacy_unverified',
                    'deleting', 'deleted', 'delete_unknown'
                )
            )""",
            "delete_request_id TEXT",
            "delete_error_code TEXT",
            "delete_started_at TEXT",
            "deleted_at TEXT",
        )
        for definition in additions:
            connection.execute(f"ALTER TABLE upload_tasks ADD COLUMN {definition}")
        connection.execute(
            """
            UPDATE upload_tasks
            SET object_status = CASE
                WHEN status='succeeded' THEN 'legacy_unverified'
                WHEN status='failed' THEN 'absent'
                ELSE 'pending'
            END
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_upload_tasks_object_status
            ON upload_tasks(object_status, created_at DESC)
            """
        )

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        for index in (
            "uq_storage_configs_active",
            "idx_storage_configs_revision",
            "idx_storage_configs_provider_revision",
            "uq_upload_tasks_idempotency_key",
            "idx_upload_tasks_created_at_id",
            "idx_upload_tasks_status_created_at",
            "idx_upload_tasks_request_id",
            "idx_upload_tasks_storage_config_id",
            "idx_upload_tasks_object_status",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.execute("ALTER TABLE upload_tasks RENAME TO upload_tasks_v2")
        connection.execute("ALTER TABLE storage_configs RENAME TO storage_configs_v2")
        now = utc_now()
        connection.execute(
            """
            CREATE TABLE storage_presets (
                id TEXT PRIMARY KEY,
                preset_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO storage_presets (
                id, preset_key, display_name, enabled, is_default,
                state_revision, created_at, updated_at
            ) VALUES (?, 'default', '默认 ZOS', 1, 1, 1, ?, ?)
            """,
            (DEFAULT_PRESET_ID, now, now),
        )
        _execute_statements(
            connection,
            "CREATE UNIQUE INDEX uq_storage_presets_default "
            "ON storage_presets(is_default) WHERE is_default = 1;"
            + STORAGE_CONFIG_SCHEMA_V3
            + UPLOAD_TASK_SCHEMA_V3,
        )
        connection.execute(
            """
            INSERT INTO storage_configs (
                id, preset_id, revision, provider, provider_schema_version,
                config_json, credentials_ciphertext, status, created_at,
                activated_at, last_tested_at, last_test_latency_ms
            )
            SELECT id, ?, revision, provider, provider_schema_version,
                   config_json, credentials_ciphertext, status, created_at,
                   activated_at, last_tested_at, last_test_latency_ms
            FROM storage_configs_v2
            """,
            (DEFAULT_PRESET_ID,),
        )
        connection.execute(
            """
            INSERT INTO upload_tasks (
                id, request_id, idempotency_key, storage_config_id, filename,
                content_type, object_key, public_url, status, size_bytes, etag,
                version_id, delete_token_hash, object_status, delete_request_id,
                delete_error_code, delete_started_at, deleted_at, error_code,
                created_at, finished_at, duration_ms
            )
            SELECT id, request_id, idempotency_key, storage_config_id, filename,
                   content_type, object_key, public_url, status, size_bytes, etag,
                   version_id, delete_token_hash, object_status, delete_request_id,
                   delete_error_code, delete_started_at, deleted_at, error_code,
                   created_at, finished_at, duration_ms
            FROM upload_tasks_v2
            """
        )
        connection.execute("DROP TABLE upload_tasks_v2")
        connection.execute("DROP TABLE storage_configs_v2")

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        for index in (
            "uq_upload_tasks_idempotency_key",
            "idx_upload_tasks_created_at_id",
            "idx_upload_tasks_status_created_at",
            "idx_upload_tasks_request_id",
            "idx_upload_tasks_storage_config_id",
            "idx_upload_tasks_object_status",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index}")
        connection.execute("ALTER TABLE upload_tasks RENAME TO upload_tasks_v3")
        _execute_statements(connection, UPLOAD_TASK_SCHEMA_V4)
        connection.execute(
            """
            INSERT INTO upload_tasks (
                id, request_id, idempotency_key, storage_config_id, filename,
                content_type, object_key, public_url, status, size_bytes, etag,
                version_id, delete_token_hash, object_status, delete_request_id,
                delete_error_code, delete_started_at, deleted_at, error_code,
                created_at, finished_at, duration_ms
            )
            SELECT id, request_id, idempotency_key, storage_config_id, filename,
                   content_type, object_key, public_url, status, size_bytes, etag,
                   version_id, delete_token_hash,
                   CASE
                       WHEN status='failed' THEN 'absent'
                       WHEN status IN ('uploading', 'unknown') THEN 'pending'
                       WHEN object_status='present' AND delete_token_hash IS NULL
                           THEN 'present_unclaimed'
                       WHEN status='succeeded' AND object_status='pending'
                            AND delete_token_hash IS NULL
                           THEN 'present_unclaimed'
                       WHEN status='succeeded' AND object_status='pending'
                           THEN 'present'
                       ELSE object_status
                   END,
                   delete_request_id, delete_error_code, delete_started_at,
                   deleted_at, error_code, created_at, finished_at, duration_ms
            FROM upload_tasks_v3
            """
        )
        connection.execute("DROP TABLE upload_tasks_v3")

    def _backup_before_upgrade(self, connection: sqlite3.Connection) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.path.with_name(f"{self.path.name}.pre-v4-{timestamp}")
        with closing(sqlite3.connect(backup_path)) as destination:
            connection.backup(destination)
        return backup_path

    def _verify_schema_v4(self, connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise RuntimeError("database schema version is not v4")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("database foreign key check failed")
        required = {
            "storage_presets",
            "storage_configs",
            "upload_tasks",
            "service_logs",
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required <= tables:
            raise RuntimeError("database schema v4 is incomplete")
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(upload_tasks)")
        }
        if not {
            "etag",
            "version_id",
            "delete_token_hash",
            "object_status",
            "deleted_at",
        } <= columns:
            raise RuntimeError("upload task schema v4 is incomplete")
        upload_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='upload_tasks'"
        ).fetchone()[0]
        if "present_unclaimed" not in upload_sql:
            raise RuntimeError("upload task schema v4 lacks unclaimed state")
        config_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(storage_configs)")
        }
        if "preset_id" not in config_columns:
            raise RuntimeError("storage config schema v4 is incomplete")
        preset_count = connection.execute(
            "SELECT COUNT(*) FROM storage_presets"
        ).fetchone()[0]
        default_count = connection.execute(
            """
            SELECT COUNT(*) FROM storage_presets
            WHERE enabled=1 AND is_default=1
            """
        ).fetchone()[0]
        if preset_count and default_count != 1:
            raise RuntimeError("database must have one enabled default preset")
        duplicate_active = connection.execute(
            """
            SELECT 1 FROM storage_configs
            WHERE status='active'
            GROUP BY preset_id HAVING COUNT(*) > 1
            """
        ).fetchone()
        if duplicate_active:
            raise RuntimeError("preset has multiple active configs")
        if connection.execute(
            """
            SELECT 1 FROM storage_configs c
            LEFT JOIN storage_presets p ON p.id=c.preset_id
            WHERE p.id IS NULL LIMIT 1
            """
        ).fetchone():
            raise RuntimeError("storage config has no preset")
        if connection.execute(
            """
            SELECT 1 FROM upload_tasks t
            LEFT JOIN storage_configs c ON c.id=t.storage_config_id
            WHERE c.id IS NULL LIMIT 1
            """
        ).fetchone():
            raise RuntimeError("upload task has no storage config")

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

    def active_storage(self, preset_key: str | None = None) -> dict[str, Any] | None:
        condition = "p.preset_key=?" if preset_key is not None else "p.is_default=1"
        values = (preset_key,) if preset_key is not None else ()
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""
                SELECT c.*, p.preset_key, p.display_name, p.enabled,
                       p.is_default, p.state_revision
                FROM storage_configs c
                JOIN storage_presets p ON p.id=c.preset_id
                WHERE c.status='active' AND {condition}
                """,
                values,
            ).fetchone()
        return dict(row) if row else None

    def active_storages(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.*, p.preset_key, p.display_name, p.enabled,
                       p.is_default, p.state_revision
                FROM storage_configs c
                JOIN storage_presets p ON p.id=c.preset_id
                WHERE c.status='active'
                ORDER BY p.preset_key
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_storage_presets(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT p.*, c.id AS storage_config_id, c.revision,
                       c.provider, c.provider_schema_version, c.activated_at,
                       c.last_tested_at, c.last_test_latency_ms
                FROM storage_presets p
                LEFT JOIN storage_configs c
                  ON c.preset_id=p.id AND c.status='active'
                ORDER BY p.preset_key
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def storage_preset_by_key(self, preset_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM storage_presets WHERE preset_key=?", (preset_key,)
            ).fetchone()
        return dict(row) if row else None

    def storage_by_id(self, config_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM storage_configs WHERE id=?", (config_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_storage_preset(
        self, preset: dict[str, Any], record: dict[str, Any]
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            is_default = (
                connection.execute("SELECT COUNT(*) FROM storage_presets").fetchone()[0]
                == 0
            )
            connection.execute(
                """
                INSERT INTO storage_presets (
                    id, preset_key, display_name, enabled, is_default,
                    state_revision, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, 1, ?, ?)
                """,
                (
                    preset["id"],
                    preset["preset_key"],
                    preset["display_name"],
                    int(is_default),
                    preset["created_at"],
                    preset["updated_at"],
                ),
            )
            self._insert_storage_config(connection, record, preset["id"], 1)
        record.update(
            preset_id=preset["id"],
            preset_key=preset["preset_key"],
            display_name=preset["display_name"],
            enabled=1,
            is_default=int(is_default),
            state_revision=1,
            revision=1,
            status="active",
        )
        return record

    @staticmethod
    def _insert_storage_config(
        connection: sqlite3.Connection,
        record: dict[str, Any],
        preset_id: str,
        revision: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO storage_configs (
                id, preset_id, revision, provider, provider_schema_version, config_json,
                credentials_ciphertext, status, created_at, activated_at,
                last_tested_at, last_test_latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                record["id"],
                preset_id,
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

    def activate_storage(
        self,
        record: dict[str, Any],
        expected_revision: int,
        preset_key: str | None = None,
    ) -> dict:
        with self.transaction() as connection:
            condition = "preset_key=?" if preset_key is not None else "is_default=1"
            values = (preset_key,) if preset_key is not None else ()
            preset = connection.execute(
                f"SELECT * FROM storage_presets WHERE {condition}", values
            ).fetchone()
            if preset is None:
                if preset_key is not None:
                    raise PresetNotFound(preset_key)
                now = record["created_at"]
                preset_id = DEFAULT_PRESET_ID
                if connection.execute(
                    "SELECT 1 FROM storage_presets WHERE id=?", (preset_id,)
                ).fetchone():
                    preset_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO storage_presets (
                        id, preset_key, display_name, enabled, is_default,
                        state_revision, created_at, updated_at
                    ) VALUES (?, 'default', '默认 ZOS', 1, 1, 1, ?, ?)
                    """,
                    (preset_id, now, now),
                )
            else:
                preset_id = preset["id"]
            preset_key = preset["preset_key"] if preset else "default"
            current = connection.execute(
                """
                SELECT revision FROM storage_configs
                WHERE preset_id=? AND status='active'
                """,
                (preset_id,),
            ).fetchone()
            current_revision = current["revision"] if current else 0
            if current_revision != expected_revision:
                raise RevisionConflict(current_revision)
            revision = current_revision + 1
            connection.execute(
                """
                UPDATE storage_configs SET status='inactive'
                WHERE preset_id=? AND status='active'
                """,
                (preset_id,),
            )
            self._insert_storage_config(connection, record, preset_id, revision)
        record.update(
            preset_id=preset_id,
            preset_key=preset_key,
            display_name=preset["display_name"] if preset else "默认 ZOS",
            enabled=preset["enabled"] if preset else 1,
            is_default=preset["is_default"] if preset else 1,
            state_revision=preset["state_revision"] if preset else 1,
            revision=revision,
            status="active",
        )
        return record

    def update_storage_preset(
        self,
        preset_key: str,
        expected_state_revision: int,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        if display_name is None and enabled is None:
            raise ValueError("no preset changes")
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM storage_presets WHERE preset_key=?", (preset_key,)
            ).fetchone()
            if current is None:
                raise PresetNotFound(preset_key)
            if current["state_revision"] != expected_state_revision:
                raise PresetStateConflict(current["state_revision"])
            if enabled is False and current["is_default"]:
                raise ValueError("default preset cannot be disabled")
            changes: dict[str, Any] = {
                "state_revision": expected_state_revision + 1,
                "updated_at": utc_now(),
            }
            if display_name is not None:
                changes["display_name"] = display_name
            if enabled is not None:
                changes["enabled"] = int(enabled)
            assignments = ", ".join(f"{name}=?" for name in changes)
            connection.execute(
                f"UPDATE storage_presets SET {assignments} WHERE id=?",
                (*changes.values(), current["id"]),
            )
            return dict(current) | changes

    def set_default_storage_preset(
        self,
        preset_key: str,
        expected_default_preset: str,
        expected_state_revision: int,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM storage_presets WHERE is_default=1"
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM storage_presets WHERE preset_key=?", (preset_key,)
            ).fetchone()
            if target is None:
                raise PresetNotFound(preset_key)
            current_key = current["preset_key"] if current else None
            if (
                current_key != expected_default_preset
                or target["state_revision"] != expected_state_revision
            ):
                raise DefaultPresetConflict(
                    current_key, target["state_revision"]
                )
            if not target["enabled"]:
                raise ValueError("default preset must be enabled")
            if current and current["id"] == target["id"]:
                return dict(target)
            now = utc_now()
            if current:
                connection.execute(
                    """
                    UPDATE storage_presets
                    SET is_default=0, state_revision=state_revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (now, current["id"]),
                )
            connection.execute(
                """
                UPDATE storage_presets
                SET is_default=1, state_revision=state_revision+1, updated_at=?
                WHERE id=?
                """,
                (now, target["id"]),
            )
            return dict(target) | {
                "is_default": 1,
                "state_revision": target["state_revision"] + 1,
                "updated_at": now,
            }

    def create_task(self, record: dict[str, Any]) -> None:
        status = record["status"]
        object_status = record.get(
            "object_status",
            "absent"
            if status == "failed"
            else "present"
            if status == "succeeded"
            else "pending",
        )
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
            "object_status",
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
                tuple(
                    object_status if column == "object_status" else record.get(column)
                    for column in columns
                ),
            )

    def update_task(self, task_id: str, **changes: Any) -> None:
        allowed = {
            "public_url",
            "status",
            "size_bytes",
            "etag",
            "version_id",
            "delete_token_hash",
            "object_status",
            "delete_request_id",
            "delete_error_code",
            "delete_started_at",
            "deleted_at",
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

    def claim_task_deletion(
        self, task_id: str, request_id: str, started_at: str
    ) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_tasks
                SET object_status='deleting', delete_request_id=?,
                    delete_error_code=NULL, delete_started_at=?, deleted_at=NULL
                WHERE id=? AND status='succeeded' AND object_status='present'
                """,
                (request_id, started_at, task_id),
            )
            return cursor.rowcount == 1

    def claim_unclaimed_deletion(
        self, task_id: str, request_id: str, started_at: str
    ) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_tasks
                SET object_status='deleting', delete_request_id=?,
                    delete_error_code=NULL, delete_started_at=?, deleted_at=NULL
                WHERE id=? AND status='succeeded'
                  AND object_status='present_unclaimed'
                  AND delete_token_hash IS NULL
                """,
                (request_id, started_at, task_id),
            )
            return cursor.rowcount == 1

    def task_by_id(self, task_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT t.*, p.preset_key AS storage_preset,
                       s.provider AS storage_provider,
                       s.revision AS storage_config_revision
                FROM upload_tasks t
                JOIN storage_configs s ON s.id=t.storage_config_id
                JOIN storage_presets p ON p.id=s.preset_id
                WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def task_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT t.*, p.preset_key AS storage_preset
                FROM upload_tasks t
                JOIN storage_configs s ON s.id=t.storage_config_id
                JOIN storage_presets p ON p.id=s.preset_id
                WHERE t.idempotency_key=?
                """,
                (key,),
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
                SELECT t.*, p.preset_key AS storage_preset,
                       s.provider AS storage_provider,
                       s.revision AS storage_config_revision
                FROM upload_tasks t
                JOIN storage_configs s ON s.id=t.storage_config_id
                JOIN storage_presets p ON p.id=s.preset_id
                {clause}
                ORDER BY t.created_at DESC, t.id DESC
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_tasks(
        self, stale_before: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM upload_tasks WHERE status='unknown'"
        values: list[Any] = []
        if stale_before:
            query += " OR (status='uploading' AND created_at<?)"
            values.append(stale_before)
        query += " ORDER BY created_at, id"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with closing(self.connect()) as connection:
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def pending_deletions(
        self, stale_before: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        limit_clause = " LIMIT ?" if limit is not None else ""
        values: tuple[Any, ...] = (
            (stale_before, limit) if limit is not None else (stale_before,)
        )
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM upload_tasks
                WHERE object_status='delete_unknown'
                OR (
                    object_status='deleting'
                    AND delete_started_at<?
                )
                ORDER BY delete_started_at, id
                {limit_clause}
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def recovery_backlog(
        self, stale_upload_before: str, stale_delete_before: str
    ) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            uploads = connection.execute(
                """
                SELECT COUNT(*), MIN(created_at) FROM upload_tasks
                WHERE status='unknown'
                   OR (status='uploading' AND created_at<?)
                """,
                (stale_upload_before,),
            ).fetchone()
            deletions = connection.execute(
                """
                SELECT COUNT(*), MIN(COALESCE(delete_started_at, created_at))
                FROM upload_tasks
                WHERE object_status='delete_unknown'
                   OR (object_status='deleting' AND delete_started_at<?)
                """,
                (stale_delete_before,),
            ).fetchone()
        oldest = min(
            (value for value in (uploads[1], deletions[1]) if value is not None),
            default=None,
        )
        return {
            "pending_uploads": uploads[0],
            "pending_deletions": deletions[0],
            "pending_tasks": uploads[0] + deletions[0],
            "oldest_created_at": oldest,
        }

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
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> list[dict[str, Any]]:
        where, values = ["level_no>=?"], [min_level]
        if before_id is not None:
            where.append("id<?")
            values.append(before_id)
        if from_time:
            where.append("created_at>=?")
            values.append(from_time)
        if to_time:
            where.append("created_at<?")
            values.append(to_time)
        filters = filters or {}
        if not set(filters) <= {"event", "request_id", "task_id", "error_code"}:
            raise ValueError("invalid log filter")
        for key, value in filters.items():
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

    def tasks_in_range(self, from_time: str, to_time: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT status, size_bytes, duration_ms, created_at
                FROM upload_tasks
                WHERE created_at>=? AND created_at<?
                ORDER BY created_at
                """,
                (from_time, to_time),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, from_time: str, to_time: str) -> dict[str, Any]:
        tasks = self.tasks_in_range(from_time, to_time)
        counts = {
            status: sum(task["status"] == status for task in tasks)
            for status in ("succeeded", "failed", "uploading", "unknown")
        }
        durations = sorted(
            task["duration_ms"]
            for task in tasks
            if task["duration_ms"] is not None
            and task["status"] in {"succeeded", "failed"}
        )
        completed = counts["succeeded"] + counts["failed"]
        p95_index = max(0, (95 * len(durations) + 99) // 100 - 1)
        return {
            "attempt_count": len(tasks),
            "success_count": counts["succeeded"],
            "failure_count": counts["failed"],
            "uploading_count": counts["uploading"],
            "unknown_count": counts["unknown"],
            "success_rate": counts["succeeded"] / completed if completed else None,
            "successful_upload_bytes": sum(
                task["size_bytes"] or 0
                for task in tasks
                if task["status"] == "succeeded"
            ),
            "average_duration_ms": round(sum(durations) / len(durations))
            if durations
            else None,
            "p95_duration_ms": durations[p95_index] if durations else None,
        }

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
                WHERE status IN ('succeeded','failed')
                AND object_status IN ('absent','deleted')
                AND created_at<?
                """,
                (task_cutoff,),
            )
            connection.execute(
                """
                DELETE FROM service_logs
                WHERE created_at<? AND event NOT LIKE 'object_delete_%'
                """,
                (log_cutoff,),
            )
            connection.execute(
                """
                DELETE FROM service_logs WHERE id IN (
                    SELECT id FROM service_logs
                    WHERE event NOT LIKE 'object_delete_%'
                    ORDER BY id DESC LIMIT -1 OFFSET ?
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


class PresetNotFound(Exception):
    def __init__(self, preset_key: str):
        self.preset_key = preset_key


class PresetStateConflict(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision


class DefaultPresetConflict(Exception):
    def __init__(
        self, current_default_preset: str | None, current_state_revision: int
    ):
        self.current_default_preset = current_default_preset
        self.current_state_revision = current_state_revision
