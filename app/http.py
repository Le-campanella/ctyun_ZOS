from __future__ import annotations

import base64
import binascii
import math
import secrets
from collections import deque
from functools import partial
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse

from .config import Settings


def admin_path(path: str, method: str) -> bool:
    return (
        path.startswith("/v1/settings/")
        or path == "/v1/settings/storage"
        or path.startswith("/v1/dashboard/")
        or path.startswith("/v1/admin/")
        or path
        in {"/dashboard", "/dashboard/settings", "/openapi.json", "/docs", "/redoc"}
        or path.startswith("/static/")
        or (
            method == "GET"
            and (path == "/v1/upload-tasks" or path.startswith("/v1/upload-tasks/"))
        )
    )


def _admin_key(headers: dict[bytes, bytes]) -> str | None:
    direct = headers.get(b"x-admin-key")
    if direct:
        return direct.decode("latin1")
    authorization = headers.get(b"authorization", b"").decode("latin1")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    if scheme.lower() == "basic" and value:
        try:
            username, separator, password = (
                base64.b64decode(value, validate=True).decode("utf-8").partition(":")
            )
        except (binascii.Error, UnicodeDecodeError):
            return None
        if separator and username == "admin":
            return password
    return None


def _valid_admin_key(supplied: str | None, expected: tuple[str, ...]) -> bool:
    candidate = supplied or ""
    matched = False
    for key in expected:
        matched |= secrets.compare_digest(candidate, key)
    return matched


def _client_identity(headers: dict[bytes, bytes], settings: Settings) -> str | None:
    if _valid_admin_key(_admin_key(headers), settings.admin_api_keys):
        return "admin-dashboard"
    if not settings.client_api_keys:
        return "legacy"
    client_id = headers.get(b"x-client-id", b"").decode("latin1")
    supplied = headers.get(b"x-client-key", b"").decode("latin1")
    matched = False
    for expected_id, expected_key in settings.client_api_keys:
        matched |= secrets.compare_digest(
            client_id, expected_id
        ) & secrets.compare_digest(supplied, expected_key)
    return client_id if matched else None


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
        # ponytail: direct LAN peers keep this map small; add a global sweep if
        # the service ever accepts high-cardinality public source addresses.
        self._upload_attempts: dict[str, deque[float]] = {}

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
        settings = self.settings()
        if not settings.dashboard_enabled and (
            scope["path"] in {"/dashboard", "/dashboard/settings"}
            or scope["path"].startswith("/static/")
            or scope["path"].startswith("/v1/dashboard/")
        ):
            await self._response(
                scope,
                receive,
                send,
                404,
                error_body("NOT_FOUND", "资源未启用", request_id),
                request_id,
            )
            return
        if admin_path(scope["path"].rstrip("/") or "/", scope["method"]):
            if not _valid_admin_key(_admin_key(headers), settings.admin_api_keys):
                await self._response(
                    scope,
                    receive,
                    send,
                    401,
                    error_body("ADMIN_AUTH_REQUIRED", "需要管理员凭证", request_id),
                    request_id,
                    {"WWW-Authenticate": 'Basic realm="ZOS Admin"'},
                )
                return
        is_upload = scope["method"] == "POST" and scope["path"].rstrip("/") in {
            "/v1/uploads",
            "/v1/uploads/validate",
        }
        acquired = False
        if is_upload:
            client_id = _client_identity(headers, settings)
            if client_id is None:
                await self._response(
                    scope,
                    receive,
                    send,
                    401,
                    error_body(
                        "CLIENT_AUTH_REQUIRED", "需要有效的调用方凭证", request_id
                    ),
                    request_id,
                )
                return
            scope["state"]["client_id"] = client_id
            source = scope.get("client")
            source_ip = source[0] if source else "unknown"
            now = monotonic()
            attempts = self._upload_attempts.setdefault(source_ip, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= settings.upload_rate_limit_per_minute:
                retry_after = max(1, math.ceil(60 - (now - attempts[0])))
                await self._response(
                    scope,
                    receive,
                    send,
                    429,
                    error_body(
                        "UPLOAD_RATE_LIMITED", "来源地址上传频率过高", request_id
                    ),
                    request_id,
                    {"Retry-After": str(retry_after)},
                )
                return
            attempts.append(now)
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
                    too_large = int(content_length) > settings.max_request_body_bytes
                except ValueError:
                    too_large = True
                if too_large:
                    self.active_uploads -= 1
                    await self._response(
                        scope,
                        receive,
                        send,
                        413,
                        error_body("FILE_TOO_LARGE", "请求体超过允许上限", request_id),
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


async def database_call(method, *args, **kwargs):
    return await anyio.to_thread.run_sync(partial(method, *args, **kwargs))


def request_id(request: Request) -> str:
    return request.state.request_id


def duration(request: Request) -> int:
    return round((monotonic() - request.state.started_at) * 1_000)


def no_store(body: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        body, status_code=status_code, headers={"Cache-Control": "no-store"}
    )
