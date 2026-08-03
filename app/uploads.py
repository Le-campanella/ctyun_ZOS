from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from .database import QuotaExceeded, utc_now
from .eventlog import NOTIFY
from .http import (
    APIError,
    BodyTooLarge,
    database_call,
    duration,
    no_store,
    request_id,
)
from .providers import ProviderError, require_upload_metadata
from .runtime import Runtime
from .security import issue_delete_token

SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")
SAFE_PRESET_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def _filename(value: str | None) -> str:
    name = (value or "unnamed").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char >= " " and char != "\x7f")
    return name[:255] or "unnamed"


def _extension(filename: str) -> str:
    suffix = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    return suffix if SAFE_EXTENSION.fullmatch(suffix) else ""


def _content_type(value: str | None) -> str:
    if (
        not value
        or "/" not in value
        or len(value) > 255
        or any(ord(char) < 32 for char in value)
    ):
        return "application/octet-stream"
    return value


async def _upload_file(request: Request, runtime: Runtime):
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise APIError(400, "BAD_REQUEST", "上传请求必须使用 multipart/form-data")
    try:
        form = await request.form(
            max_files=2,
            max_fields=10,
            max_part_size=runtime.settings.max_upload_bytes + 1,
        )
    except BodyTooLarge:
        raise
    except Exception as exc:
        raise APIError(400, "BAD_REQUEST", "multipart 请求格式错误") from exc
    try:
        files = form.getlist("file")
        if not files:
            raise APIError(400, "FILE_REQUIRED", "缺少 file 字段")
        if len(files) != 1 or not isinstance(files[0], UploadFile):
            raise APIError(400, "BAD_REQUEST", "每次请求只能上传一个文件")
        return form, files[0]
    except Exception:
        await form.close()
        raise


async def _source_size(source: UploadFile) -> int:
    if source.size is None:

        def measure() -> int:
            source.file.seek(0, 2)
            size = source.file.tell()
            source.file.seek(0)
            return size

        size = await anyio.to_thread.run_sync(measure)
    else:
        size = source.size
        await source.seek(0)
    return size


def _upload_response(task: dict, delete_token: str | None = None) -> dict:
    return {
        "task_id": task["id"],
        "storage_preset": task["storage_preset"],
        "key": task["object_key"],
        "url": task["public_url"],
        "size_bytes": task["size_bytes"],
        "content_type": task["content_type"],
        "etag": task["etag"],
        "version_id": task["version_id"],
        "delete_capability_available": (
            delete_token is not None or task.get("delete_token_hash") is not None
        ),
        "delete_token": delete_token,
    }


def provider_status(error: ProviderError) -> int:
    if error.code == "STORAGE_PRESET_NOT_FOUND":
        return 404
    if error.code == "STORAGE_PRESET_DISABLED":
        return 409
    if error.code in {
        "STORAGE_CONFIG_INVALID",
        "STORAGE_CREDENTIALS_REQUIRED",
        "STORAGE_ENDPOINT_FORBIDDEN",
    }:
        return 400
    if error.code in {
        "STORAGE_DEFAULT_NOT_CONFIGURED",
        "STORAGE_METRICS_UNAVAILABLE",
        "STORAGE_NOT_CONFIGURED",
    }:
        return 503
    return 502


def preset_key(value: str) -> str:
    if not SAFE_PRESET_KEY.fullmatch(value):
        raise APIError(400, "STORAGE_PRESET_INVALID", "preset_key 格式不合法")
    return value


def _existing_upload(task: dict, requested_preset: str | None) -> JSONResponse:
    if requested_preset is not None and requested_preset != task["storage_preset"]:
        raise APIError(
            409,
            "IDEMPOTENCY_SCOPE_MISMATCH",
            "该幂等键已绑定其他存储预设",
            task_id=task["id"],
        )
    if task["status"] == "succeeded":
        response = no_store(_upload_response(task))
        response.headers["Idempotency-Replayed"] = "true"
        return response
    code = (
        "IDEMPOTENCY_KEY_REUSED" if task["status"] == "failed" else "UPLOAD_IN_PROGRESS"
    )
    raise APIError(409, code, "该幂等键已经绑定上传任务", task_id=task["id"])


