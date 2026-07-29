from __future__ import annotations

import json
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
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from .config import Settings
from .database import Database, RevisionConflict, utc_now
from .eventlog import NOTIFY
from .providers import ProviderError, ProviderRegistry, default_registry
from .runtime import Runtime


STATUS_VALUES = {"uploading", "unknown", "succeeded", "failed"}
LEVELS = {"NOTIFY": 25, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")


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
        "storage_provider": task.get("storage_provider"),
        "storage_config_revision": task.get("storage_config_revision"),
        "filename": task["filename"],
        "content_type": task["content_type"],
        "object_key": task["object_key"],
        "public_url": task["public_url"] if task["status"] == "succeeded" else None,
        "status": task["status"],
        "size_bytes": task["size_bytes"],
        "error_code": task["error_code"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
        "duration_ms": task["duration_ms"],
    }
    if detail:
        item["idempotency_key"] = task["idempotency_key"]
    return item


def _provider_status(error: ProviderError) -> int:
    if error.code in {"STORAGE_CONFIG_INVALID", "STORAGE_CREDENTIALS_REQUIRED"}:
        return 400
    if error.code == "STORAGE_METRICS_UNAVAILABLE":
        return 503
    return 502


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
    async def validation_error(request: Request, _exc: RequestValidationError):
        return JSONResponse(
            error_body("BAD_REQUEST", "请求参数不合法", _request_id(request)),
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

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
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

    @app.get("/v1/settings/storage/providers")
    async def provider_schemas(request: Request):
        runtime: Runtime = request.app.state.runtime
        return {"items": runtime.registry.schemas()}

    @app.get("/v1/settings/storage")
    async def current_storage(request: Request):
        response = JSONResponse(request.app.state.runtime.current_storage())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/v1/settings/storage/test")
    async def test_storage(request: Request):
        _settings_request(request)
        try:
            payload = await request.json()
            result = await request.app.state.runtime.test_storage(payload)
        except json.JSONDecodeError as exc:
            raise APIError(400, "STORAGE_CONFIG_INVALID", "JSON 格式不合法") from exc
        except ProviderError as exc:
            raise APIError(_provider_status(exc), exc.code, exc.message) from exc
        response = JSONResponse(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.put("/v1/settings/storage")
    async def save_storage(request: Request):
        _settings_request(request)
        try:
            payload = await request.json()
            result = await request.app.state.runtime.activate_storage(payload)
        except json.JSONDecodeError as exc:
            raise APIError(400, "STORAGE_CONFIG_INVALID", "JSON 格式不合法") from exc
        except ProviderError as exc:
            raise APIError(_provider_status(exc), exc.code, exc.message) from exc
        except RevisionConflict as exc:
            raise APIError(
                409,
                "CONFIG_REVISION_CONFLICT",
                f"当前配置 revision 为 {exc.current_revision}",
            ) from exc
        response = JSONResponse(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/v1/uploads/validate")
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

    @app.post("/v1/uploads")
    async def upload(request: Request):
        runtime: Runtime = request.app.state.runtime
        snapshot = runtime.active_snapshot()
        if snapshot is None:
            raise APIError(503, "STORAGE_NOT_CONFIGURED", "尚未激活存储配置")
        storage, provider = snapshot
        idempotency = request.headers.get("idempotency-key")
        if idempotency is not None:
            if not 1 <= len(idempotency) <= 128 or any(
                ord(char) < 33 or ord(char) > 126 for char in idempotency
            ):
                raise APIError(400, "BAD_REQUEST", "Idempotency-Key 不合法")
            existing = runtime.database.task_by_idempotency(idempotency)
            if existing:
                if existing["status"] == "succeeded":
                    response = JSONResponse(
                        {
                            "task_id": existing["id"],
                            "key": existing["object_key"],
                            "url": existing["public_url"],
                        }
                    )
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                code = (
                    "IDEMPOTENCY_KEY_REUSED"
                    if existing["status"] == "failed"
                    else "UPLOAD_IN_PROGRESS"
                )
                raise APIError(
                    409,
                    code,
                    "该幂等键已经绑定上传任务",
                    task_id=existing["id"],
                )
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
            "storage_config_id": storage["id"],
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
            if existing and existing["status"] == "succeeded":
                response = JSONResponse(
                    {
                        "task_id": existing["id"],
                        "key": existing["object_key"],
                        "url": existing["public_url"],
                    }
                )
                response.headers["Idempotency-Replayed"] = "true"
                return response
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
            details={"filename": filename, "object_key": object_key},
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
            duration = _duration(request)
            try:
                runtime.database.update_task(
                    task_id,
                    status="succeeded",
                    size_bytes=size,
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
                    "storage_provider": storage["provider"],
                    "storage_config_revision": storage["revision"],
                },
            )
            return JSONResponse(
                {"task_id": task_id, "key": object_key, "url": public_url},
                status_code=201,
            )
        finally:
            spool.close()
            await source.close()

    @app.get("/v1/upload-tasks")
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

    @app.get("/v1/upload-tasks/{task_id}")
    async def task_detail(request: Request, task_id: str):
        try:
            UUID(task_id)
        except ValueError as exc:
            raise APIError(400, "BAD_REQUEST", "任务 ID 不合法") from exc
        task = request.app.state.runtime.database.task_by_id(task_id)
        if task is None:
            raise APIError(404, "TASK_NOT_FOUND", "任务不存在")
        return _task_item(task, detail=True)

    @app.get("/v1/dashboard/summary")
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

    @app.get("/v1/dashboard/traffic")
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

    @app.get("/v1/dashboard/logs")
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

    @app.get("/v1/dashboard/storage")
    async def dashboard_storage(request: Request):
        from_time, to_time = _time_range(request)
        runtime: Runtime = request.app.state.runtime
        snapshot = runtime.active_snapshot()
        if snapshot is None:
            raise APIError(503, "STORAGE_NOT_CONFIGURED", "尚未激活存储配置")
        storage, provider = snapshot
        try:
            result = await anyio.to_thread.run_sync(
                provider.get_metrics, from_time, to_time
            )
        except ProviderError as exc:
            raise APIError(503, exc.code, exc.message) from exc
        return {
            **result,
            "provider": storage["provider"],
            "provider_schema_version": storage["provider_schema_version"],
            "storage_config_revision": storage["revision"],
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
