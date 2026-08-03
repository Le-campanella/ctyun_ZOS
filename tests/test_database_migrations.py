from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import Database, SCHEMA_V1, SCHEMA_VERSION


CONFIG_IDS = ("config-v1", "config-v2")
TASK_IDS = {
    "succeeded": "task-succeeded",
    "failed": "task-failed",
    "uploading": "task-uploading",
    "unknown": "task-unknown",
}


def create_v1(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA_V1)
    connection.execute("PRAGMA user_version=1")
    for revision, config_id in enumerate(CONFIG_IDS, 1):
        connection.execute(
            """
            INSERT INTO storage_configs (
                id, revision, provider, provider_schema_version, config_json,
                credentials_ciphertext, status, created_at, activated_at,
                last_tested_at, last_test_latency_ms
            ) VALUES (?, ?, 'ctyun_zos', 1, '{}', ?, ?, ?, ?, ?, 10)
            """,
            (
                config_id,
                revision,
                b"encrypted",
                "active" if revision == 2 else "inactive",
                f"2026-07-3{revision}T00:00:00Z",
                f"2026-07-3{revision}T00:00:00Z",
                f"2026-07-3{revision}T00:00:00Z",
            ),
        )
    for status, task_id in TASK_IDS.items():
        connection.execute(
            """
            INSERT INTO upload_tasks (
                id, request_id, idempotency_key, storage_config_id, filename,
                content_type, object_key, public_url, status, size_bytes,
                error_code, created_at, finished_at, duration_ms
            ) VALUES (?, ?, ?, ?, 'file.bin', 'application/octet-stream', ?,
                      ?, ?, 7, NULL, '2026-07-31T00:00:00Z', NULL, NULL)
            """,
            (
                task_id,
                f"request-{status}",
                f"key-{status}",
                CONFIG_IDS[1],
                f"2026/07/31/{task_id}.bin",
                f"https://files.example/{task_id}.bin",
                status,
            ),
        )
    connection.execute(
        """
        INSERT INTO service_logs (
            created_at, level_no, level_name, event, message
        ) VALUES ('2026-07-31T00:00:00Z', 25, 'NOTIFY', 'legacy', 'kept')
        """
    )
    connection.commit()
    connection.close()


def create_v2(path: Path) -> None:
    create_v1(path)
    database = Database(path)
    with database.connect() as connection:
        database._run_migration(connection, 2, database._migrate_v1_to_v2)


def create_v3(path: Path) -> None:
    create_v2(path)
    database = Database(path)
    with database.connect() as connection:
        database._run_migration(
            connection, 3, database._migrate_v2_to_v3, foreign_keys=False
        )


def test_empty_database_creates_schema_v4_and_is_idempotent(tmp_path: Path):
    path = tmp_path / "empty.db"
    database = Database(path)

    assert database.initialize() is None
    assert database.initialize() is None

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "storage_presets",
            "storage_configs",
            "upload_tasks",
            "service_logs",
        } <= tables
        assert connection.execute("SELECT COUNT(*) FROM storage_presets").fetchone()[0] == 0


def test_v1_to_v4_preserves_ids_revisions_tasks_and_creates_backup(tmp_path: Path):
    path = tmp_path / "service.db"
    create_v1(path)
    database = Database(path)

    backup = database.initialize()

    assert backup is not None and backup.exists()
    assert backup.name.startswith("service.db.pre-v4-")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_tasks").fetchone()[0] == 4

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        preset = connection.execute("SELECT * FROM storage_presets").fetchone()
        assert (preset["preset_key"], preset["enabled"], preset["is_default"]) == (
            "default",
            1,
            1,
        )
        configs = connection.execute(
            "SELECT id, revision, preset_id FROM storage_configs ORDER BY revision"
        ).fetchall()
        assert [(row["id"], row["revision"]) for row in configs] == [
            ("config-v1", 1),
            ("config-v2", 2),
        ]
        assert {row["preset_id"] for row in configs} == {preset["id"]}
        tasks = connection.execute(
            """
            SELECT id, status, object_status, delete_token_hash, storage_config_id
            FROM upload_tasks
            """
        ).fetchall()
        assert {row["id"] for row in tasks} == set(TASK_IDS.values())
        assert {row["storage_config_id"] for row in tasks} == {"config-v2"}
        states = {row["status"]: row["object_status"] for row in tasks}
        assert states == {
            "succeeded": "legacy_unverified",
            "failed": "absent",
            "uploading": "pending",
            "unknown": "pending",
        }
        assert all(row["delete_token_hash"] is None for row in tasks)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM service_logs").fetchone()[0] == 1

    assert database.initialize() is None


def test_v2_to_v4_allows_revision_one_per_preset(tmp_path: Path):
    path = tmp_path / "service.db"
    create_v2(path)
    database = Database(path)

    database.initialize()

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO storage_presets (
                id, preset_key, display_name, enabled, is_default,
                state_revision, created_at, updated_at
            ) VALUES ('preset-2', 'archive', 'Archive', 1, 0, 1, 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO storage_configs (
                id, preset_id, revision, provider, provider_schema_version,
                config_json, credentials_ciphertext, status, created_at,
                activated_at, last_tested_at
            ) VALUES (
                'archive-config-1', 'preset-2', 1, 'ctyun_zos', 1,
                '{}', X'00', 'active', 'now', 'now', 'now'
            )
            """
        )
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_configs WHERE revision=1"
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT preset_id FROM storage_configs
                WHERE status='active' GROUP BY preset_id
            )
            """
        ).fetchone()[0] == 2


def test_v2_to_v3_failure_rolls_back_current_migration(tmp_path: Path, monkeypatch):
    path = tmp_path / "service.db"
    create_v2(path)
    database = Database(path)

    def fail_after_rename(connection):
        connection.execute("ALTER TABLE upload_tasks RENAME TO broken_tasks")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(database, "_migrate_v2_to_v3", fail_after_rename)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        database.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "upload_tasks" in tables
        assert "broken_tasks" not in tables
        assert connection.execute("SELECT COUNT(*) FROM upload_tasks").fetchone()[0] == 4


def test_v3_to_v4_marks_unclaimed_objects_and_enforces_state_invariants(
    tmp_path: Path,
):
    path = tmp_path / "service.db"
    create_v3(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE upload_tasks
            SET status='succeeded', object_status='present', delete_token_hash=NULL
            WHERE id=?
            """,
            (TASK_IDS["succeeded"],),
        )
        connection.commit()
    database = Database(path)

    backup = database.initialize()

    assert backup is not None and backup.name.startswith("service.db.pre-v4-")
    task = database.task_by_id(TASK_IDS["succeeded"])
    assert task["object_status"] == "present_unclaimed"
    with pytest.raises(sqlite3.IntegrityError):
        database.update_task(
            TASK_IDS["failed"], status="failed", object_status="pending"
        )


def test_v3_to_v4_failure_rolls_back_current_migration(tmp_path: Path, monkeypatch):
    path = tmp_path / "service.db"
    create_v3(path)
    database = Database(path)

    def fail_after_rename(connection):
        connection.execute("ALTER TABLE upload_tasks RENAME TO broken_tasks")
        raise RuntimeError("injected v4 migration failure")

    monkeypatch.setattr(database, "_migrate_v3_to_v4", fail_after_rename)
    with pytest.raises(RuntimeError, match="injected v4 migration failure"):
        database.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "upload_tasks" in tables
        assert "broken_tasks" not in tables
