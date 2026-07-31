from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import Fernet, InvalidToken


MAGIC = b"ZOSBACKUP1"
SALT_BYTES = 16
ARCHIVE_FILES = {"database.sqlite3", "manifest.json", "settings_encryption_key.txt"}


class BackupError(RuntimeError):
    pass


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise BackupError(f"{name} 未配置")
    return value


def _config(env: Mapping[str, str], *, require_source: bool = False) -> dict[str, Any]:
    endpoint = _required(env, "BACKUP_ZOS_ENDPOINT")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise BackupError("BACKUP_ZOS_ENDPOINT 必须是无路径的 HTTPS URL")
    bucket = _required(env, "BACKUP_ZOS_BUCKET")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise BackupError("BACKUP_ZOS_BUCKET 格式无效")
    prefix = env.get("BACKUP_PREFIX", "ctyun-zos-upload").strip().strip("/")
    if not prefix or ".." in prefix or not re.fullmatch(r"[A-Za-z0-9._/-]+", prefix):
        raise BackupError("BACKUP_PREFIX 格式无效")
    passphrase = _required(env, "BACKUP_PASSPHRASE")
    if len(passphrase) < 32:
        raise BackupError("BACKUP_PASSPHRASE 至少需要 32 个字符")
    config = {
        "endpoint": endpoint.rstrip("/"),
        "bucket": bucket,
        "prefix": prefix,
        "access_key": _required(env, "BACKUP_ZOS_ACCESS_KEY"),
        "secret_key": _required(env, "BACKUP_ZOS_SECRET_KEY"),
        "passphrase": passphrase,
    }
    if require_source:
        settings_key = _required(env, "SETTINGS_ENCRYPTION_KEY")
        try:
            Fernet(settings_key.encode())
        except (TypeError, ValueError) as exc:
            raise BackupError("SETTINGS_ENCRYPTION_KEY 格式无效") from exc
        config.update(
            settings_key=settings_key,
            database_path=Path(
                env.get("DATABASE_PATH", "/data/db/zos-upload.db")
            ),
        )
    return config


def _cipher(passphrase: str, salt: bytes) -> Fernet:
    key = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode(), salt, 600_000, dklen=32
    )
    return Fernet(base64.urlsafe_b64encode(key))


def _tar_add(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    item = tarfile.TarInfo(name)
    item.size = len(value)
    item.mode = 0o600
    item.mtime = 0
    archive.addfile(item, io.BytesIO(value))


def _database_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise BackupError(f"SQLite integrity_check 失败：{integrity}")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "task_count": connection.execute("SELECT count(*) FROM upload_tasks").fetchone()[
            0
        ]
        if "upload_tasks" in tables
        else 0,
        "preset_count": connection.execute(
            "SELECT count(*) FROM storage_presets"
        ).fetchone()[0]
        if "storage_presets" in tables
        else 0,
    }


