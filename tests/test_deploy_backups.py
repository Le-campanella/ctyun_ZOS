from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.deploy_backups import prune, restore, snapshot


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))


def test_snapshot_restore_and_bounded_retention(tmp_path):
    database = tmp_path / "live.sqlite3"
    backup = tmp_path / "deploy-01-from-3-to-4.sqlite3"
    _database(database, "before")
    snapshot(database, backup)

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE marker SET value='after'")
    restore(backup, database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "before"

    names = [
        "deploy-02-from-4-to-4.sqlite3",
        "deploy-03-from-4-to-4.sqlite3",
        "deploy-04-from-4-to-5.sqlite3",
        "deploy-05-from-5-to-5.sqlite3",
    ]
    for index, name in enumerate(names, start=2):
        path = tmp_path / name
        path.write_bytes(b"x" * 10)
        os.utime(path, (index, index))
    os.utime(backup, (1, 1))

    result = prune(
        tmp_path,
        keep_releases=2,
        keep_migrations=1,
        max_bytes=30,
    )

    assert result == {"kept": 3, "deleted": 2, "bytes": 30}
    assert {path.name for path in tmp_path.glob("deploy-*.sqlite3")} == {
        "deploy-03-from-4-to-4.sqlite3",
        "deploy-04-from-4-to-5.sqlite3",
        "deploy-05-from-5-to-5.sqlite3",
    }
