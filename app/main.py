from __future__ import annotations

import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import SpooledTemporaryFile
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import anyio
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from .config import Settings
from .database import (
    Database,
    DefaultPresetConflict,
    PresetNotFound,
    PresetStateConflict,
    RevisionConflict,
    utc_now,
)
from .eventlog import NOTIFY
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
    matches_object_metadata,
    require_upload_metadata,
)
from .runtime import Runtime
from .security import issue_delete_token, matches_delete_token


STATUS_VALUES = {"uploading", "unknown", "succeeded", "failed"}
LEVELS = {"NOTIFY": 25, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")
SAFE_PRESET_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class APIError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        task_id: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.code = code
        self.message = message
        self.task_id = task_id
        self.headers = headers or {}


class BodyTooLarge(Exception):
    pass


class FileTooLarge(Exception):
    def __init__(self, observed: int):
        self.observed = observed


def error_body(
    code: str, message: str, request_id: str, task_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message, "request_id": request_id}
    }
    if task_id:
        body["task_id"] = task_id
    return body


class RequestGuardMiddleware:
    def __init__(self, app, settings: Callable[[], Settings]):
        self.app = app
        self.settings = settings
        self.active_uploads = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id")
        request_id = supplied.decode("latin1") if supplied else str(uuid4())
        if not 1 <= len(request_id) <= 128 or any(
            ord(char) < 32 or ord(char) > 126 for char in request_id
        ):
            request_id = str(uuid4())
            await self._response(
                scope,
                receive,
                send,
                400,
                error_body("BAD_REQUEST", "X-Request-ID 不合法", request_id),
                request_id,
            )
            return
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["started_at"] = monotonic()
        is_upload = (
            scope["method"] == "POST"
            and scope["path"].rstrip("/")
            in {"/v1/uploads", "/v1/uploads/validate"}
        )
        acquired = False
        if is_upload:
            settings = self.settings()
            if self.active_uploads >= settings.max_concurrent_uploads:
                await self._response(
                    scope,
                    receive,
                    send,
                    503,
                    error_body(
                        "UPLOAD_CAPACITY_EXCEEDED", "上传并发容量已满", request_id
                    ),
                    request_id,
                    {"Retry-After": "5"},
                )
                return
            self.active_uploads += 1
            acquired = True
            content_length = headers.get(b"content-length")
            if content_length:
                try:
                    too_large = (
                        int(content_length) > settings.max_request_body_bytes
                    )
                except ValueError:
                    too_large = True
                if too_large:
                    self.active_uploads -= 1
                    await self._response(
                        scope,
                        receive,
                        send,
                        413,
                        error_body(
                            "FILE_TOO_LARGE", "请求体超过允许上限", request_id
                        ),
                        request_id,
                    )
                    return
        total = 0
        started = False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if is_upload and message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > settings.max_request_body_bytes:
                    raise BodyTooLarge
            return message

        async def response_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("ascii"))
                )
            await send(message)

        try:
            await self.app(scope, limited_receive, response_send)
        except BodyTooLarge:
            if not started:
                await self._response(
                    scope,
                    receive,
                    send,
                    413,
                    error_body("FILE_TOO_LARGE", "请求体超过允许上限", request_id),
                    request_id,
                )
        finally:
            if acquired:
                self.active_uploads -= 1

    async def _response(
        self,
        scope,
        receive,
        send,
        status: int,
        body: dict,
        request_id: str,
        headers: dict[str, str] | None = None,
    ):
        response = JSONResponse(body, status, headers=headers)
        response.headers["X-Request-ID"] = request_id
        await response(scope, receive, send)


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


def _copy_upload(source, destination, chunk_size: int, maximum: int) -> int:
    source.seek(0)
    size = 0
    while chunk := source.read(chunk_size):
        size += len(chunk)
        if size > maximum:
            raise FileTooLarge(size)
        destination.write(chunk)
    destination.seek(0)
    return size


async def _upload_file(request: Request, runtime: Runtime) -> UploadFile:
    if not request.headers.get("content-type", "").startswith(
        "multipart/form-data"
    ):
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
    files = form.getlist("file")
    if not files:
        raise APIError(400, "FILE_REQUIRED", "缺少 file 字段")
    if len(files) != 1 or not isinstance(files[0], UploadFile):
        raise APIError(400, "BAD_REQUEST", "每次请求只能上传一个文件")
    return files[0]


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


def _request_id(request: Request) -> str:
    return request.state.request_id


def _duration(request: Request) -> int:
    return round((monotonic() - request.state.started_at) * 1_000)