def build_backup(
    database_path: Path,
    settings_key: str,
    passphrase: str,
    *,
    now: datetime | None = None,
) -> tuple[bytes, dict[str, Any]]:
    if not database_path.is_file():
        raise BackupError("SQLite 数据库不存在")
    created = (now or datetime.now(UTC)).astimezone(UTC)
    with tempfile.TemporaryDirectory() as directory:
        snapshot_path = Path(directory) / "database.sqlite3"
        source = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        destination = sqlite3.connect(snapshot_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        database_bytes = snapshot_path.read_bytes()
        with sqlite3.connect(snapshot_path) as snapshot:
            summary = _database_summary(snapshot)

    manifest = {
        "format_version": 1,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "database_sha256": hashlib.sha256(database_bytes).hexdigest(),
        **summary,
    }
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        _tar_add(archive, "database.sqlite3", database_bytes)
        _tar_add(
            archive,
            "manifest.json",
            json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(),
        )
        _tar_add(archive, "settings_encryption_key.txt", settings_key.encode())

    # ponytail: archive is encrypted in memory; switch to streaming encryption if
    # operational databases grow beyond a few hundred MiB.
    salt = os.urandom(SALT_BYTES)
    encrypted = _cipher(passphrase, salt).encrypt(archive_buffer.getvalue())
    return MAGIC + salt + encrypted, manifest


def verify_backup(
    blob: bytes,
    passphrase: str,
    *,
    restore_directory: Path | None = None,
) -> dict[str, Any]:
    if not blob.startswith(MAGIC) or len(blob) <= len(MAGIC) + SALT_BYTES:
        raise BackupError("备份格式无效")
    salt_start = len(MAGIC)
    salt = blob[salt_start : salt_start + SALT_BYTES]
    try:
        plaintext = _cipher(passphrase, salt).decrypt(blob[salt_start + SALT_BYTES :])
    except InvalidToken as exc:
        raise BackupError("备份密码错误或备份已损坏") from exc

    try:
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as archive:
            if {item.name for item in archive.getmembers()} != ARCHIVE_FILES:
                raise BackupError("备份内容不完整")
            database_bytes = archive.extractfile("database.sqlite3").read()
            manifest = json.load(archive.extractfile("manifest.json"))
            settings_key = (
                archive.extractfile("settings_encryption_key.txt").read().decode()
            )
    except (AttributeError, json.JSONDecodeError, tarfile.TarError, UnicodeDecodeError) as exc:
        raise BackupError("备份内容无法解析") from exc
    if manifest.get("format_version") != 1:
        raise BackupError("不支持的备份格式版本")
    if not hmac.compare_digest(
        hashlib.sha256(database_bytes).hexdigest(),
        str(manifest.get("database_sha256", "")),
    ):
        raise BackupError("数据库摘要不匹配")
    try:
        Fernet(settings_key.encode())
    except (TypeError, ValueError) as exc:
        raise BackupError("备份中的 SETTINGS_ENCRYPTION_KEY 无效") from exc

    with tempfile.NamedTemporaryFile() as database_file:
        database_file.write(database_bytes)
        database_file.flush()
        with sqlite3.connect(database_file.name) as connection:
            summary = _database_summary(connection)
    for name in ("schema_version", "task_count", "preset_count"):
        if summary[name] != manifest.get(name):
            raise BackupError(f"备份清单中的 {name} 不匹配")
    result = {
        **manifest,
        "integrity": "ok",
        "settings_encryption_key": "present",
    }
    if restore_directory is not None:
        if restore_directory.exists():
            raise BackupError("恢复目录已存在，拒绝覆盖")
        restore_directory.mkdir(mode=0o700)
        database_output = restore_directory / "zos-upload.db"
        key_output = restore_directory / "settings-encryption-key.env"
        database_output.write_bytes(database_bytes)
        key_output.write_text(f"SETTINGS_ENCRYPTION_KEY={settings_key}\n")
        database_output.chmod(0o600)
        key_output.chmod(0o600)
        result["restored_to"] = str(restore_directory)
    return result


def _client(config: Mapping[str, Any]):
    return boto3.client(
        "s3",
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        endpoint_url=config["endpoint"],
        config=Config(
            connect_timeout=5,
            read_timeout=300,
            retries={"max_attempts": 2, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def create(env: Mapping[str, str], client: Any | None = None) -> dict[str, Any]:
    config = _config(env, require_source=True)
    blob, manifest = build_backup(
        config["database_path"], config["settings_key"], config["passphrase"]
    )
    created = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    object_key = (
        f"{config['prefix']}/{created:%Y/%m/%d}/"
        f"zos-upload-{created:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.backup"
    )
    digest = hashlib.sha256(blob).hexdigest()
    storage = client or _client(config)
    storage.put_object(
        Bucket=config["bucket"],
        Key=object_key,
        Body=blob,
        ACL="private",
        ContentType="application/octet-stream",
        Metadata={
            "sha256": digest,
            "schema-version": str(manifest["schema_version"]),
        },
    )
    remote = storage.head_object(Bucket=config["bucket"], Key=object_key)
    if remote.get("ContentLength") != len(blob):
        raise BackupError("远端备份大小校验失败")
    return {
        "status": "ok",
        "bucket": config["bucket"],
        "object_key": object_key,
        "size_bytes": len(blob),
        **manifest,
    }


def _download(
    env: Mapping[str, str], object_key: str, client: Any | None
) -> tuple[dict[str, Any], bytes]:
    config = _config(env)
    if not object_key.startswith(f"{config['prefix']}/") or ".." in object_key:
        raise BackupError("备份对象 Key 不属于配置的前缀")
    storage = client or _client(config)
    response = storage.get_object(Bucket=config["bucket"], Key=object_key)
    blob = response["Body"].read()
    expected = response.get("Metadata", {}).get("sha256")
    if expected and not hmac.compare_digest(hashlib.sha256(blob).hexdigest(), expected):
        raise BackupError("远端备份摘要不匹配")
    return config, blob


def verify(
    env: Mapping[str, str], object_key: str, client: Any | None = None
) -> dict[str, Any]:
    config, blob = _download(env, object_key, client)
    return {
        "status": "ok",
        "bucket": config["bucket"],
        "object_key": object_key,
        **verify_backup(blob, config["passphrase"]),
    }


def restore(
    env: Mapping[str, str],
    object_key: str,
    output_directory: Path,
    client: Any | None = None,
) -> dict[str, Any]:
    config, blob = _download(env, object_key, client)
    return {
        "status": "ok",
        "bucket": config["bucket"],
        "object_key": object_key,
        **verify_backup(
            blob,
            config["passphrase"],
            restore_directory=output_directory,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ZOS 私有加密备份")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("create")
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("object_key")
    restore_parser = subcommands.add_parser("restore")
    restore_parser.add_argument("object_key")
    restore_parser.add_argument("output_directory", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            result = create(os.environ)
        elif arguments.command == "verify":
            result = verify(os.environ, arguments.object_key)
        else:
            result = restore(
                os.environ,
                arguments.object_key,
                arguments.output_directory,
            )
    except (BackupError, BotoCoreError, ClientError) as exc:
        code = (
            exc.response.get("Error", {}).get("Code", "ZOS_ERROR")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        print(json.dumps({"status": "error", "code": code}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
