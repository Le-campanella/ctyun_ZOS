from __future__ import annotations

import sqlite3
from typing import Any
from uuid import UUID

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse

from .database import utc_now
from .eventlog import NOTIFY
from .http import APIError, database_call, error_body, no_store, request_id
from .providers import ProviderError, matches_object_metadata
from .runtime import Runtime
from .security import matches_delete_token


def _deleted_response(
    task: dict[str, Any], *, already_deleted: bool, already_absent: bool
) -> JSONResponse:
    return no_store(
        {
            "task_id": task["id"],
            "key": task["object_key"],
            "object_status": "deleted",
            "deleted_at": task["deleted_at"],
            "already_deleted": already_deleted,
            "already_absent": already_absent,
        }
    )


def _deletion_gate(task: dict[str, Any]) -> JSONResponse | None:
    if task["object_status"] == "deleted":
        return _deleted_response(task, already_deleted=True, already_absent=False)
    if task["object_status"] in {"deleting", "delete_unknown"}:
        raise APIError(
            409, "DELETE_IN_PROGRESS", "对象正在删除或等待确认", task_id=task["id"]
        )
    if task["status"] != "succeeded" or task["object_status"] != "present":
        raise APIError(
            409, "OBJECT_NOT_DELETABLE", "任务当前不允许严格删除", task_id=task["id"]
        )
    return None


async def _save_deletion(
    runtime: Runtime,
    current_request_id: str,
    task: dict[str, Any],
    provider_result: str,
    **changes: Any,
) -> None:
    try:
        await database_call(runtime.database.update_task, task["id"], **changes)
    except sqlite3.Error as exc:
        runtime.log.emit(
            50,
            "database_error",
            "对象删除结果写入失败",
            request_id=current_request_id,
            task_id=task["id"],
            error_code="DATABASE_ERROR",
        )
        raise APIError(
            500,
            "DATABASE_ERROR",
            "对象删除状态无法安全写入",
            task_id=task["id"],
        ) from exc
    to_status = changes["object_status"]
    error_code = changes.get("delete_error_code")
    event = (
        "object_delete_succeeded"
        if to_status == "deleted"
        else "object_delete_pending"
        if to_status == "delete_unknown"
        else "object_delete_changed"
        if error_code == "OBJECT_CHANGED"
        else "object_delete_failed"
    )
    runtime.log.emit(
        NOTIFY,
        event,
        "对象删除状态已更新",
        request_id=current_request_id,
        task_id=task["id"],
        error_code=error_code,
        details={
            "from_status": "deleting",
            "to_status": to_status,
            "provider_result": provider_result,
            "object_key": task["object_key"],
            "size_bytes": task["size_bytes"],
            "etag": task["etag"],
            "version_id": task["version_id"],
            "storage_config_id": task["storage_config_id"],
        },
    )


async def _delete_pending(
    runtime: Runtime,
    request: Request,
    task: dict[str, Any],
    provider_result: str = "uncertain",
) -> JSONResponse:
    await _save_deletion(
        runtime,
        request_id(request),
        task,
        provider_result,
        object_status="delete_unknown",
        delete_error_code="DELETE_PENDING",
    )
    body = error_body("DELETE_PENDING", "删除结果暂时无法确认", request_id(request))
    return no_store(
        {
            "task_id": task["id"],
            "key": task["object_key"],
            "object_status": "delete_unknown",
            "error": body["error"],
        },
        status_code=202,
    )