async def validate_upload(request: Request, runtime: Runtime) -> dict:
    form, source = await _upload_file(request, runtime)
    try:
        filename = _filename(source.filename)
        content_type = _content_type(source.content_type)
        size = await _source_size(source)
        if size > runtime.settings.max_upload_bytes:
            raise APIError(413, "FILE_TOO_LARGE", "文件不能超过 200 MiB")
        if size == 0:
            raise APIError(400, "FILE_EMPTY", "文件不能为空")
        return {
            "received": True,
            "uploaded_to_storage": False,
            "recorded_as_task": False,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "request_id": request_id(request),
        }
    finally:
        await form.close()


async def upload(request: Request, runtime: Runtime, storage_preset: str | None):
    client_id = request.state.client_id
    idempotency = request.headers.get("idempotency-key")
    if idempotency is not None:
        if not 1 <= len(idempotency) <= 128 or any(
            ord(char) < 33 or ord(char) > 126 for char in idempotency
        ):
            raise APIError(400, "BAD_REQUEST", "Idempotency-Key 不合法")
        existing = await database_call(
            runtime.database.task_by_idempotency, client_id, idempotency
        )
        if existing:
            return _existing_upload(existing, storage_preset)
    if storage_preset is not None:
        storage_preset = preset_key(storage_preset)
    try:
        snapshot = runtime.resolve_upload_snapshot(storage_preset)
    except ProviderError as exc:
        raise APIError(provider_status(exc), exc.code, exc.message) from exc
    provider = snapshot.provider
    form, source = await _upload_file(request, runtime)
    try:
        filename = _filename(source.filename)
        content_type = _content_type(source.content_type)
        size = await _source_size(source)
    except Exception:
        await form.close()
        raise
    task_id = str(uuid4())
    date = datetime.now(ZoneInfo(runtime.settings.app_timezone)).strftime("%Y/%m/%d")
    extension = _extension(filename)
    object_key = f"{date}/{task_id}{'.' + extension if extension else ''}"
    public_url = provider.build_public_url(object_key)
    task = {
        "id": task_id,
        "request_id": request_id(request),
        "client_id": client_id,
        "idempotency_key": idempotency,
        "storage_config_id": snapshot.storage_config_id,
        "filename": filename,
        "content_type": content_type,
        "object_key": object_key,
        "public_url": public_url,
        "status": "uploading",
        "size_bytes": size,
        "error_code": None,
        "created_at": utc_now(),
        "finished_at": None,
        "duration_ms": None,
    }
    try:
        if size == 0 or size > runtime.settings.max_upload_bytes:
            await database_call(runtime.database.create_task, task)
        else:
            await database_call(
                runtime.database.create_task_with_quota,
                task,
                max_objects=runtime.settings.client_max_objects,
                max_bytes=runtime.settings.client_max_bytes,
            )
    except QuotaExceeded as exc:
        existing = await database_call(
            runtime.database.task_by_idempotency, client_id, idempotency
        )
        await form.close()
        if existing:
            return _existing_upload(existing, snapshot.preset_key)
        raise APIError(
            429,
            "CLIENT_QUOTA_EXCEEDED",
            "调用方对象数量或字节配额已满",
            headers={"Retry-After": "3600"},
        ) from exc
    except sqlite3.IntegrityError:
        existing = await database_call(
            runtime.database.task_by_idempotency, client_id, idempotency
        )
        if existing:
            await form.close()
            return _existing_upload(existing, snapshot.preset_key)
        await form.close()
        raise APIError(
            409,
            "UPLOAD_IN_PROGRESS",
            "该幂等键已经绑定上传任务",
            task_id=existing["id"] if existing else None,
        )
    except sqlite3.Error as exc:
        await form.close()
        raise APIError(500, "DATABASE_ERROR", "无法创建上传任务") from exc
    runtime.log.emit(
        NOTIFY,
        "upload_started",
        "上传任务已开始",
        request_id=task["request_id"],
        task_id=task_id,
        details={
            "filename": filename,
            "object_key": object_key,
            "storage_preset": snapshot.preset_key,
        },
    )
    try:
        if size > runtime.settings.max_upload_bytes:
            await database_call(
                runtime.database.update_task,
                task_id,
                status="failed",
                size_bytes=size,
                object_status="absent",
                error_code="FILE_TOO_LARGE",
                finished_at=utc_now(),
                duration_ms=duration(request),
            )
            raise APIError(
                413, "FILE_TOO_LARGE", "文件不能超过 200 MiB", task_id=task_id
            )
        if size == 0:
            await database_call(
                runtime.database.update_task,
                task_id,
                status="failed",
                size_bytes=0,
                object_status="absent",
                error_code="FILE_EMPTY",
                finished_at=utc_now(),
                duration_ms=duration(request),
            )
            raise APIError(400, "FILE_EMPTY", "文件不能为空", task_id=task_id)
        try:
            await anyio.to_thread.run_sync(
                provider.upload_file, source.file, object_key, content_type
            )
        except ProviderError as exc:
            status = "unknown" if exc.uncertain else "failed"
            await database_call(
                runtime.database.update_task,
                task_id,
                status=status,
                size_bytes=size,
                object_status="pending" if exc.uncertain else "absent",
                error_code=exc.code,
                finished_at=utc_now() if status == "failed" else None,
                duration_ms=duration(request),
            )
            raise APIError(502, exc.code, exc.message, task_id=task_id) from exc
        try:
            metadata = await anyio.to_thread.run_sync(provider.head_object, object_key)
            if metadata is None:
                raise ProviderError(
                    "UPLOAD_CONFIRMATION_PENDING",
                    "上传已返回成功，但暂时无法确认远端对象",
                    uncertain=True,
                )
            require_upload_metadata(metadata, size)
        except ProviderError as exc:
            await database_call(
                runtime.database.update_task,
                task_id,
                status="unknown",
                size_bytes=size,
                object_status="pending",
                error_code=exc.code,
                duration_ms=duration(request),
            )
            raise APIError(502, exc.code, exc.message, task_id=task_id) from exc
        finished_duration = duration(request)
        delete_token, delete_token_hash = issue_delete_token()
        try:
            await database_call(
                runtime.database.update_task,
                task_id,
                status="succeeded",
                size_bytes=metadata.size_bytes,
                etag=metadata.etag,
                version_id=metadata.version_id,
                delete_token_hash=delete_token_hash,
                object_status="present",
                error_code=None,
                finished_at=utc_now(),
                duration_ms=finished_duration,
            )
        except sqlite3.Error as exc:
            runtime.log.emit(
                50,
                "database_error",
                "对象已上传但任务状态写入失败",
                request_id=task["request_id"],
                task_id=task_id,
                error_code="DATABASE_ERROR",
            )
            raise APIError(
                500,
                "DATABASE_ERROR",
                "对象已上传但任务状态写入失败",
                task_id=task_id,
            ) from exc
        runtime.log.emit(
            NOTIFY,
            "upload_succeeded",
            "上传任务成功",
            request_id=task["request_id"],
            task_id=task_id,
            details={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size,
                "object_key": object_key,
                "duration_ms": finished_duration,
                "storage_preset": snapshot.preset_key,
                "storage_provider": snapshot.provider_id,
                "storage_config_revision": snapshot.revision,
            },
        )
        task.update(
            storage_preset=snapshot.preset_key,
            size_bytes=metadata.size_bytes,
            etag=metadata.etag,
            version_id=metadata.version_id,
        )
        return JSONResponse(
            _upload_response(task, delete_token),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )
    finally:
        await form.close()
