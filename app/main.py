from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import anyio
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .database import (
    Database,
    DefaultPresetConflict,
    PresetNotFound,
    PresetStateConflict,
    RevisionConflict,
    utc_now,
)
from .deletions import delete_task_object as process_delete_task_object
from .http import (
    APIError,
    RequestGuardMiddleware,
    error_body,
)
from .http import (
    admin_path as _admin_path,
)
from .http import (
    database_call as _database_call,
)
from .http import (
    no_store as _no_store,
)
from .http import (
    request_id as _request_id,
)
from .models import (
    DashboardLogsResponse,
    DashboardStorageResponse,
    DashboardSummaryResponse,
    DashboardTrafficResponse,
    DeleteObjectResponse,
    HealthResponse,
    ProviderSchemasResponse,
    ReadyResponse,
    ReceiveValidationResponse,
    StorageDefaultRequest,
    StoragePresetCreateRequest,
    StoragePresetDetailResponse,
    StoragePresetListResponse,
    StoragePresetPatchRequest,
    StoragePresetSaveResponse,
    StorageSaveResponse,
    StorageSettingsResponse,
    StorageTestRequest,
    StorageTestResponse,
    StorageUpdateRequest,
    TaskDetailResponse,
    TaskListResponse,
    UploadResponse,
)
from .providers import (
    ProviderError,
    ProviderRegistry,
    default_registry,
)
from .runtime import Runtime
from .uploads import (
    preset_key as _preset_key,
)
from .uploads import (
    provider_status as _provider_status,
)
from .uploads import (
    upload as process_upload,
)
from .uploads import (
    validate_upload as process_validate_upload,
)