async def delete_task_object(
    request: Request,
    runtime: Runtime,
    task_id: str,
    delete_token: str | None,
):
    try:
        UUID(task_id)
    except ValueError as exc:
        raise APIError(400, "BAD_REQUEST", "任务 ID 不合法") from exc
    if request.headers.get("transfer-encoding") or request.headers.get(
        "content-length"
    ) not in {None, "0"}:
        raise APIError(400, "BAD_REQUEST", "删除请求不能包含请求体")
    task = await database_call(runtime.database.task_by_id, task_id)
    if task is None:
        raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
    admin_cleanup = request.url.path.startswith("/v1/admin/")
    deletable_status = "present_unclaimed" if admin_cleanup else "present"
    if admin_cleanup:
        if (
            task["status"] != "succeeded"
            or task["object_status"] != "present_unclaimed"
            or task.get("delete_token_hash") is not None
        ):
            raise APIError(
                409,
                "OBJECT_NOT_UNCLAIMED",
                "任务不是可管理清理的无凭证对象",
                task_id=task_id,
            )
    else:
        if not matches_delete_token(delete_token, task.get("delete_token_hash")):
            raise APIError(403, "DELETE_TOKEN_INVALID", "删除凭证无效", task_id=task_id)
        existing = _deletion_gate(task)
        if existing:
            return existing
    try:
        claim = (
            runtime.database.claim_unclaimed_deletion
            if admin_cleanup
            else runtime.database.claim_task_deletion
        )
        claimed = await database_call(claim, task_id, request_id(request), utc_now())
    except sqlite3.Error as exc:
        raise APIError(
            500, "DATABASE_ERROR", "无法开始对象删除", task_id=task_id
        ) from exc
    if not claimed:
        current = await database_call(runtime.database.task_by_id, task_id)
        if current is None:
            raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
        if admin_cleanup:
            raise APIError(
                409,
                "OBJECT_NOT_UNCLAIMED",
                "无凭证对象状态已改变",
                task_id=task_id,
            )
        result = _deletion_gate(current)
        if result:
            return result
        raise APIError(409, "DELETE_IN_PROGRESS", "对象删除状态已改变", task_id=task_id)
    runtime.log.emit(
        NOTIFY,
        "object_delete_started",
        "对象删除已开始",
        request_id=request_id(request),
        task_id=task_id,
        details={
            "from_status": deletable_status,
            "to_status": "deleting",
            "provider_result": None,
            "object_key": task["object_key"],
            "size_bytes": task["size_bytes"],
            "etag": task["etag"],
            "version_id": task["version_id"],
            "storage_config_id": task["storage_config_id"],
        },
    )
    try:
        provider = runtime.provider_for_config(task["storage_config_id"])
    except Exception as exc:
        await _save_deletion(
            runtime,
            request_id(request),
            task,
            "config_unavailable",
            object_status=deletable_status,
            delete_error_code="STORAGE_CONFIG_UNAVAILABLE",
        )
        raise APIError(
            503,
            "STORAGE_CONFIG_UNAVAILABLE",
            "任务原存储配置不可用",
            task_id=task_id,
        ) from exc
    try:
        metadata = await anyio.to_thread.run_sync(
            provider.head_object, task["object_key"], task["version_id"]
        )
    except ProviderError as exc:
        if exc.uncertain:
            return await _delete_pending(runtime, request, task, exc.code)
        await _save_deletion(
            runtime,
            request_id(request),
            task,
            exc.code,
            object_status=deletable_status,
            delete_error_code="DELETE_FAILED",
        )
        raise APIError(
            502, "DELETE_FAILED", "无法读取待删除对象", task_id=task_id
        ) from exc
    if metadata is None:
        deleted_at = utc_now()
        await _save_deletion(
            runtime,
            request_id(request),
            task,
            "already_absent",
            object_status="deleted",
            delete_error_code=None,
            deleted_at=deleted_at,
        )
        return _deleted_response(
            task | {"deleted_at": deleted_at},
            already_deleted=False,
            already_absent=True,
        )
    if not matches_object_metadata(
        metadata, task["size_bytes"], task["etag"], task["version_id"]
    ):
        await _save_deletion(
            runtime,
            request_id(request),
            task,
            "metadata_mismatch",
            object_status=deletable_status,
            delete_error_code="OBJECT_CHANGED",
        )
        raise APIError(
            409, "OBJECT_CHANGED", "远端对象与上传记录不一致", task_id=task_id
        )
    try:
        await anyio.to_thread.run_sync(
            provider.delete_object, task["object_key"], task["version_id"]
        )
    except ProviderError as exc:
        if exc.uncertain:
            return await _delete_pending(runtime, request, task, exc.code)
        await _save_deletion(
            runtime,
            request_id(request),
            task,
            exc.code,
            object_status=deletable_status,
            delete_error_code="DELETE_FAILED",
        )
        raise APIError(
            502, "DELETE_FAILED", "Storage Provider 拒绝删除", task_id=task_id
        ) from exc
    try:
        remaining = await anyio.to_thread.run_sync(
            provider.head_object, task["object_key"], task["version_id"]
        )
    except ProviderError:
        return await _delete_pending(runtime, request, task, "post_delete_head_error")
    if remaining is not None:
        if not matches_object_metadata(
            remaining, task["size_bytes"], task["etag"], task["version_id"]
        ):
            return await _delete_pending(
                runtime, request, task, "post_delete_object_changed"
            )
        await _save_deletion(
            runtime,
            request_id(request),
            task,
            "still_present",
            object_status=deletable_status,
            delete_error_code="DELETE_FAILED",
        )
        raise APIError(502, "DELETE_FAILED", "删除后对象仍然存在", task_id=task_id)
    deleted_at = utc_now()
    await _save_deletion(
        runtime,
        request_id(request),
        task,
        "confirmed_absent",
        object_status="deleted",
        delete_error_code=None,
        deleted_at=deleted_at,
    )
    return _deleted_response(
        task | {"deleted_at": deleted_at},
        already_deleted=False,
        already_absent=False,
    )
