from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

SNAPSHOT_RE = re.compile(r"^deploy-.+-from-(\d+)-to-(\d+)\.sqlite3$")


def _validate_database(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"SQLite 文件不存在：{path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite integrity_check 失败：{result}")


def snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    _validate_database(destination)


def restore(source: Path, destination: Path) -> None:
    _validate_database(source)
    for suffix in ("-wal", "-shm"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    _validate_database(destination)


def prune(
    directory: Path,
    *,
    keep_releases: int,
    keep_migrations: int,
    max_bytes: int,
) -> dict[str, int]:
    files = sorted(
        (
            path
            for path in directory.glob("deploy-*.sqlite3")
            if SNAPSHOT_RE.fullmatch(path.name)
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    kept: list[Path] = []
    release_count = migration_count = total_bytes = 0
    for path in files:
        match = SNAPSHOT_RE.fullmatch(path.name)
        assert match is not None
        is_migration = match[1] != match[2]
        category_count = migration_count if is_migration else release_count
        category_limit = keep_migrations if is_migration else keep_releases
        size = path.stat().st_size
        mandatory = not kept or (is_migration and migration_count == 0)
        if category_count < category_limit and (
            mandatory or total_bytes + size <= max_bytes
        ):
            kept.append(path)
            total_bytes += size
            if is_migration:
                migration_count += 1
            else:
                release_count += 1
        else:
            path.unlink()
    return {
        "kept": len(kept),
        "deleted": len(files) - len(kept),
        "bytes": total_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="部署 SQLite 快照工具")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "restore"):
        command = commands.add_parser(name)
        command.add_argument("source", type=Path)
        command.add_argument("destination", type=Path)
    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("directory", type=Path)
    prune_parser.add_argument("--keep-releases", type=int, required=True)
    prune_parser.add_argument("--keep-migrations", type=int, required=True)
    prune_parser.add_argument("--max-bytes", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.command == "snapshot":
        snapshot(arguments.source, arguments.destination)
    elif arguments.command == "restore":
        restore(arguments.source, arguments.destination)
    else:
        if (
            min(
                arguments.keep_releases,
                arguments.keep_migrations,
                arguments.max_bytes,
            )
            < 1
        ):
            parser.error("保留数量和容量必须大于 0")
        print(
            json.dumps(
                prune(
                    arguments.directory,
                    keep_releases=arguments.keep_releases,
                    keep_migrations=arguments.keep_migrations,
                    max_bytes=arguments.max_bytes,
                ),
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