STATUS_VALUES = {"uploading", "unknown", "succeeded", "failed"}
LEVELS = {"NOTIFY": 25, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _parse_time(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise APIError(400, "BAD_REQUEST", f"{name} 时间格式不合法") from exc
    if parsed.tzinfo is None:
        raise APIError(400, "BAD_REQUEST", f"{name} 必须包含时区")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time_range(request: Request, *, maximum_days: int = 31) -> tuple[str, str]:
    now = datetime.now(UTC)
    start = _parse_time(request.query_params.get("from"), "from") or now - timedelta(
        hours=24
    )
    end = _parse_time(request.query_params.get("to"), "to") or now
    if end <= start or end - start > timedelta(days=maximum_days):
        raise APIError(400, "BAD_REQUEST", "时间范围不合法")
    return _iso(start), _iso(end)


def _task_item(task: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    item = {
        "id": task["id"],
        "request_id": task["request_id"],
        "client_id": task.get("client_id", "legacy"),
        "storage_preset": task["storage_preset"],
        "storage_provider": task.get("storage_provider"),
        "storage_config_revision": task.get("storage_config_revision"),
        "filename": task["filename"],
        "content_type": task["content_type"],
        "object_key": task["object_key"],
        "public_url": task["public_url"] if task["status"] == "succeeded" else None,
        "status": task["status"],
        "size_bytes": task["size_bytes"],
        "etag": task.get("etag"),
        "version_id": task.get("version_id"),
        "object_status": task.get("object_status", "pending"),
        "delete_capability_available": task.get("delete_token_hash") is not None,
        "delete_error_code": task.get("delete_error_code"),
        "delete_started_at": task.get("delete_started_at"),
        "deleted_at": task.get("deleted_at"),
        "error_code": task["error_code"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
        "duration_ms": task["duration_ms"],
    }
    if detail:
        item["idempotency_key"] = task["idempotency_key"]
    return item


def _settings_request(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise APIError(400, "STORAGE_CONFIG_INVALID", "设置请求必须使用 JSON")
    if request.headers.get("x-settings-request") != "true":
        raise APIError(400, "STORAGE_CONFIG_INVALID", "缺少设置请求确认 Header")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        actual = f"{parsed.scheme}://{parsed.netloc}"
        if actual != expected:
            raise APIError(400, "STORAGE_CONFIG_INVALID", "设置请求 Origin 不合法")


def create_app(
    settings: Settings | None = None,
    registry: ProviderRegistry | None = None,
    database: Database | None = None,
    *,
    background: bool = True,
) -> FastAPI:
    resolved = settings

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal resolved
        resolved = resolved or Settings.from_env()
        runtime = Runtime(resolved, registry or default_registry(), database)
        application.state.runtime = runtime
        await runtime.start(background=background)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="ZOS Upload Service", version="1.0.0", lifespan=lifespan)
    web_root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=web_root / "templates")
    app.mount("/static", StaticFiles(directory=web_root / "static"), name="static")
    guard_settings = Settings(
        encryption_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        admin_api_keys=("guard-admin-key-0000000000000000",),
    )
    app.add_middleware(
        RequestGuardMiddleware, settings=lambda: resolved or guard_settings
    )

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError):
        return JSONResponse(
            error_body(exc.code, exc.message, _request_id(request), exc.task_id),
            status_code=exc.status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        preset_invalid = any(
            error["loc"][-1] in {"preset_key", "expected_default_preset"}
            for error in exc.errors()
        )
        return JSONResponse(
            error_body(
                "STORAGE_PRESET_INVALID" if preset_invalid else "BAD_REQUEST",
                "preset_key 格式不合法" if preset_invalid else "请求参数不合法",
                _request_id(request),
            ),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        runtime: Runtime | None = getattr(request.app.state, "runtime", None)
        if runtime:
            runtime.log.emit(
                40,
                "internal_error",
                "服务内部异常",
                request_id=_request_id(request),
                error_code="INTERNAL_ERROR",
                details={"exception": type(exc).__name__},
            )
        return JSONResponse(
            error_body("INTERNAL_ERROR", "服务内部异常", _request_id(request)),
            status_code=500,
        )

    @app.get("/healthz", response_model=HealthResponse)
    async def health():
        return {"status": "ok"}

    @app.get("/readyz", response_model=ReadyResponse, response_model_exclude_none=True)
    async def ready(request: Request):
        runtime: Runtime = request.app.state.runtime
        is_ready, checks, code = runtime.ready_checks()
        body: dict[str, Any] = {
            "status": "ready" if is_ready else "not_ready",
            "checked_at": utc_now(),
            "checks": checks,
        }
        if not is_ready:
            body["error"] = {
                "code": code,
                "message": "服务依赖项未达到就绪条件",
                "request_id": _request_id(request),
            }
        return JSONResponse(body, status_code=200 if is_ready else 503)

    @app.get("/v1/settings/storage/providers", response_model=ProviderSchemasResponse)
    async def provider_schemas(request: Request):
        runtime: Runtime = request.app.state.runtime
        return {"items": runtime.registry.schemas()}

    @app.get(
        "/v1/settings/storage/presets",
        response_model=StoragePresetListResponse,
    )
    async def storage_presets(request: Request):
        try:
            return _no_store({"items": request.app.state.runtime.storage_presets()})
        except sqlite3.Error as exc:
            raise APIError(500, "DATABASE_ERROR", "无法读取存储预设") from exc

    @app.post(
        "/v1/settings/storage/presets",
        response_model=StoragePresetDetailResponse,
        status_code=201,
    )
    async def create_storage_preset(
        request: Request, payload: StoragePresetCreateRequest
    ):
        _settings_request(request)
        body = payload.model_dump(exclude_none=True)
        preset_key = body.pop("preset_key")
        display_name = body.pop("display_name")
        try:
            if request.app.state.runtime.database.storage_preset_by_key(preset_key):
                raise APIError(409, "PRESET_STATE_CONFLICT", "preset_key 已存在")
            result = await request.app.state.runtime.create_storage_preset(
                preset_key, display_name, body
            )
        except ProviderError as exc:
            raise APIError(_provider_status(exc), exc.code, exc.message) from exc
        except sqlite3.IntegrityError as exc:
            raise APIError(409, "PRESET_STATE_CONFLICT", "preset_key 已存在") from exc
        except sqlite3.Error as exc:
            raise APIError(500, "SETTINGS_STORAGE_ERROR", "无法保存存储预设") from exc
        return _no_store(result, 201)

    @app.get(
        "/v1/settings/storage/presets/{preset_key}",
        response_model=StoragePresetDetailResponse,
    )
    async def storage_preset_detail(request: Request, preset_key: str):
        preset_key = _preset_key(preset_key)
        try:
            result = request.app.state.runtime.storage_preset_detail(preset_key)
        except PresetNotFound as exc:
            raise APIError(404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在") from exc
        except sqlite3.Error as exc:
            raise APIError(500, "DATABASE_ERROR", "无法读取存储预设") from exc
        return _no_store(result)

    @app.put(
        "/v1/settings/storage/presets/{preset_key}",
        response_model=StoragePresetSaveResponse,
    )
    async def save_storage_preset(
        request: Request, preset_key: str, payload: StorageUpdateRequest
    ):
        _settings_request(request)
        preset_key = _preset_key(preset_key)
        try:
            await request.app.state.runtime.activate_storage(
                payload.model_dump(exclude_none=True), preset_key
            )
            result = request.app.state.runtime.storage_preset_detail(preset_key) | {
                "previous_revision": payload.expected_revision
            }
        except PresetNotFound as exc:
            raise APIError(404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在") from exc
        except ProviderError as exc:
            raise APIError(_provider_status(exc), exc.code, exc.message) from exc
        except RevisionConflict as exc:
            raise APIError(
                409,
                "CONFIG_REVISION_CONFLICT",
                f"当前配置 revision 为 {exc.current_revision}",
            ) from exc
        except sqlite3.Error as exc:
            raise APIError(
                500, "SETTINGS_STORAGE_ERROR", "无法更新存储预设配置"
            ) from exc
        return _no_store(result)

    @app.patch(
        "/v1/settings/storage/presets/{preset_key}",
        response_model=StoragePresetDetailResponse,
    )
    async def update_storage_preset(
        request: Request, preset_key: str, payload: StoragePresetPatchRequest
    ):
        _settings_request(request)
        preset_key = _preset_key(preset_key)
        if payload.display_name is None and payload.enabled is None:
            raise APIError(400, "STORAGE_PRESET_INVALID", "至少提交一个可修改字段")
        try:
            request.app.state.runtime.update_storage_preset(
                preset_key,
                payload.expected_state_revision,
                display_name=payload.display_name,
                enabled=payload.enabled,
            )
            result = request.app.state.runtime.storage_preset_detail(preset_key)
        except PresetNotFound as exc:
            raise APIError(404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在") from exc
        except PresetStateConflict as exc:
            raise APIError(
                409,
                "PRESET_STATE_CONFLICT",
                f"当前预设状态 revision 为 {exc.current_revision}",
            ) from exc
        except ValueError as exc:
            raise APIError(
                409, "PRESET_STATE_CONFLICT", "当前默认预设不能禁用"
            ) from exc
        except sqlite3.Error as exc:
            raise APIError(
                500, "SETTINGS_STORAGE_ERROR", "无法更新存储预设状态"
            ) from exc
        return _no_store(result)

    @app.put(
        "/v1/settings/storage/default",
        response_model=StoragePresetDetailResponse,
    )
    async def set_default_storage_preset(
        request: Request, payload: StorageDefaultRequest
    ):
        _settings_request(request)
        try:
            await request.app.state.runtime.set_default_storage_preset(
                payload.preset_key,
                payload.expected_default_preset,
                payload.expected_state_revision,
            )
            result = request.app.state.runtime.storage_preset_detail(payload.preset_key)
        except PresetNotFound as exc:
            raise APIError(404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在") from exc
        except DefaultPresetConflict as exc:
            raise APIError(
                409,
                "DEFAULT_PRESET_CONFLICT",
                f"当前默认预设为 {exc.current_default_preset}",
            ) from exc
        except ValueError as exc:
            raise APIError(409, "DEFAULT_PRESET_CONFLICT", "目标预设未启用") from exc
        except sqlite3.Error as exc:
            raise APIError(500, "SETTINGS_STORAGE_ERROR", "无法切换默认预设") from exc
        return _no_store(result)

    @app.get("/v1/settings/storage", response_model=StorageSettingsResponse)
    async def current_storage(request: Request):
        return _no_store(request.app.state.runtime.current_storage())

    @app.post("/v1/settings/storage/test", response_model=StorageTestResponse)
    async def test_storage(request: Request, payload: StorageTestRequest):
        _settings_request(request)
        body = payload.model_dump(exclude_none=True)
        preset_key = body.pop("preset_key", None)
        try:
            result = await request.app.state.runtime.test_storage(body, preset_key)
        except PresetNotFound as exc:
            raise APIError(404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在") from exc
        except ProviderError as exc:
            raise APIError(_provider_status(exc), exc.code, exc.message) from exc
        return _no_store(result)

    @app.put("/v1/settings/storage", response_model=StorageSaveResponse)
    async def save_storage(request: Request, payload: StorageUpdateRequest):
        _settings_request(request)
        try:
            result = await request.app.state.runtime.activate_storage(
                payload.model_dump(exclude_none=True)
            )
        except ProviderError as exc:
            raise APIError(_provider_status(exc), exc.code, exc.message) from exc
        except RevisionConflict as exc:
            raise APIError(
                409,
                "CONFIG_REVISION_CONFLICT",
                f"当前配置 revision 为 {exc.current_revision}",
            ) from exc
        return _no_store(result)

    @app.post("/v1/uploads/validate", response_model=ReceiveValidationResponse)
    async def validate_upload(
        request: Request,
        x_client_id: str | None = Header(default=None, alias="X-Client-ID"),
        x_client_key: str | None = Header(default=None, alias="X-Client-Key"),
    ):
        del x_client_id, x_client_key
        return await process_validate_upload(request, request.app.state.runtime)

    @app.post("/v1/uploads", response_model=UploadResponse)
    async def upload(
        request: Request,
        x_storage_preset: str | None = Header(default=None, alias="X-Storage-Preset"),
        x_client_id: str | None = Header(default=None, alias="X-Client-ID"),
        x_client_key: str | None = Header(default=None, alias="X-Client-Key"),
    ):
        del x_client_id, x_client_key
        return await process_upload(
            request, request.app.state.runtime, x_storage_preset
        )

    @app.get("/v1/upload-tasks", response_model=TaskListResponse)
    async def tasks(request: Request):
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError as exc:
            raise APIError(400, "BAD_REQUEST", "分页参数不合法") from exc
        status = request.query_params.get("status")
        if (
            not 1 <= limit <= 200
            or offset < 0
            or (status is not None and status not in STATUS_VALUES)
        ):
            raise APIError(400, "BAD_REQUEST", "任务查询参数不合法")
        from_time = _parse_time(request.query_params.get("from"), "from")
        to_time = _parse_time(request.query_params.get("to"), "to")
        if from_time and to_time and to_time <= from_time:
            raise APIError(400, "BAD_REQUEST", "to 必须晚于 from")
        try:
            items = await _database_call(
                request.app.state.runtime.database.list_tasks,
                limit=limit,
                offset=offset,
                status=status,
                from_time=_iso(from_time) if from_time else None,
                to_time=_iso(to_time) if to_time else None,
            )
        except sqlite3.Error as exc:
            raise APIError(500, "DATABASE_ERROR", "无法查询上传任务") from exc
        return {
            "items": [_task_item(item) for item in items],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/upload-tasks/{task_id}", response_model=TaskDetailResponse)
    async def task_detail(request: Request, task_id: str):
        try:
            UUID(task_id)
        except ValueError as exc:
            raise APIError(400, "BAD_REQUEST", "任务 ID 不合法") from exc
        task = await _database_call(
            request.app.state.runtime.database.task_by_id, task_id
        )
        if task is None:
            raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
        return _task_item(task, detail=True)

    @app.delete(
        "/v1/admin/upload-tasks/{task_id}/object",
        response_model=DeleteObjectResponse,
    )
    @app.delete(
        "/v1/upload-tasks/{task_id}/object",
        response_model=DeleteObjectResponse,
    )
    async def delete_task_object(
        request: Request,
        task_id: str,
        x_delete_token: str | None = Header(default=None, alias="X-Delete-Token"),
    ):
        return await process_delete_task_object(
            request,
            request.app.state.runtime,
            task_id,
            x_delete_token,
        )

    @app.get("/v1/dashboard/summary", response_model=DashboardSummaryResponse)
    async def dashboard_summary(request: Request):
        from_time, to_time = _time_range(request)
        runtime: Runtime = request.app.state.runtime
        uploads = await _database_call(runtime.database.summary, from_time, to_time)
        usage = await _database_call(runtime.database.quota_usage)
        uploads["quota"] = {
            "max_objects": runtime.settings.client_max_objects,
            "max_bytes": runtime.settings.client_max_bytes,
            "clients": usage,
            "warning": any(
                (
                    runtime.settings.client_max_objects
                    and item["object_count"]
                    >= runtime.settings.client_max_objects * 0.8
                )
                or (
                    runtime.settings.client_max_bytes
                    and item["size_bytes"] >= runtime.settings.client_max_bytes * 0.8
                )
                for item in usage
            ),
        }
        ready_state, checks, _ = runtime.ready_checks()
        return {
            "range": {"from": from_time, "to": to_time},
            "generated_at": utc_now(),
            "service": {
                "status": "ok" if ready_state else "degraded",
                "ready": ready_state,
                "checks": checks,
            },
            "uploads": uploads,
        }

    @app.get("/v1/dashboard/traffic", response_model=DashboardTrafficResponse)
    async def dashboard_traffic(request: Request):
        from_time, to_time = _time_range(request)
        interval = request.query_params.get("interval", "hour")
        if interval not in {"hour", "day"}:
            raise APIError(400, "BAD_REQUEST", "interval 必须为 hour 或 day")
        runtime: Runtime = request.app.state.runtime
        tasks = await _database_call(
            runtime.database.tasks_in_range, from_time, to_time
        )
        timezone = ZoneInfo(runtime.settings.app_timezone)
        start_utc = _parse_time(from_time, "from")
        end_utc = _parse_time(to_time, "to")
        assert start_utc and end_utc
        cursor = start_utc.astimezone(timezone)
        cursor = cursor.replace(
            minute=0,
            second=0,
            microsecond=0,
            hour=0 if interval == "day" else cursor.hour,
        )
        buckets: dict[datetime, dict[str, int]] = {}
        for task in tasks:
            created = _parse_time(task["created_at"], "created_at")
            assert created
            local_created = created.astimezone(timezone)
            bucket = local_created.replace(
                minute=0,
                second=0,
                microsecond=0,
                hour=0 if interval == "day" else local_created.hour,
            )
            totals = buckets.setdefault(
                bucket,
                {
                    "attempt_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "uploading_count": 0,
                    "unknown_count": 0,
                    "successful_upload_bytes": 0,
                },
            )
            totals["attempt_count"] += 1
            key = {
                "succeeded": "success_count",
                "failed": "failure_count",
                "uploading": "uploading_count",
                "unknown": "unknown_count",
            }[task["status"]]
            totals[key] += 1
            if task["status"] == "succeeded":
                totals["successful_upload_bytes"] += task["size_bytes"] or 0
        points = []
        while cursor.astimezone(UTC) < end_utc:
            next_cursor = cursor + (
                timedelta(hours=1) if interval == "hour" else timedelta(days=1)
            )
            point = {
                "start": _iso(cursor),
                "end": _iso(next_cursor),
                **buckets.get(
                    cursor,
                    {
                        "attempt_count": 0,
                        "success_count": 0,
                        "failure_count": 0,
                        "uploading_count": 0,
                        "unknown_count": 0,
                        "successful_upload_bytes": 0,
                    },
                ),
            }
            points.append(point)
            cursor = next_cursor
        return {
            "range": {"from": from_time, "to": to_time},
            "interval": interval,
            "aggregation_timezone": runtime.settings.app_timezone,
            "generated_at": utc_now(),
            "points": points,
        }

    @app.get("/v1/dashboard/logs", response_model=DashboardLogsResponse)
    async def dashboard_logs(request: Request):
        level_name = request.query_params.get("min_level", "NOTIFY")
        if level_name not in LEVELS:
            raise APIError(400, "BAD_REQUEST", "日志级别不合法")
        try:
            limit = int(request.query_params.get("limit", "100"))
            before = request.query_params.get("before_id")
            before_id = int(before) if before else None
        except ValueError as exc:
            raise APIError(400, "BAD_REQUEST", "日志分页参数不合法") from exc
        if not 1 <= limit <= 500 or (before_id is not None and before_id < 1):
            raise APIError(400, "BAD_REQUEST", "日志分页参数不合法")
        from_time = _parse_time(request.query_params.get("from"), "from")
        to_time = _parse_time(request.query_params.get("to"), "to")
        filters = {
            key: request.query_params.get(key)
            for key in ("event", "request_id", "task_id", "error_code")
        }
        items = await _database_call(
            request.app.state.runtime.database.list_logs,
            min_level=LEVELS[level_name],
            limit=limit,
            before_id=before_id,
            filters=filters,
            from_time=_iso(from_time) if from_time else None,
            to_time=_iso(to_time) if to_time else None,
        )
        return {
            "items": items,
            "limit": limit,
            "before_id": before_id,
            "next_before_id": items[-1]["id"] if len(items) == limit else None,
        }

    @app.get("/v1/dashboard/storage", response_model=DashboardStorageResponse)
    async def dashboard_storage(request: Request):
        from_time, to_time = _time_range(request)
        runtime: Runtime = request.app.state.runtime
        snapshot = runtime.active_snapshot()
        if snapshot is None:
            raise APIError(503, "STORAGE_NOT_CONFIGURED", "尚未激活存储配置")
        try:
            result = await anyio.to_thread.run_sync(
                snapshot.provider.get_metrics, from_time, to_time
            )
        except ProviderError as exc:
            raise APIError(503, exc.code, exc.message) from exc
        return {
            **result,
            "provider": snapshot.provider_id,
            "provider_schema_version": snapshot.provider_schema_version,
            "storage_config_revision": snapshot.revision,
            "range": {"from": from_time, "to": to_time},
            "cache": None,
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/dashboard/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        response = templates.TemplateResponse(request, "settings.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    def secured_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes.update(
            AdminBearer={"type": "http", "scheme": "bearer"},
            AdminBasic={"type": "http", "scheme": "basic"},
            AdminKeyHeader={
                "type": "apiKey",
                "in": "header",
                "name": "X-Admin-Key",
            },
            ClientIdHeader={
                "type": "apiKey",
                "in": "header",
                "name": "X-Client-ID",
            },
            ClientKeyHeader={
                "type": "apiKey",
                "in": "header",
                "name": "X-Client-Key",
            },
        )
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                if method.upper() in {
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                } and _admin_path(path, method.upper()):
                    operation["security"] = [
                        {"AdminBearer": []},
                        {"AdminBasic": []},
                        {"AdminKeyHeader": []},
                    ]
                elif method.upper() == "POST" and path in {
                    "/v1/uploads",
                    "/v1/uploads/validate",
                }:
                    operation["security"] = [
                        {"ClientIdHeader": [], "ClientKeyHeader": []},
                        {"AdminBasic": []},
                        {"AdminBearer": []},
                        {"AdminKeyHeader": []},
                    ]
        app.openapi_schema = schema
        return schema

    app.openapi = secured_openapi  # type: ignore[method-assign]

    return app


app = create_app()