def _task_item(task: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    item = {
        "id": task["id"],
        "request_id": task["request_id"],
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


def _upload_response(
    task: dict[str, Any], delete_token: str | None = None
) -> dict[str, Any]:
    return {
        "task_id": task["id"],
        "storage_preset": task["storage_preset"],
        "key": task["object_key"],
        "url": task["public_url"],
        "size_bytes": task["size_bytes"],
        "content_type": task["content_type"],
        "etag": task["etag"],
        "version_id": task["version_id"],
        "delete_token": delete_token,
    }


def _provider_status(error: ProviderError) -> int:
    if error.code == "STORAGE_PRESET_NOT_FOUND":
        return 404
    if error.code == "STORAGE_PRESET_DISABLED":
        return 409
    if error.code in {"STORAGE_CONFIG_INVALID", "STORAGE_CREDENTIALS_REQUIRED"}:
        return 400
    if error.code in {
        "STORAGE_DEFAULT_NOT_CONFIGURED",
        "STORAGE_METRICS_UNAVAILABLE",
        "STORAGE_NOT_CONFIGURED",
    }:
        return 503
    return 502


def _existing_upload(
    task: dict[str, Any], requested_preset: str | None
) -> JSONResponse:
    if (
        requested_preset is not None
        and requested_preset != task["storage_preset"]
    ):
        raise APIError(
            409,
            "IDEMPOTENCY_SCOPE_MISMATCH",
            "该幂等键已绑定其他存储预设",
            task_id=task["id"],
        )
    if task["status"] == "succeeded":
        response = _no_store(_upload_response(task))
        response.headers["Idempotency-Replayed"] = "true"
        return response
    code = (
        "IDEMPOTENCY_KEY_REUSED"
        if task["status"] == "failed"
        else "UPLOAD_IN_PROGRESS"
    )
    raise APIError(
        409,
        code,
        "该幂等键已经绑定上传任务",
        task_id=task["id"],
    )


def _deleted_response(
    task: dict[str, Any], *, already_deleted: bool, already_absent: bool
) -> JSONResponse:
    return _no_store(
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
        return _deleted_response(
            task, already_deleted=True, already_absent=False
        )
    if task["object_status"] in {"deleting", "delete_unknown"}:
        raise APIError(
            409,
            "DELETE_IN_PROGRESS",
            "对象正在删除或等待确认",
            task_id=task["id"],
        )
    if task["status"] != "succeeded" or task["object_status"] != "present":
        raise APIError(
            409,
            "OBJECT_NOT_DELETABLE",
            "任务当前不允许严格删除",
            task_id=task["id"],
        )
    return None


def _save_deletion(
    runtime: Runtime,
    request_id: str,
    task: dict[str, Any],
    provider_result: str,
    **changes: Any,
) -> None:
    try:
        runtime.database.update_task(task["id"], **changes)
    except sqlite3.Error as exc:
        runtime.log.emit(
            50,
            "database_error",
            "对象删除结果写入失败",
            request_id=request_id,
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
        request_id=request_id,
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


def _delete_pending(
    runtime: Runtime,
    request: Request,
    task: dict[str, Any],
    provider_result: str = "uncertain",
) -> JSONResponse:
    _save_deletion(
        runtime,
        _request_id(request),
        task,
        provider_result,
        object_status="delete_unknown",
        delete_error_code="DELETE_PENDING",
    )
    body = error_body(
        "DELETE_PENDING",
        "删除结果暂时无法确认",
        _request_id(request),
    )
    return _no_store(
        {
            "task_id": task["id"],
            "key": task["object_key"],
            "object_status": "delete_unknown",
            "error": body["error"],
        },
        status_code=202,
    )


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


def _preset_key(value: str) -> str:
    if not SAFE_PRESET_KEY.fullmatch(value):
        raise APIError(400, "STORAGE_PRESET_INVALID", "preset_key 格式不合法")
    return value


def _no_store(body: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        body, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


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

    @app.get(
        "/v1/settings/storage/providers", response_model=ProviderSchemasResponse
    )
    async def provider_schemas(request: Request):
        runtime: Runtime = request.app.state.runtime
        return {"items": runtime.registry.schemas()}

    @app.get(
        "/v1/settings/storage/presets",
        response_model=StoragePresetListResponse,
    )
    async def storage_presets(request: Request):
        try:
            return _no_store(
                {"items": request.app.state.runtime.storage_presets()}
            )
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
            raise APIError(
                500, "SETTINGS_STORAGE_ERROR", "无法保存存储预设"
            ) from exc
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
            raise APIError(
                404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在"
            ) from exc
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
            result = (
                request.app.state.runtime.storage_preset_detail(preset_key)
                | {"previous_revision": payload.expected_revision}
            )
        except PresetNotFound as exc:
            raise APIError(
                404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在"
            ) from exc
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
            raise APIError(
                400, "STORAGE_PRESET_INVALID", "至少提交一个可修改字段"
            )
        try:
            request.app.state.runtime.update_storage_preset(
                preset_key,
                payload.expected_state_revision,
                display_name=payload.display_name,
                enabled=payload.enabled,
            )
            result = request.app.state.runtime.storage_preset_detail(preset_key)
        except PresetNotFound as exc:
            raise APIError(
                404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在"
            ) from exc
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
            result = request.app.state.runtime.storage_preset_detail(
                payload.preset_key
            )
        except PresetNotFound as exc:
            raise APIError(
                404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在"
            ) from exc
        except DefaultPresetConflict as exc:
            raise APIError(
                409,
                "DEFAULT_PRESET_CONFLICT",
                f"当前默认预设为 {exc.current_default_preset}",
            ) from exc
        except ValueError as exc:
            raise APIError(
                409, "DEFAULT_PRESET_CONFLICT", "目标预设未启用"
            ) from exc
        except sqlite3.Error as exc:
            raise APIError(
                500, "SETTINGS_STORAGE_ERROR", "无法切换默认预设"
            ) from exc
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
            result = await request.app.state.runtime.test_storage(
                body, preset_key
            )
        except PresetNotFound as exc:
            raise APIError(
                404, "STORAGE_PRESET_NOT_FOUND", "存储预设不存在"
            ) from exc
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
    async def validate_upload(request: Request):
        runtime: Runtime = request.app.state.runtime
        source = await _upload_file(request, runtime)
        filename = _filename(source.filename)
        content_type = _content_type(source.content_type)
        spool = SpooledTemporaryFile(
            max_size=runtime.settings.upload_spool_threshold_bytes,
            mode="w+b",
            dir=runtime.settings.temp_dir,
        )
        try:
            try:
                size = await anyio.to_thread.run_sync(
                    _copy_upload,
                    source.file,
                    spool,
                    runtime.settings.upload_read_chunk_bytes,
                    runtime.settings.max_upload_bytes,
                )
            except FileTooLarge as exc:
                raise APIError(
                    413, "FILE_TOO_LARGE", "文件不能超过 200 MiB"
                ) from exc
            if size == 0:
                raise APIError(400, "FILE_EMPTY", "文件不能为空")
            return {
                "received": True,
                "uploaded_to_storage": False,
                "recorded_as_task": False,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size,
                "request_id": _request_id(request),
            }
        finally:
            spool.close()
            await source.close()

    @app.post("/v1/uploads", response_model=UploadResponse)
    async def upload(
        request: Request,
        x_storage_preset: str | None = Header(
            default=None, alias="X-Storage-Preset"
        ),
    ):
        runtime: Runtime = request.app.state.runtime
        idempotency = request.headers.get("idempotency-key")
        if idempotency is not None:
            if not 1 <= len(idempotency) <= 128 or any(
                ord(char) < 33 or ord(char) > 126 for char in idempotency
            ):
                raise APIError(400, "BAD_REQUEST", "Idempotency-Key 不合法")
            existing = runtime.database.task_by_idempotency(idempotency)
            if existing:
                return _existing_upload(existing, x_storage_preset)
        if x_storage_preset is not None:
            x_storage_preset = _preset_key(x_storage_preset)
        try:
            snapshot = runtime.resolve_upload_snapshot(x_storage_preset)
        except ProviderError as exc:
            raise APIError(
                _provider_status(exc), exc.code, exc.message
            ) from exc
        provider = snapshot.provider
        source = await _upload_file(request, runtime)
        filename = _filename(source.filename)
        content_type = _content_type(source.content_type)
        task_id = str(uuid4())
        date = datetime.now(ZoneInfo(runtime.settings.app_timezone)).strftime(
            "%Y/%m/%d"
        )
        extension = _extension(filename)
        object_key = f"{date}/{task_id}{'.' + extension if extension else ''}"
        public_url = provider.build_public_url(object_key)
        task = {
            "id": task_id,
            "request_id": _request_id(request),
            "idempotency_key": idempotency,
            "storage_config_id": snapshot.storage_config_id,
            "filename": filename,
            "content_type": content_type,
            "object_key": object_key,
            "public_url": public_url,
            "status": "uploading",
            "size_bytes": None,
            "error_code": None,
            "created_at": utc_now(),
            "finished_at": None,
            "duration_ms": None,
        }
        try:
            runtime.database.create_task(task)
        except sqlite3.IntegrityError:
            existing = runtime.database.task_by_idempotency(idempotency)
            if existing:
                return _existing_upload(existing, snapshot.preset_key)
            raise APIError(
                409,
                "UPLOAD_IN_PROGRESS",
                "该幂等键已经绑定上传任务",
                task_id=existing["id"] if existing else None,
            )
        except sqlite3.Error as exc:
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
        spool = SpooledTemporaryFile(
            max_size=runtime.settings.upload_spool_threshold_bytes,
            mode="w+b",
            dir=runtime.settings.temp_dir,
        )
        try:
            try:
                size = await anyio.to_thread.run_sync(
                    _copy_upload,
                    source.file,
                    spool,
                    runtime.settings.upload_read_chunk_bytes,
                    runtime.settings.max_upload_bytes,
                )
            except FileTooLarge as exc:
                runtime.database.update_task(
                    task_id,
                    status="failed",
                    size_bytes=None,
                    error_code="FILE_TOO_LARGE",
                    finished_at=utc_now(),
                    duration_ms=_duration(request),
                )
                raise APIError(
                    413,
                    "FILE_TOO_LARGE",
                    "文件不能超过 200 MiB",
                    task_id=task_id,
                ) from exc
            if size == 0:
                runtime.database.update_task(
                    task_id,
                    status="failed",
                    size_bytes=0,
                    error_code="FILE_EMPTY",
                    finished_at=utc_now(),
                    duration_ms=_duration(request),
                )
                raise APIError(400, "FILE_EMPTY", "文件不能为空", task_id=task_id)
            try:
                await anyio.to_thread.run_sync(
                    provider.upload_file, spool, object_key, content_type
                )
            except ProviderError as exc:
                status = "unknown" if exc.uncertain else "failed"
                runtime.database.update_task(
                    task_id,
                    status=status,
                    size_bytes=size,
                    error_code=exc.code,
                    finished_at=utc_now() if status == "failed" else None,
                    duration_ms=_duration(request),
                )
                raise APIError(
                    502, exc.code, exc.message, task_id=task_id
                ) from exc
            try:
                metadata = await anyio.to_thread.run_sync(
                    provider.head_object, object_key
                )
                if metadata is None:
                    raise ProviderError(
                        "UPLOAD_CONFIRMATION_PENDING",
                        "上传已返回成功，但暂时无法确认远端对象",
                        uncertain=True,
                    )
                require_upload_metadata(metadata, size)
            except ProviderError as exc:
                runtime.database.update_task(
                    task_id,
                    status="unknown",
                    size_bytes=size,
                    object_status="pending",
                    error_code=exc.code,
                    duration_ms=_duration(request),
                )
                raise APIError(502, exc.code, exc.message, task_id=task_id) from exc
            duration = _duration(request)
            delete_token, delete_token_hash = issue_delete_token()
            try:
                runtime.database.update_task(
                    task_id,
                    status="succeeded",
                    size_bytes=metadata.size_bytes,
                    etag=metadata.etag,
                    version_id=metadata.version_id,
                    delete_token_hash=delete_token_hash,
                    object_status="present",
                    error_code=None,
                    finished_at=utc_now(),
                    duration_ms=duration,
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
                    "duration_ms": duration,
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
            spool.close()
            await source.close()

    @app.get("/v1/upload-tasks", response_model=TaskListResponse)
    async def tasks(request: Request):
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError as exc:
            raise APIError(400, "BAD_REQUEST", "分页参数不合法") from exc
        status = request.query_params.get("status")
        if not 1 <= limit <= 200 or offset < 0 or (
            status is not None and status not in STATUS_VALUES
        ):
            raise APIError(400, "BAD_REQUEST", "任务查询参数不合法")
        from_time = _parse_time(request.query_params.get("from"), "from")
        to_time = _parse_time(request.query_params.get("to"), "to")
        if from_time and to_time and to_time <= from_time:
            raise APIError(400, "BAD_REQUEST", "to 必须晚于 from")
        try:
            items = request.app.state.runtime.database.list_tasks(
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
        task = request.app.state.runtime.database.task_by_id(task_id)
        if task is None:
            raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
        return _task_item(task, detail=True)

    @app.delete(
        "/v1/upload-tasks/{task_id}/object",
        response_model=DeleteObjectResponse,
    )
    async def delete_task_object(
        request: Request,
        task_id: str,
        x_delete_token: str | None = Header(
            default=None, alias="X-Delete-Token"
        ),
    ):
        try:
            UUID(task_id)
        except ValueError as exc:
            raise APIError(400, "BAD_REQUEST", "任务 ID 不合法") from exc
        if request.headers.get("transfer-encoding") or request.headers.get(
            "content-length"
        ) not in {None, "0"}:
            raise APIError(400, "BAD_REQUEST", "删除请求不能包含请求体")
        runtime: Runtime = request.app.state.runtime
        task = runtime.database.task_by_id(task_id)
        if task is None:
            raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
        if not matches_delete_token(
            x_delete_token, task.get("delete_token_hash")
        ):
            raise APIError(
                403,
                "DELETE_TOKEN_INVALID",
                "删除凭证无效",
                task_id=task_id,
            )
        existing = _deletion_gate(task)
        if existing:
            return existing
        try:
            claimed = runtime.database.claim_task_deletion(
                task_id, _request_id(request), utc_now()
            )
        except sqlite3.Error as exc:
            raise APIError(
                500,
                "DATABASE_ERROR",
                "无法开始对象删除",
                task_id=task_id,
            ) from exc
        if not claimed:
            current = runtime.database.task_by_id(task_id)
            if current is None:
                raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
            result = _deletion_gate(current)
            if result:
                return result
            raise APIError(
                409,
                "DELETE_IN_PROGRESS",
                "对象删除状态已改变",
                task_id=task_id,
            )
        runtime.log.emit(
            NOTIFY,
            "object_delete_started",
            "对象删除已开始",
            request_id=_request_id(request),
            task_id=task_id,
            details={
                "from_status": "present",
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
            _save_deletion(
                runtime,
                _request_id(request),
                task,
                "config_unavailable",
                object_status="present",
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
                return _delete_pending(runtime, request, task, exc.code)
            _save_deletion(
                runtime,
                _request_id(request),
                task,
                exc.code,
                object_status="present",
                delete_error_code="DELETE_FAILED",
            )
            raise APIError(
                502,
                "DELETE_FAILED",
                "无法读取待删除对象",
                task_id=task_id,
            ) from exc
        if metadata is None:
            deleted_at = utc_now()
            _save_deletion(
                runtime,
                _request_id(request),
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
            metadata,
            task["size_bytes"],
            task["etag"],
            task["version_id"],
        ):
            _save_deletion(
                runtime,
                _request_id(request),
                task,
                "metadata_mismatch",
                object_status="present",
                delete_error_code="OBJECT_CHANGED",
            )
            raise APIError(
                409,
                "OBJECT_CHANGED",
                "远端对象与上传记录不一致",
                task_id=task_id,
            )
        try:
            await anyio.to_thread.run_sync(
                provider.delete_object, task["object_key"], task["version_id"]
            )
        except ProviderError as exc:
            if exc.uncertain:
                return _delete_pending(runtime, request, task, exc.code)
            _save_deletion(
                runtime,
                _request_id(request),
                task,
                exc.code,
                object_status="present",
                delete_error_code="DELETE_FAILED",
            )
            raise APIError(
                502,
                "DELETE_FAILED",
                "Storage Provider 拒绝删除",
                task_id=task_id,
            ) from exc
        try:
            remaining = await anyio.to_thread.run_sync(
                provider.head_object, task["object_key"], task["version_id"]
            )
        except ProviderError:
            return _delete_pending(runtime, request, task, "post_delete_head_error")
        if remaining is not None:
            if not matches_object_metadata(
                remaining,
                task["size_bytes"],
                task["etag"],
                task["version_id"],
            ):
                return _delete_pending(
                    runtime, request, task, "post_delete_object_changed"
                )
            _save_deletion(
                runtime,
                _request_id(request),
                task,
                "still_present",
                object_status="present",
                delete_error_code="DELETE_FAILED",
            )
            raise APIError(
                502,
                "DELETE_FAILED",
                "删除后对象仍然存在",
                task_id=task_id,
            )
        deleted_at = utc_now()
        _save_deletion(
            runtime,
            _request_id(request),
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

    @app.get("/v1/dashboard/summary", response_model=DashboardSummaryResponse)
    async def dashboard_summary(request: Request):
        from_time, to_time = _time_range(request)
        runtime: Runtime = request.app.state.runtime
        uploads = runtime.database.summary(from_time, to_time)
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
        tasks = runtime.database.tasks_in_range(from_time, to_time)
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
        items = request.app.state.runtime.database.list_logs(
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

    return app


app = create_app()
