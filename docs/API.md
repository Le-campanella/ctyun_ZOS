---
status: unreleased
target_version: v3
implementation_baseline: API v1
database_schema: v3
implementation_status: verified_delete_ready
---

# 局域网轻量文件上传服务接口文档（ZOS v3）

> 面向对象：局域网内调用本服务的其他服务、接入 Agent，以及包含监控与存储设置功能的 Dashboard。
>
> 接口文档修订：`v3`（多存储预设、上传路由和严格删除已实现，删除恢复与完整审计待实现）；HTTP 路径命名空间继续使用 `/v1`。
>
> 同步基线：`PLAN.md` v6
>
> **注意：本文是尚未发布的 v3 目标契约，不能作为当前生产接口契约。当前已实现行为以 README 的“当前发布基线”和服务 OpenAPI 为准。**

## 1. 能力范围

本服务提供：

1. 通过服务端配置的存储预设上传单个文件；可显式选择预设，未指定时使用默认预设。
2. 返回上传任务 ID、对象 Key 和公网 URL。
3. 查询上传任务列表和单个任务详情。
4. 使用可选 `Idempotency-Key` 避免调用方超时后产生重复对象。
5. 返回进程健康状态和服务就绪状态。
6. 提供上传统计、流量时间序列、`NOTIFY` 及以上日志和可选 Storage Provider 原生指标；第一版为 ZOS Bucket 指标。
7. 提供同源、无登录的 Web Dashboard，其中监控区域只读，设置页面可以测试和激活存储配置。
8. 提供 Provider 类型、多个存储预设、默认项、连接测试和配置 revision API。
9. 提供不会上传对象存储、不会创建任务记录的局域网文件接收测试。
10. 使用上传成功时返回的对象级删除凭证，严格删除该任务创建的对象并保留审计记录。

服务记录上传过程、任务结果、对象元数据、删除结果、存储配置 revision、统计和运行日志。对象的下载、更新、重命名、列表和 Bucket 权限管理由其他系统或对象存储配置负责。上传与删除调用方 API 保持 Provider 无关。

## 2. 网络与访问约定

- 示例地址：`http://zos-upload-service:8000`
- 服务仅部署在受控局域网地址或内部容器网络。
- API 与 Dashboard 共用同一个局域网端口。
- 服务不设置统一调用方认证、登录、用户、角色或权限系统。
- 删除是破坏性操作，除受控局域网边界外还必须提供上传成功响应中的任务级 `delete_token`。
- 防火墙、VLAN、容器网络和端口暴露规则构成访问边界。
- 部署配置关闭公网入口、端口转发和公有负载均衡器。
- CORS 默认关闭，Dashboard 通过同源请求读写数据。
- Dashboard 监控 API 使用 `GET`；存储设置使用 `GET`、`POST`、`PUT` 和 `PATCH`。
- 设置 API 不设身份认证。局域网内能够访问服务端口的客户端均可选择任意启用预设，并可修改全部存储预设。
- 设置写请求只接受 JSON，并要求自定义 Header `X-Settings-Request: true`，用于降低浏览器跨站误提交风险。
- 设置请求会传输 AK/SK。正式部署应通过内网 HTTPS 暴露 Dashboard 与设置 API，或将其限制在隔离的管理 VLAN / 管理主机；使用 HTTP 时，能够监听局域网流量的设备也属于信任边界。服务仍保持无身份认证。
- `delete_token` 是持有者凭证，正式部署必须使用内网 HTTPS；反向代理、APM 和访问日志不得记录 `X-Delete-Token`。

## 3. 通用协议约定

### 3.1 编码与数据格式

- 请求和响应使用 UTF-8。
- JSON 响应使用 `application/json`。
- 时间字段使用带时区的 ISO 8601，并统一输出为 UTC，例如：

```text
2026-07-29T06:30:00Z
```

- 查询参数中的 `from` 为包含边界，`to` 为排除边界，即：

```text
from <= created_at < to
```

- 带时区偏移的时间会先转换为 UTC。缺少时区的时间参数返回 `400 BAD_REQUEST`。
- 字节数使用整数，单位为 byte。
- 耗时使用整数，单位为 millisecond。
- 调用方应忽略响应中尚未识别的新增字段。

### 3.2 请求 ID

调用方可以传入：

```http
X-Request-ID: optional-request-id
```

规则：

- Header 可选。
- 长度范围为 1 至 128 个可见字符。
- 控制字符和超长值返回 `400 BAD_REQUEST`。
- 未传时由服务生成 UUID。
- 服务在所有 API 响应头中返回最终使用的 `X-Request-ID`。
- `X-Request-ID` 用于追踪当前 HTTP 请求，不承担幂等作用。

### 3.3 通用错误结构

```json
{
  "task_id": "optional-task-id",
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "文件不能超过 200 MiB",
    "request_id": "82d1f9d8-..."
  }
}
```

约定：

- 已创建任务时，顶层包含 `task_id`。
- 尚未创建任务时，顶层省略 `task_id`。
- `message` 用于人工阅读，调用方程序逻辑判断 `code`。
- FastAPI 默认的参数校验错误统一映射为 `400 BAD_REQUEST`，接口不直接返回框架默认的 `422` 结构。
- 运维接口可以在通用错误对象之外附带只读诊断字段，例如 `/readyz` 的 `checks`。

### 3.4 设置请求与敏感字段

设置测试和保存请求必须满足：

```http
Content-Type: application/json
X-Settings-Request: true
```

规则：

- 浏览器请求携带 `Origin` 时，Origin 必须与当前服务同源；非浏览器局域网客户端可以不携带 Origin。
- CORS 继续关闭，自定义 Header 会触发浏览器跨域预检。
- 这些规则用于减少跨站网页对局域网服务的误写入，不提供调用方身份识别。
- 设置读取、测试和保存响应使用 `Cache-Control: no-store`。
- API 永远不返回 AK、SK 明文、凭证密文或部署级加密主密钥。
- GET 设置接口只返回 masked AK 和凭证是否已配置。
- 设置客户端使用 `provider_schema_version` 绑定 Provider 专属设置结构。
- Dashboard 不把凭证写入 LocalStorage、SessionStorage、URL、日志或错误对象；保存或测试完成后立即清空输入值。
- `delete_token` 不属于存储凭证，但按密码处理：只在首次上传成功的 `201` 中返回一次，不出现在幂等重放、任务查询、Dashboard、日志、错误对象或 URL 中。
- 调用方不得把 `delete_token` 发送给本服务之外的公网 API，也不得用它作为业务对象 ID。

## 4. 上传文件

### 4.1 请求

```http
POST /v1/uploads HTTP/1.1
Host: zos-upload-service:8000
Content-Type: multipart/form-data; boundary=...
X-Request-ID: optional-request-id
Idempotency-Key: optional-idempotency-key
X-Storage-Preset: optional-preset-key
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 二进制文件 | 新任务必填 | 单个文件，最大 200 MiB |

请求规则：

- 每次请求只能包含一个 `file` 字段。
- 文件上限为 `209715200` 字节。
- 请求体上限默认为 `213909504` 字节，用于容纳文件和 multipart 边界。
- 接受所有文件类型。
- 空文件返回 `FILE_EMPTY`。
- 文件部分的 `Content-Type` 会写入当前 active Storage Provider；缺失或无效时使用 `application/octet-stream`。
- 正式上传固定设置对象 canned ACL `public-read`；调用方不能修改。
- 原始文件名最多保存 255 个 Unicode 字符，超出部分截断。
- 原始文件名只用于任务记录和 Dashboard 展示。
- `X-Storage-Preset` 可选；值为 1 至 64 个字符的小写 slug，只允许小写字母、数字和中划线，且首尾必须为字母或数字。
- 显式指定时使用对应的启用预设；未指定时使用唯一默认预设。预设固定绑定 Provider、Endpoint、Bucket 和公网访问根地址。
- 新任务使用请求开始时解析出的预设及其 active storage config revision；默认项或设置切换不会改变已经创建的任务。
- 显式预设不存在时返回 `404 STORAGE_PRESET_NOT_FOUND`，已禁用时返回 `409 STORAGE_PRESET_DISABLED`；服务不会回退到默认项或其他预设。
- 未指定预设且默认项未配置时返回 `503 STORAGE_DEFAULT_NOT_CONFIGURED`，不创建任务。
- 幂等键命中已有任务时，服务可以在解析文件体之前返回重放或冲突结果；因此重放请求中的文件内容不会用于比较。
- 对已有幂等任务，未传预设或显式传入原预设时按原任务处理，即使该预设之后被禁用；显式传入其他 key 时优先返回 `IDEMPOTENCY_SCOPE_MISMATCH`，不重新解析默认项。

### 4.2 对象 Key

对象 Key 按 `Asia/Shanghai` 日期生成：

```text
YYYY/MM/DD/{task_id}.{safe_extension}
```

安全扩展名规则：

- 取原始文件名最后一个后缀。
- 转为小写。
- 只保留 `a-z` 和 `0-9`。
- 长度为 1 至 10 个字符。
- 无安全扩展名时，对象名称只包含任务 UUID。

示例：

```text
2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf
```

调用方不能直接指定 Provider、Endpoint、Bucket、对象 ACL 或对象 Key；只能通过 `X-Storage-Preset` 选择服务端预设。第一版不做负载均衡、权重、健康路由、故障转移或失败回退。

### 4.3 首次上传成功

```http
HTTP/1.1 201 Created
Content-Type: application/json
Cache-Control: no-store
X-Request-ID: 82d1f9d8-...
```

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_preset": "zos-main",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "size_bytes": 125678,
  "content_type": "application/pdf",
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "delete_token": "opaque-object-delete-capability"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 上传任务 UUID |
| `storage_preset` | string | 本任务解析并绑定的稳定预设 key |
| `key` | string | 对象存储中的对象 Key |
| `url` | string | 对象对应的完整公网 URL |
| `size_bytes` | integer | `HeadObject` 确认的对象大小 |
| `content_type` | string | 上传到对象存储的 Content-Type |
| `etag` | string | Provider 返回的不透明对象 ETag；不得假设为 MD5 |
| `version_id` | string 或 null | 对象版本 ID；Bucket 未启用或 Provider 不支持版本控制时为 null |
| `delete_token` | string | 只允许删除本任务对象的敏感持有者凭证；仅首次 `201` 返回 |

收到 `201` 表示：

1. S3 Transfer Manager 已确认上传成功。
2. `HeadObject` 已确认对象大小，并取得 ETag 和可选 VersionId。
3. SQLite 上传状态已更新为 `succeeded`，对象状态已更新为 `present`。
4. `size_bytes`、对象元数据、`finished_at` 和 `duration_ms` 已完成持久化。

调用方应直接保存和使用 `url`，并把它视为不透明字符串。调用方需要未来删除对象时，还必须安全保存 `task_id` 和 `delete_token`。服务上传时请求 `public-read` 对象 ACL；Bucket Policy、账号权限或 ZOS 侧安全策略仍可能阻止匿名访问。

响应不会返回 Bucket、Endpoint、AK/SK、`storage_config_id`、SDK 原始响应、请求签名或内部诊断 Header。

### 4.4 幂等重放成功

已有相同 `Idempotency-Key` 的任务状态为 `succeeded` 时：

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: no-store
X-Request-ID: current-request-id
Idempotency-Replayed: true
```

响应体的任务和对象字段与首次成功响应一致，但不会补发删除凭证：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_preset": "zos-main",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "size_bytes": 125678,
  "content_type": "application/pdf",
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "delete_token": null
}
```

说明：

- 返回的是原任务和原对象元数据。
- 返回原任务绑定的 `storage_preset`；默认预设之后发生变化也不影响重放结果。
- `delete_token` 固定为 `null`，不会通过幂等重放补发；`Idempotency-Key` 不是认证凭证。
- 本次响应头中的 `X-Request-ID` 对应当前重放请求。
- 原任务中的 `request_id` 保持首次创建任务时的值。
- 重放不会创建任务，也不会重复计入上传流量。

### 4.5 上传失败

| HTTP | `code` | 场景 | 任务记录 |
|---:|---|---|---|
| 400 | `FILE_REQUIRED` | 新任务缺少 `file` 字段 | 无 |
| 400 | `FILE_EMPTY` | 文件内容为空 | `failed` |
| 400 | `BAD_REQUEST` | multipart、Header 或参数格式错误 | 视失败阶段而定 |
| 400 | `STORAGE_PRESET_INVALID` | `X-Storage-Preset` 格式错误 | 无 |
| 404 | `STORAGE_PRESET_NOT_FOUND` | 显式指定的预设不存在 | 无 |
| 409 | `STORAGE_PRESET_DISABLED` | 显式指定的预设已禁用 | 无 |
| 409 | `IDEMPOTENCY_SCOPE_MISMATCH` | 幂等键原任务绑定了不同预设 | 复用已有任务 ID |
| 409 | `UPLOAD_IN_PROGRESS` | 相同幂等键对应任务为 `uploading` 或 `unknown` | 复用已有任务 ID |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 相同幂等键对应任务为 `failed` | 复用已有任务 ID |
| 413 | `FILE_TOO_LARGE` | 文件或请求体超过上限 | 视失败阶段而定 |
| 500 | `DATABASE_ERROR` | 创建或更新任务记录失败 | 不保证 |
| 500 | `INTERNAL_ERROR` | 未分类内部异常 | 视失败阶段而定 |
| 502 | `UPLOAD_FAILED` | Storage Provider 明确拒绝或上传失败 | `failed` |
| 502 | `STORAGE_TIMEOUT` | Storage Provider 请求超时 | `failed` 或 `unknown` |
| 502 | `UPLOAD_CONFIRMATION_PENDING` | 上传返回成功，但 HeadObject 暂未找到对象 | `unknown` |
| 502 | `UPLOAD_CONFIRMATION_FAILED` | Provider 返回的对象确认结果无效或无法读取 | `unknown` |
| 502 | `OBJECT_SIZE_MISMATCH` | HeadObject 大小与本次接收大小不一致 | `unknown` |
| 503 | `UPLOAD_CAPACITY_EXCEEDED` | 并发上传槽位已满 | 无新任务 |
| 503 | `STORAGE_NOT_CONFIGURED` | 显式预设没有 active 配置 | 无新任务 |
| 503 | `STORAGE_DEFAULT_NOT_CONFIGURED` | 未指定预设且默认预设不可用 | 无新任务 |

容量已满时响应包含：

```http
Retry-After: 5
```

`Retry-After` 是服务建议的等待秒数，调用方应按该值延迟重试。

幂等任务处理中示例：

```http
HTTP/1.1 409 Conflict
```

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "error": {
    "code": "UPLOAD_IN_PROGRESS",
    "message": "该幂等键对应的上传任务仍在处理或等待确认",
    "request_id": "82d1f9d8-..."
  }
}
```

### 4.6 局域网文件接收测试

用于验证调用方到本服务的网络、multipart 编码、文件大小限制和临时文件读写，不连接当前 Storage Provider，也不创建上传任务：

```http
POST /v1/uploads/validate HTTP/1.1
Host: zos-upload-service:8000
Content-Type: multipart/form-data; boundary=...
X-Request-ID: optional-request-id
```

请求使用与正式上传相同的单个 `file` 字段、200 MiB 文件上限、请求体上限和并发容量。未配置 Storage Provider 时也可以调用。

成功响应：

```json
{
  "received": true,
  "uploaded_to_storage": false,
  "recorded_as_task": false,
  "filename": "test.pdf",
  "content_type": "application/pdf",
  "size_bytes": 125678,
  "request_id": "82d1f9d8-..."
}
```

请求结束后立即关闭临时文件。该接口只能证明局域网文件接收链路正常，不能证明 ZOS 上传权限或公网 URL 可用。

## 5. 查询上传任务列表

### 5.1 请求

```http
GET /v1/upload-tasks?limit=50&offset=0&status=succeeded&from=2026-07-28T00%3A00%3A00Z&to=2026-07-29T00%3A00%3A00Z HTTP/1.1
Host: zos-upload-service:8000
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `limit` | integer | 否 | `50` | 返回数量，范围 `1` 至 `200` |
| `offset` | integer | 否 | `0` | 跳过数量，必须大于等于 `0` |
| `status` | string | 否 | 无 | `uploading`、`unknown`、`succeeded`、`failed` |
| `from` | datetime | 否 | 无 | 按 `created_at` 筛选，包含边界 |
| `to` | datetime | 否 | 无 | 按 `created_at` 筛选，排除边界 |

规则：

- `to` 必须晚于 `from`。
- 排序固定为 `created_at DESC, id DESC`。
- 时间过滤应用于任务的 `created_at`。
- 第一版使用 offset 分页。
- 持续写入期间需要遍历固定结果集时，调用方应固定 `to` 参数。

### 5.2 成功响应

```json
{
  "items": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "request_id": "example-001",
      "storage_preset": "zos-main",
      "storage_provider": "ctyun_zos",
      "storage_config_revision": 3,
      "filename": "report.pdf",
      "content_type": "application/pdf",
      "object_key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
      "public_url": "https://public-bucket.example.com/2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
      "status": "succeeded",
      "size_bytes": 125678,
      "etag": "\"opaque-etag\"",
      "version_id": null,
      "object_status": "present",
      "delete_error_code": null,
      "delete_started_at": null,
      "deleted_at": null,
      "error_code": null,
      "created_at": "2026-07-29T06:30:00Z",
      "finished_at": "2026-07-29T06:30:02Z",
      "duration_ms": 1842
    },
    {
      "id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "request_id": "example-002",
      "storage_preset": "zos-main",
      "storage_provider": "ctyun_zos",
      "storage_config_revision": 3,
      "filename": "archive.zip",
      "content_type": "application/zip",
      "object_key": "2026/07/29/6ba7b810-9dad-11d1-80b4-00c04fd430c8.zip",
      "public_url": null,
      "status": "unknown",
      "size_bytes": 10485760,
      "etag": null,
      "version_id": null,
      "object_status": "pending",
      "delete_error_code": null,
      "delete_started_at": null,
      "deleted_at": null,
      "error_code": "RECOVERY_PENDING",
      "created_at": "2026-07-29T06:20:00Z",
      "finished_at": null,
      "duration_ms": null
    }
  ],
  "limit": 50,
  "offset": 0
}
```

任务列表字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 任务 UUID |
| `request_id` | string | 首次创建任务的请求追踪 ID |
| `storage_preset` | string | 创建任务时解析并绑定的预设 key |
| `storage_provider` | string | 创建任务时使用的 Provider |
| `storage_config_revision` | integer | 创建任务时绑定的配置 revision |
| `filename` | string | 经过长度限制的原始文件名 |
| `content_type` | string | 上传到对象存储的 Content-Type |
| `object_key` | string | 上传前已持久化的对象 Key |
| `public_url` | string 或 null | `succeeded` 时的公网 URL |
| `status` | string | `uploading`、`unknown`、`succeeded`、`failed` |
| `size_bytes` | integer 或 null | 已确认的文件大小 |
| `etag` | string 或 null | 对象 ETag；不透明字符串，不保证是 MD5 |
| `version_id` | string 或 null | 上传对象的精确版本 ID |
| `object_status` | string | `pending`、`present`、`absent`、`legacy_unverified`、`deleting`、`deleted` 或 `delete_unknown` |
| `delete_error_code` | string 或 null | 最近一次删除失败或不确定结果的稳定错误码 |
| `delete_started_at` | string 或 null | 最近一次删除开始时间 |
| `deleted_at` | string 或 null | 服务确认对象或精确版本不存在的时间 |
| `error_code` | string 或 null | 当前错误或恢复状态码 |
| `created_at` | string | UTC 创建时间 |
| `finished_at` | string 或 null | 终态完成时间 |
| `duration_ms` | integer 或 null | 完整请求处理耗时；中断时可能为空 |

调用方持续增加 `offset`，直到 `items` 为空。列表永远不返回 `delete_token`。

## 6. 查询单个上传任务

### 6.1 请求

```http
GET /v1/upload-tasks/550e8400-e29b-41d4-a716-446655440000 HTTP/1.1
Host: zos-upload-service:8000
```

### 6.2 成功响应

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "request_id": "example-001",
  "idempotency_key": "ingest-job-20260729-001",
  "storage_preset": "zos-main",
  "storage_provider": "ctyun_zos",
  "storage_config_revision": 3,
  "filename": "report.pdf",
  "content_type": "application/pdf",
  "object_key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "public_url": "https://public-bucket.example.com/2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "status": "succeeded",
  "size_bytes": 125678,
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "object_status": "present",
  "delete_error_code": null,
  "delete_started_at": null,
  "deleted_at": null,
  "error_code": null,
  "created_at": "2026-07-29T06:30:00Z",
  "finished_at": "2026-07-29T06:30:02Z",
  "duration_ms": 1842
}
```

单任务详情在列表字段基础上增加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `idempotency_key` | string 或 null | 首次请求携带的幂等键 |

错误：

- 非法 UUID 返回 `400 BAD_REQUEST`。
- 格式正确但任务不存在时返回 `404 TASK_NOT_FOUND`。

单任务详情同样不返回 `delete_token`。

## 7. 删除已上传对象

### 7.1 请求

```http
DELETE /v1/upload-tasks/550e8400-e29b-41d4-a716-446655440000/object HTTP/1.1
Host: zos-upload-service:8000
X-Delete-Token: opaque-object-delete-capability
X-Request-ID: optional-request-id
```

规则：

- `task_id` 必须是合法 UUID。
- `X-Delete-Token` 必填，最大 256 个可见 ASCII 字符，必须与该任务首次上传成功时返回的 token 匹配。
- 请求没有 JSON body，不接受 Bucket、对象 Key、URL、Provider、Endpoint 或 VersionId。
- 删除目标只由服务端任务记录和任务绑定的原 storage config revision 决定。
- 预设被禁用、默认项被切换或配置新增 revision 均不改变删除目标。
- 服务对提交 token 计算 SHA-256，并与任务保存的哈希执行常量时间比较。缺失、格式错误、篡改或跨任务使用统一返回 `403 DELETE_TOKEN_INVALID`。
- 只有上传状态为 `succeeded` 且对象状态为 `present` 的任务可以开始删除。
- `legacy_unverified` 历史任务默认没有可用删除凭证，也不可删除。

### 7.2 严格删除流程

1. 服务在 SQLite 中以条件更新执行 `present → deleting`，防止并发删除。
2. 使用任务保存的 `storage_config_id` 创建原 Provider Client，不使用当前 active 配置。
3. 调用 `HeadObject`，比较数据库保存的对象大小、ETag 和可选 VersionId。
4. 任一元数据不一致时不删除，恢复为 `present` 并返回 `409 OBJECT_CHANGED`。
5. 有 VersionId 时删除精确版本；否则删除保存的对象 Key。
6. 删除后再次查询相同 Key 或精确 VersionId。
7. 只有 Provider 明确返回不存在时，才更新为 `deleted` 并返回成功。

调用方提供的 token 只授权删除与其签名绑定的任务对象，不能用于删除其他任务，也不能构造任意 Key。

### 7.3 首次删除成功

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Request-ID: 82d1f9d8-...
```

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "object_status": "deleted",
  "deleted_at": "2026-07-31T08:00:00Z",
  "already_deleted": false,
  "already_absent": false
}
```

成功只表示 ZOS 源对象或记录的精确版本已经确认不存在。CDN、反向代理、浏览器或第三方服务缓存可能继续提供旧内容，缓存失效不属于本接口的成功条件。

### 7.4 重复删除

数据库已记录为 `deleted` 时，不再请求 Provider，直接返回：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "object_status": "deleted",
  "deleted_at": "2026-07-31T08:00:00Z",
  "already_deleted": true,
  "already_absent": false
}
```

删除前 `HeadObject` 已经明确返回目标不存在时，服务记录审计结果并返回 `already_absent=true`。任务记录本身不会删除。

### 7.5 删除结果不确定

Provider 删除调用超时、连接中断或返回无法确认的服务端错误时：

```http
HTTP/1.1 202 Accepted
Content-Type: application/json
```

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "object_status": "delete_unknown",
  "error": {
    "code": "DELETE_PENDING",
    "message": "删除结果暂时无法确认",
    "request_id": "82d1f9d8-..."
  }
}
```

调用方不得把 `202` 当作删除成功。服务恢复器会继续查询目标：确认不存在时转为 `deleted`；确认原对象仍存在时转回 `present`；仍无法确认时保持 `delete_unknown`。调用方通过任务详情查询最终状态。

### 7.6 删除错误

| HTTP | `code` | 场景 |
|---:|---|---|
| 400 | `BAD_REQUEST` | `task_id` 或 Header 格式不合法 |
| 403 | `DELETE_TOKEN_INVALID` | 删除凭证缺失、错误或与任务不匹配 |
| 404 | `TASK_NOT_FOUND` | 任务不存在 |
| 409 | `OBJECT_NOT_DELETABLE` | 上传或对象状态不允许删除，或历史元数据未验证 |
| 409 | `OBJECT_CHANGED` | 删除前大小、ETag 或 VersionId 与上传记录不一致 |
| 409 | `DELETE_IN_PROGRESS` | 对象正在删除或等待恢复确认 |
| 500 | `DATABASE_ERROR` | 删除状态无法安全写入 SQLite |
| 502 | `DELETE_FAILED` | Provider 明确拒绝删除 |
| 503 | `STORAGE_CONFIG_UNAVAILABLE` | 任务原配置无法加载、解密或对应 Provider 已不可用 |

## 8. 健康检查

### 8.1 进程健康

```http
GET /healthz HTTP/1.1
Host: zos-upload-service:8000
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "status": "ok"
}
```

该接口只表示服务进程可以响应，不访问 SQLite、临时目录或 Storage Provider。

### 8.2 服务就绪

```http
GET /readyz HTTP/1.1
Host: zos-upload-service:8000
```

检查项：

- 部署级配置和 `SETTINGS_ENCRYPTION_KEY` 已加载。
- 唯一、启用的默认预设及其 active storage config 已存在并可解密。
- SQLite 可读写。
- 临时目录可写且剩余空间满足阈值。
- schema 初始化完成。
- 启动恢复扫描完成。
- 最近一次默认预设 Storage Provider 探测成功且未超过缓存有效期；`ctyun_zos` 使用 `HeadBucket`。
- 非默认预设的探测失败在设置页显示为 degraded，不使默认上传的 `/readyz` 失败。

就绪响应：

```http
HTTP/1.1 200 OK
```

```json
{
  "status": "ready",
  "checked_at": "2026-07-29T06:31:00Z",
  "checks": {
    "config": {
      "status": "ok",
      "configured": true,
      "storage_preset": "zos-main",
      "provider": "ctyun_zos",
      "provider_schema_version": 1,
      "revision": 3
    },
    "database": {
      "status": "ok"
    },
    "temp_dir": {
      "status": "ok",
      "free_bytes": 2147483648,
      "required_free_bytes": 1006632960
    },
    "schema": {
      "status": "ok"
    },
    "recovery": {
      "status": "ok",
      "completed": true,
      "pending_tasks": 0
    },
    "storage": {
      "status": "ok",
      "last_checked_at": "2026-07-29T06:30:52Z",
      "age_seconds": 8
    }
  }
}
```

未就绪响应：

```http
HTTP/1.1 503 Service Unavailable
```

```json
{
  "status": "not_ready",
  "checked_at": "2026-07-29T06:31:00Z",
  "checks": {
    "config": {
      "status": "ok",
      "configured": true,
      "storage_preset": "zos-main",
      "provider": "ctyun_zos",
      "provider_schema_version": 1,
      "revision": 3
    },
    "database": {
      "status": "ok"
    },
    "temp_dir": {
      "status": "error",
      "free_bytes": 104857600,
      "required_free_bytes": 1006632960
    },
    "schema": {
      "status": "ok"
    },
    "recovery": {
      "status": "ok",
      "completed": true,
      "pending_tasks": 0
    },
    "storage": {
      "status": "ok",
      "last_checked_at": "2026-07-29T06:30:52Z",
      "age_seconds": 8
    }
  },
  "error": {
    "code": "NOT_READY",
    "message": "服务依赖项未达到就绪条件",
    "request_id": "82d1f9d8-..."
  }
}
```

检查项的 `status` 可为：

- `ok`
- `pending`
- `degraded`
- `error`

没有可用默认预设时，`/readyz` 返回 `503`，`checks.config.status="error"`、`configured=false`，错误码为 `STORAGE_DEFAULT_NOT_CONFIGURED`。此时 `/healthz`、Dashboard 和设置接口继续可用。

`/readyz` 用于编排平台就绪检查。上传调用方在收到 `503 NOT_READY` 或 `503 STORAGE_DEFAULT_NOT_CONFIGURED` 时应延迟重试。

## 9. Web Dashboard 页面

### 9.1 监控页面

```http
GET /dashboard HTTP/1.1
Host: zos-upload-service:8000
```

成功响应：

```http
HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

页面特性：

- 与 API 同源。
- 无登录。
- 监控区域只读。
- 静态资源全部打包在服务镜像中。
- 页面不加载公网 CDN、远程字体或第三方脚本。
- 文件名、日志消息和动态字段统一执行 HTML 转义。

页面展示局域网文件接收测试、服务状态、默认预设与 revision、上传概览、上传流量图、近期上传任务和 `NOTIFY` 及以上日志。接收测试的“真实上传到 ZOS”开关默认关闭；开启后页面可选择一个启用预设并调用正式 `/v1/uploads`。

### 9.2 存储设置页面

```http
GET /dashboard/settings HTTP/1.1
Host: zos-upload-service:8000
```

页面提供：

- 存储预设列表、新建、编辑、测试、设为默认以及启停非默认预设。
- Provider 类型选择；第一版只显示“天翼云对象存储 ZOS”。
- SDK Endpoint（上传接口地址）、Bucket、public base URL、AK、SK、超时、重试、TLS 校验和 Bucket 指标开关。
- 当前预设状态 revision、配置 revision、masked AK、SK 是否配置和最近连接测试结果。
- “测试连接”和“保存并激活”操作。
- 当 `public_base_url` 为空时，页面可根据 Bucket 与外网 Endpoint 建议 `https://{bucket}.{endpoint-host}`；用户可以改为控制台显示的 Bucket 外网访问域名、CDN 或自定义域名。

AK/SK 输入框使用密码类型并关闭自动填充，页面加载时不回填原值，浏览器本地存储中不保存凭证。测试或保存完成后清空输入值。局域网内能够访问该页面和设置 API 的客户端均可修改配置。

## 10. Dashboard 存储设置

设置 API 采用稳定的 `provider + provider_schema_version + config + credentials` envelope。一个存储预设固定绑定一份配置，即一个 Provider、Endpoint、Bucket 和公网访问根地址。

### 10.1 查询 Provider 类型

```http
GET /v1/settings/storage/providers HTTP/1.1
Host: zos-upload-service:8000
```

成功响应：

```json
{
  "items": [
    {
      "id": "ctyun_zos",
      "display_name": "天翼云对象存储 ZOS",
      "schema_version": 1,
      "config_fields": [
        {
          "name": "endpoint_url",
          "type": "url",
          "required": true,
          "secret": false,
          "label": "ZOS Endpoint（SDK 上传接口地址）"
        },
        {
          "name": "bucket",
          "type": "string",
          "required": true,
          "secret": false,
          "label": "Bucket 名称"
        },
        {
          "name": "public_base_url",
          "type": "url",
          "required": true,
          "secret": false,
          "label": "对象访问根地址",
          "hint": "Bucket 外网访问域名、CDN 或自定义访问根地址",
          "suggested_value_template": "https://{bucket}.{endpoint_host}"
        },
        {
          "name": "connect_timeout_seconds",
          "type": "integer",
          "required": true,
          "default": 5
        },
        {
          "name": "read_timeout_seconds",
          "type": "integer",
          "required": true,
          "default": 300
        },
        {
          "name": "max_attempts",
          "type": "integer",
          "required": true,
          "default": 2
        },
        {
          "name": "verify_tls",
          "type": "boolean",
          "required": true,
          "default": true
        },
        {
          "name": "enable_bucket_metrics",
          "type": "boolean",
          "required": true,
          "default": false
        }
      ],
      "credential_fields": [
        {
          "name": "access_key",
          "type": "secret",
          "required_on_create": true,
          "label": "Access Key（AK）"
        },
        {
          "name": "secret_key",
          "type": "secret",
          "required_on_create": true,
          "label": "Secret Key（SK）"
        }
      ]
    }
  ]
}
```

Dashboard 使用该接口渲染 Provider 设置表单。这里的 Provider 类型不是上传可选的存储预设；上传可选项由 `/v1/settings/storage/presets` 返回。

### 10.2 管理存储预设

#### 10.2.1 查询预设列表

```http
GET /v1/settings/storage/presets HTTP/1.1
Host: zos-upload-service:8000
```

```json
{
  "items": [
    {
      "preset_key": "zos-main",
      "display_name": "主资料库",
      "enabled": true,
      "is_default": true,
      "state_revision": 2,
      "provider": "ctyun_zos",
      "provider_schema_version": 1,
      "config_revision": 4,
      "endpoint_host": "jiangsu-10.zos.ctyun.cn",
      "bucket": "main-assets",
      "last_connection_test": {
        "status": "ok",
        "tested_at": "2026-07-31T02:00:00Z",
        "latency_ms": 82
      },
      "created_at": "2026-07-30T01:00:00Z",
      "updated_at": "2026-07-31T02:00:01Z"
    }
  ]
}
```

列表不返回完整 Endpoint、public base URL 或 masked 凭证；编辑页面通过详情接口取得这些字段。

#### 10.2.2 创建预设

```http
POST /v1/settings/storage/presets HTTP/1.1
Content-Type: application/json
X-Settings-Request: true
```

```json
{
  "preset_key": "zos-archive",
  "display_name": "归档资料库",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "config": {
    "endpoint_url": "https://jiangsu-10.zos.ctyun.cn",
    "bucket": "archive-assets",
    "public_base_url": "https://archive-assets.jiangsu-10.zos.ctyun.cn",
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 300,
    "max_attempts": 2,
    "verify_tls": true,
    "enable_bucket_metrics": false
  },
  "credentials": {
    "access_key": "candidate-access-key",
    "secret_key": "candidate-secret-key"
  }
}
```

服务先执行 `HeadBucket`，成功后创建启用的预设及配置 revision 1。第一个预设自动成为默认项；后续预设默认不是默认项。成功返回 `201`、`state_revision=1` 和完整非敏感详情。`preset_key` 创建后不可修改，也不提供 `DELETE` 接口。

#### 10.2.3 查询单个预设

```http
GET /v1/settings/storage/presets/zos-archive HTTP/1.1
```

成功响应在列表字段基础上增加完整 `config`、masked `credentials` 和 `activated_at`，结构与本章默认设置响应相同。

#### 10.2.4 更新预设配置

```http
PUT /v1/settings/storage/presets/zos-archive HTTP/1.1
Content-Type: application/json
X-Settings-Request: true
```

请求使用本章“保存并激活设置”的 envelope，并提交该预设当前的 `expected_revision`。测试成功后只为目标预设创建新 revision；其他预设和默认项不变。在途任务继续使用旧 revision。

#### 10.2.5 修改显示名或启用状态

```http
PATCH /v1/settings/storage/presets/zos-archive HTTP/1.1
Content-Type: application/json
X-Settings-Request: true
```

```json
{
  "expected_state_revision": 1,
  "display_name": "冷数据归档",
  "enabled": false
}
```

至少提交一个可修改字段。`preset_key` 不可修改。成功后 `state_revision` 加 1；版本不匹配返回 `409 PRESET_STATE_CONFLICT`。当前默认预设不能直接禁用，必须先切换默认项。禁用只阻止新上传，历史任务的恢复与删除仍使用原 `storage_config_id`。

#### 10.2.6 切换默认预设

```http
PUT /v1/settings/storage/default HTTP/1.1
Content-Type: application/json
X-Settings-Request: true
```

```json
{
  "preset_key": "zos-archive",
  "expected_default_preset": "zos-main",
  "expected_state_revision": 1
}
```

目标必须存在且已启用。服务在单个事务内切换唯一默认项；并发条件不匹配返回 `409 DEFAULT_PRESET_CONFLICT`。切换只影响之后未携带 `X-Storage-Preset` 的新任务，不改变在途任务、历史任务或幂等重放。

所有预设管理响应都使用 `Cache-Control: no-store`。第一版不提供硬删除、批量修改、负载均衡、权重、自动故障转移或失败回退。

### 10.3 查询默认设置（兼容接口）

```http
GET /v1/settings/storage HTTP/1.1
Host: zos-upload-service:8000
```

已配置响应：

```json
{
  "configured": true,
  "preset_key": "zos-main",
  "display_name": "主资料库",
  "enabled": true,
  "is_default": true,
  "state_revision": 2,
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "revision": 3,
  "config": {
    "endpoint_url": "https://jiangsu-10.zos.ctyun.cn",
    "bucket": "example-bucket",
    "public_base_url": "https://example-bucket.jiangsu-10.zos.ctyun.cn",
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 300,
    "max_attempts": 2,
    "verify_tls": true,
    "enable_bucket_metrics": false
  },
  "credentials": {
    "access_key_configured": true,
    "access_key_masked": "****A1B2",
    "secret_key_configured": true
  },
  "last_connection_test": {
    "status": "ok",
    "tested_at": "2026-07-29T07:00:00Z",
    "latency_ms": 82
  },
  "activated_at": "2026-07-29T07:00:01Z"
}
```

尚未配置响应：

```json
{
  "configured": false,
  "preset_key": null,
  "provider": null,
  "provider_schema_version": null,
  "revision": 0,
  "config": null,
  "credentials": {
    "access_key_configured": false,
    "access_key_masked": null,
    "secret_key_configured": false
  },
  "last_connection_test": null,
  "activated_at": null
}
```

该接口是默认预设详情的兼容别名。响应不会包含 AK、SK 明文或密文。

### 10.4 ZOS 设置字段

设置 envelope 的 `provider_schema_version` 对 `ctyun_zos` 第一版固定为 `1`。`ctyun_zos` 的完整 `config`：

| 字段 | 类型 | 必填 | 约束与含义 |
|---|---|---:|---|
| `endpoint_url` | string | 是 | SDK 上传接口地址，即 ZOS 地域 Endpoint 或内网 Endpoint；只允许 `http`、`https`，禁止 userinfo、query、fragment，路径只允许为空或 `/` |
| `bucket` | string | 是 | 3 至 63 个字符，小写字母、数字和中划线，首尾不能为中划线 |
| `public_base_url` | string | 是 | 对象访问根地址；只允许 `http`、`https`，禁止 userinfo、query、fragment；允许路径前缀，保存时去除末尾 `/` |
| `connect_timeout_seconds` | integer | 是 | `1` 至 `60` |
| `read_timeout_seconds` | integer | 是 | `1` 至 `3600` |
| `max_attempts` | integer | 是 | SDK 最大重试次数，不含首次请求；`0` 至 `5` |
| `verify_tls` | boolean | 是 | HTTPS 证书校验；默认 `true` |
| `enable_bucket_metrics` | boolean | 是 | 启用 ZOS Bucket Statistics 和 Storage Info |

凭证对象：

| 字段 | 首次配置 | 更新配置 | 说明 |
|---|---:|---:|---|
| `access_key` | 必填 | 条件可省略 | 同一 Provider、`provider_schema_version` 和 `endpoint_url` 下更新时，省略表示沿用当前 active revision 的 AK |
| `secret_key` | 必填 | 条件可省略 | 同一 Provider、`provider_schema_version` 和 `endpoint_url` 下更新时，省略表示沿用当前 active revision 的 SK |

首次配置以及 Provider、`provider_schema_version` 或 `endpoint_url` 发生变化时，必须提交 `credentials` 对象并同时提供 AK 和 SK；缺失时返回 `400 STORAGE_CREDENTIALS_REQUIRED`。同一 Provider、schema version 和 Endpoint 下更新时，可以省略整个 `credentials` 对象或其中一个字段。Dashboard 输入框留空时应省略字段，空字符串返回 `400 STORAGE_CONFIG_INVALID`。

天翼云 Bucket 外网访问域名通常采用 `协议://BucketName.Endpoint`。服务仍要求显式保存 `public_base_url`，以兼容内网 Endpoint、CDN 和自定义域名。

### 10.5 测试候选设置

```http
POST /v1/settings/storage/test HTTP/1.1
Host: zos-upload-service:8000
Content-Type: application/json
X-Settings-Request: true
```

```json
{
  "preset_key": "zos-archive",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "config": {
    "endpoint_url": "https://jiangsu-10.zos.ctyun.cn",
    "bucket": "example-bucket",
    "public_base_url": "https://example-bucket.jiangsu-10.zos.ctyun.cn",
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 300,
    "max_attempts": 2,
    "verify_tls": true,
    "enable_bucket_metrics": false
  },
  "credentials": {
    "access_key": "candidate-access-key",
    "secret_key": "candidate-secret-key"
  }
}
```

规则：

- 请求不写入数据库，也不切换 active revision。
- `preset_key` 可选；传入时可以复用该预设的现有凭证，未传时使用默认预设作为合并上下文。候选 Endpoint 改变时仍必须提交完整 AK/SK。
- 已有配置且 Provider、`provider_schema_version` 与 `endpoint_url` 均保持不变时，可以省略整个 `credentials` 对象或其中一个字段。
- 首次配置以及 Provider、`provider_schema_version` 或 `endpoint_url` 发生变化时，必须同时提供 AK 和 SK。
- 服务校验 `provider_schema_version` 与 Provider schema，创建候选 Client，并调用 `HeadBucket`。
- `HeadBucket` 成功表示 Endpoint 可达、签名凭证被接受且 Bucket 可访问。
- 该测试不会写入探测对象，因此不会验证 `PutObject` 权限，也不会验证 `public_base_url` 已具备公网读取权限。

成功响应：

```json
{
  "status": "ok",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "tested_at": "2026-07-29T07:00:00Z",
  "latency_ms": 82,
  "checks": {
    "schema": {
      "status": "ok"
    },
    "client": {
      "status": "ok"
    },
    "head_bucket": {
      "status": "ok"
    }
  }
}
```

失败响应示例：

```http
HTTP/1.1 502 Bad Gateway
```

```json
{
  "status": "error",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "tested_at": "2026-07-29T07:00:00Z",
  "checks": {
    "schema": {
      "status": "ok"
    },
    "client": {
      "status": "ok"
    },
    "head_bucket": {
      "status": "error"
    }
  },
  "error": {
    "code": "STORAGE_CREDENTIALS_REJECTED",
    "message": "ZOS 拒绝了当前访问凭证",
    "request_id": "82d1f9d8-..."
  }
}
```

### 10.6 保存并激活默认设置（兼容接口）

```http
PUT /v1/settings/storage HTTP/1.1
Host: zos-upload-service:8000
Content-Type: application/json
X-Settings-Request: true
```

```json
{
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "expected_revision": 3,
  "config": {
    "endpoint_url": "https://jiangsu-10.zos.ctyun.cn",
    "bucket": "example-bucket",
    "public_base_url": "https://example-bucket.jiangsu-10.zos.ctyun.cn",
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 300,
    "max_attempts": 2,
    "verify_tls": true,
    "enable_bucket_metrics": true
  },
  "credentials": {
    "access_key": "rotated-access-key",
    "secret_key": "rotated-secret-key"
  }
}
```

`expected_revision`：

- 首次配置固定为 `0`。
- 更新时必须等于 GET 当前设置返回的 `revision`。
- 不一致时返回 `409 CONFIG_REVISION_CONFLICT`，服务不执行测试或保存。

该接口是 `PUT /v1/settings/storage/presets/{当前默认 preset_key}` 的兼容别名，只更新当前默认预设的配置，不切换默认项。数据库尚无任何预设时，首次 `expected_revision=0` 会创建 `preset_key=default`、显示名“默认 ZOS”的启用默认预设。

保存流程：

1. 校验请求；仅在 Provider、`provider_schema_version` 与 `endpoint_url` 保持不变时合并被省略的现有凭证。
2. 执行与测试接口相同的 `HeadBucket` 检查。
3. 使用部署级 `SETTINGS_ENCRYPTION_KEY` 加密 AK/SK。
4. 在单个事务中创建新 revision，将旧 revision 标记为 inactive，并激活新 revision。
5. 原子切换进程内 Provider Client；新上传使用新 revision。
6. 在途上传继续使用请求开始时取得的旧 revision。
7. 写入不含凭证的 `storage_config_activated` NOTIFY 日志。

成功响应：

```json
{
  "configured": true,
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "revision": 4,
  "previous_revision": 3,
  "config": {
    "endpoint_url": "https://jiangsu-10.zos.ctyun.cn",
    "bucket": "example-bucket",
    "public_base_url": "https://example-bucket.jiangsu-10.zos.ctyun.cn",
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 300,
    "max_attempts": 2,
    "verify_tls": true,
    "enable_bucket_metrics": true
  },
  "credentials": {
    "access_key_configured": true,
    "access_key_masked": "****C3D4",
    "secret_key_configured": true
  },
  "last_connection_test": {
    "status": "ok",
    "tested_at": "2026-07-29T07:10:00Z",
    "latency_ms": 76
  },
  "activated_at": "2026-07-29T07:10:01Z"
}
```

测试、加密、数据库事务或 Client 切换失败时，旧 active revision 保持有效。历史 revision 不通过本 API 删除。

### 10.7 设置错误码

| HTTP | `code` | 说明 |
|---:|---|---|
| 400 | `STORAGE_CONFIG_INVALID` | Provider、schema version、URL、Bucket、凭证格式或连接参数不合法 |
| 400 | `STORAGE_CREDENTIALS_REQUIRED` | 首次配置或 Provider/schema version/Endpoint 变化时缺少完整 AK/SK |
| 400 | `STORAGE_PRESET_INVALID` | `preset_key` 格式不合法 |
| 404 | `STORAGE_PRESET_NOT_FOUND` | 指定预设不存在 |
| 409 | `CONFIG_REVISION_CONFLICT` | `expected_revision` 已过期 |
| 409 | `PRESET_STATE_CONFLICT` | `expected_state_revision` 已过期，或试图禁用当前默认预设 |
| 409 | `DEFAULT_PRESET_CONFLICT` | 默认项并发条件不匹配，或目标预设未启用 |
| 500 | `SETTINGS_STORAGE_ERROR` | 凭证加密、数据库写入或 Client 切换失败 |
| 502 | `STORAGE_ENDPOINT_UNREACHABLE` | Endpoint 无法连接、DNS 失败或 TLS 失败 |
| 502 | `STORAGE_CREDENTIALS_REJECTED` | AK/SK 无效、失效或被拒绝 |
| 502 | `STORAGE_BUCKET_UNAVAILABLE` | Bucket 不存在或当前凭证无访问权限 |
| 503 | `STORAGE_NOT_CONFIGURED` | 指定预设尚未激活配置 |
| 503 | `STORAGE_DEFAULT_NOT_CONFIGURED` | 没有可用默认预设 |

设置错误响应不会回显候选 AK/SK，也不会把 SDK 原始请求签名写入 `message` 或诊断字段。

## 11. Dashboard 上传概览

### 11.1 请求

```http
GET /v1/dashboard/summary?from=2026-07-28T06%3A30%3A00Z&to=2026-07-29T06%3A30%3A00Z HTTP/1.1
Host: zos-upload-service:8000
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `from` | datetime | 否 | 当前时间前 24 小时 | 任务创建时间下界，包含 |
| `to` | datetime | 否 | 当前时间 | 任务创建时间上界，排除 |

规则：

- 最大查询范围为 31 天。
- 统计主数据源为 SQLite `upload_tasks`。
- ZOS 探测失败会把服务状态标记为 `degraded` 或 `not_ready`，本地统计仍可返回。
- SQLite 查询失败返回 `500 DATABASE_ERROR`。

### 11.2 成功响应

```json
{
  "range": {
    "from": "2026-07-28T06:30:00Z",
    "to": "2026-07-29T06:30:00Z"
  },
  "generated_at": "2026-07-29T06:30:05Z",
  "service": {
    "status": "ok",
    "ready": true,
    "checks": {
      "config": {
        "status": "ok",
        "configured": true,
        "provider": "ctyun_zos",
        "provider_schema_version": 1,
        "revision": 3
      },
      "database": {
        "status": "ok"
      },
      "temp_dir": {
        "status": "ok",
        "free_bytes": 2147483648,
        "required_free_bytes": 1006632960
      },
      "recovery": {
        "status": "ok",
        "completed": true,
        "pending_tasks": 0
      },
      "storage": {
        "status": "ok",
        "last_checked_at": "2026-07-29T06:29:55Z"
      }
    }
  },
  "uploads": {
    "attempt_count": 120,
    "success_count": 116,
    "failure_count": 3,
    "uploading_count": 1,
    "unknown_count": 0,
    "success_rate": 0.97479,
    "successful_upload_bytes": 987654321,
    "average_duration_ms": 1480,
    "p95_duration_ms": 3220
  }
}
```

字段定义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `attempt_count` | integer | 范围内创建的任务数 |
| `success_count` | integer | 当前状态为 `succeeded` 的任务数 |
| `failure_count` | integer | 当前状态为 `failed` 的任务数 |
| `uploading_count` | integer | 当前状态为 `uploading` 的任务数 |
| `unknown_count` | integer | 当前状态为 `unknown` 的任务数 |
| `success_rate` | number 或 null | `success_count / (success_count + failure_count)` |
| `successful_upload_bytes` | integer | 成功任务 `size_bytes` 之和 |
| `average_duration_ms` | integer 或 null | 已完成且有耗时记录的任务平均耗时 |
| `p95_duration_ms` | integer 或 null | 已完成且有耗时记录的任务 P95 |

补充规则：

- 成功率分母为 0 时返回 `null`。
- 平均耗时四舍五入为整数。
- P95 使用 nearest-rank：对耗时升序排列，取 `ceil(0.95 × n)` 对应值。
- 范围筛选和统计分桶均依据任务 `created_at`。
- 幂等重放不创建任务，因此不增加任何统计值。
- 对象后续删除不改变历史上传成功率、成功任务数或上传字节统计。

## 12. Dashboard 上传流量时间序列

### 12.1 请求

```http
GET /v1/dashboard/traffic?from=2026-07-22T00%3A00%3A00Z&to=2026-07-29T00%3A00%3A00Z&interval=day HTTP/1.1
Host: zos-upload-service:8000
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `from` | datetime | 否 | 当前时间前 24 小时 | 查询下界，包含 |
| `to` | datetime | 否 | 当前时间 | 查询上界，排除 |
| `interval` | string | 否 | `hour` | `hour` 或 `day` |

规则：

- 最大查询范围为 31 天。
- 分桶边界按 `APP_TIMEZONE` 计算，默认 `Asia/Shanghai`。
- 每个桶的 `start` 和 `end` 仍以 UTC 输出。
- 响应包含范围内的全部桶，空桶返回零值。
- 每个任务按 `created_at` 所在桶计数。

### 12.2 成功响应

```json
{
  "range": {
    "from": "2026-07-22T00:00:00Z",
    "to": "2026-07-29T00:00:00Z"
  },
  "interval": "day",
  "aggregation_timezone": "Asia/Shanghai",
  "generated_at": "2026-07-29T06:30:05Z",
  "points": [
    {
      "start": "2026-07-21T16:00:00Z",
      "end": "2026-07-22T16:00:00Z",
      "attempt_count": 18,
      "success_count": 17,
      "failure_count": 1,
      "uploading_count": 0,
      "unknown_count": 0,
      "successful_upload_bytes": 156789012
    },
    {
      "start": "2026-07-22T16:00:00Z",
      "end": "2026-07-23T16:00:00Z",
      "attempt_count": 0,
      "success_count": 0,
      "failure_count": 0,
      "uploading_count": 0,
      "unknown_count": 0,
      "successful_upload_bytes": 0
    }
  ]
}
```

## 13. Dashboard 运行日志

### 13.1 日志级别

Dashboard 可查询以下级别：

| `level_name` | `level_no` |
|---|---:|
| `NOTIFY` | 25 |
| `WARNING` | 30 |
| `ERROR` | 40 |
| `CRITICAL` | 50 |

`NOTIFY` 位于标准 `INFO` 和 `WARNING` 之间。SQLite 只持久化 `NOTIFY` 及以上日志。

### 13.2 请求

```http
GET /v1/dashboard/logs?min_level=NOTIFY&limit=100&before_id=1200&event=upload_failed&request_id=example-001&task_id=550e8400-e29b-41d4-a716-446655440000&error_code=STORAGE_TIMEOUT&from=2026-07-28T00%3A00%3A00Z&to=2026-07-29T00%3A00%3A00Z HTTP/1.1
Host: zos-upload-service:8000
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `min_level` | string | 否 | `NOTIFY` | 最低级别：`NOTIFY`、`WARNING`、`ERROR`、`CRITICAL` |
| `limit` | integer | 否 | `100` | 返回数量，范围 `1` 至 `500` |
| `before_id` | integer | 否 | 无 | 只返回 `id < before_id` 的更早日志 |
| `event` | string | 否 | 无 | 精确匹配事件名 |
| `request_id` | string | 否 | 无 | 精确匹配请求 ID |
| `task_id` | string | 否 | 无 | 精确匹配任务 ID |
| `error_code` | string | 否 | 无 | 精确匹配错误码 |
| `from` | datetime | 否 | 无 | 日志时间下界，包含 |
| `to` | datetime | 否 | 无 | 日志时间上界，排除 |

排序固定为 `id DESC`。`before_id` 用于加载更早日志，避免 offset 在持续写入时产生重复或遗漏。

### 13.3 成功响应

```json
{
  "items": [
    {
      "id": 1199,
      "created_at": "2026-07-29T06:20:01Z",
      "level_no": 40,
      "level_name": "ERROR",
      "event": "upload_failed",
      "message": "ZOS 上传超时",
      "request_id": "example-001",
      "task_id": "550e8400-e29b-41d4-a716-446655440000",
      "error_code": "STORAGE_TIMEOUT",
      "details": {
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 125678,
        "object_key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
        "duration_ms": 300012
      }
    }
  ],
  "limit": 100,
  "before_id": 1200,
  "next_before_id": 1199
}
```

字段说明：

- `details` 是数据库 `details_json` 解析后的对象；无结构化详情时为 `null`。
- `next_before_id` 指向当前页面最后一条日志；没有更早记录时为 `null`。
- 日志内容已经执行长度限制、控制字符清洗和敏感字段清除。
- 响应不会包含 AK、SK、Authorization、Cookie、文件内容或完整环境变量。

默认保留策略为 30 天或最多 100000 条，以先达到的限制为准。

## 14. Dashboard 可选 Storage Provider 原生指标

### 14.1 请求

```http
GET /v1/dashboard/storage?preset_key=zos-main&from=2026-07-28T00%3A00%3A00Z&to=2026-07-29T00%3A00%3A00Z HTTP/1.1
Host: zos-upload-service:8000
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `preset_key` | string | 否 | 当前默认预设 | 查询哪个启用预设对应 Bucket 的指标 |
| `from` | datetime | 否 | 当前时间前 24 小时 | Provider Statistics 开始时间 |
| `to` | datetime | 否 | 当前时间 | Provider Statistics 结束时间 |

规则：

- 最大范围为 31 天。
- 时间以 UTC 传给目标预设的 active Storage Provider；`ctyun_zos` 调用 ZOS Statistics。
- 结果默认缓存 300 秒。
- 该接口由目标预设 active storage config 的 `enable_bucket_metrics` 控制。
- Provider 原生指标属于整个 Bucket，可能包含绕过本服务写入或读取的对象和请求。
- Dashboard 的本服务上传流量以 SQLite 统计为准。

### 14.2 功能关闭

```http
HTTP/1.1 200 OK
```

```json
{
  "enabled": false,
  "status": "disabled",
  "storage_preset": "zos-main",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "storage_config_revision": 3,
  "range": {
    "from": "2026-07-28T00:00:00Z",
    "to": "2026-07-29T00:00:00Z"
  },
  "cache": null,
  "statistics": null,
  "storage_info": null
}
```

### 14.3 成功响应

```json
{
  "enabled": true,
  "status": "ok",
  "storage_preset": "zos-main",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "storage_config_revision": 3,
  "range": {
    "from": "2026-07-28T00:00:00Z",
    "to": "2026-07-29T00:00:00Z"
  },
  "cache": {
    "cached": true,
    "stale": false,
    "age_seconds": 42,
    "last_success_at": "2026-07-29T06:29:20Z"
  },
  "statistics": {
    "by_storage_class": {
      "standard": {
        "ops_requested": 123,
        "bytes_sent": 123456,
        "bytes_retrieved": 654321,
        "ops_retrieved": 45,
        "bytes_cross": 0
      },
      "standard_ia": {
        "ops_requested": 0,
        "bytes_sent": 0,
        "bytes_retrieved": 0,
        "ops_retrieved": 0,
        "bytes_cross": 0
      },
      "glacier": {
        "ops_requested": 0,
        "bytes_sent": 0,
        "bytes_retrieved": 0,
        "ops_retrieved": 0,
        "bytes_cross": 0
      },
      "deep_archive": {
        "ops_requested": 0,
        "bytes_sent": 0,
        "bytes_retrieved": 0,
        "ops_retrieved": 0,
        "bytes_cross": 0
      }
    },
    "cdn": {
      "bytes_sent": 123456
    }
  },
  "storage_info": {
    "size_bytes": 1234567890,
    "object_count": 1000,
    "multipart_count": 10,
    "by_storage_class": {
      "standard": {
        "size_bytes": 500000000,
        "object_count": 500,
        "multipart_count": 5
      },
      "standard_ia": {
        "size_bytes": 300000000,
        "object_count": 300,
        "multipart_count": 3
      },
      "glacier": {
        "size_bytes": 200000000,
        "object_count": 150,
        "multipart_count": 2
      },
      "deep_archive": {
        "size_bytes": 234567890,
        "object_count": 50,
        "multipart_count": 0
      }
    }
  }
}
```

### 14.4 指标暂时不可用

已有缓存数据时，服务返回 `200`、`status="degraded"` 和最后一次成功数据：

```json
{
  "enabled": true,
  "status": "degraded",
  "provider": "ctyun_zos",
  "provider_schema_version": 1,
  "storage_config_revision": 3,
  "range": {
    "from": "2026-07-28T00:00:00Z",
    "to": "2026-07-29T00:00:00Z"
  },
  "cache": {
    "cached": true,
    "stale": true,
    "age_seconds": 620,
    "last_success_at": "2026-07-29T06:20:00Z",
    "last_error_code": "STORAGE_METRICS_UNAVAILABLE"
  },
  "statistics": {},
  "storage_info": {}
}
```

示例中的空对象代表最后一次缓存数据的省略位置，实际响应会返回完整缓存内容。

功能已开启且没有可用缓存时：

```http
HTTP/1.1 503 Service Unavailable
```

```json
{
  "error": {
    "code": "STORAGE_METRICS_UNAVAILABLE",
    "message": "Storage Provider 原生指标暂时不可用",
    "request_id": "82d1f9d8-..."
  }
}
```

该故障不会阻塞上传、任务查询、本地统计或日志查询。Provider 为 `ctyun_zos` 时，响应中的统计字段采用本节定义的 ZOS schema。

## 15. 任务状态与恢复语义

### 15.1 状态定义

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `uploading` | 当前进程正在接收文件或上传到对象存储 | 否 |
| `unknown` | 服务暂时无法确认远端结果，恢复任务会继续查询 | 否 |
| `succeeded` | Storage Provider 上传结果和 SQLite 任务结果均已确认 | 是 |
| `failed` | 上传或校验已明确失败 | 是 |

状态流：

```text
uploading ──确认成功──────────────> succeeded
    │
    ├──明确失败──────────────────> failed
    │
    └──异常中断或结果无法确认────> unknown
                                      │
                                      ├──HeadObject 存在────> succeeded
                                      ├──HeadObject 404─────> failed
                                      └──超时或 5xx─────────> unknown
```

对象状态独立于上传状态：

| `object_status` | 含义 |
|---|---|
| `pending` | 尚未确认对象是否存在或元数据不完整 |
| `present` | 已确认对象存在，且大小、ETag 和可选 VersionId 已持久化 |
| `absent` | 已确认上传没有产生对象 |
| `legacy_unverified` | v1 历史成功任务，缺少严格删除所需元数据或删除凭证 |
| `deleting` | 删除请求正在调用 Provider |
| `deleted` | 已确认对象 Key 或精确版本不存在 |
| `delete_unknown` | 删除调用结果无法确认，等待恢复 |

```text
present ──开始删除──> deleting
   │                    ├──确认不存在──> deleted
   │                    ├──明确仍存在──> present
   │                    └──结果未知────> delete_unknown
   │                                         ├──确认不存在──> deleted
   │                                         ├──确认仍存在──> present
   │                                         └──仍无法确认──> delete_unknown
   └──元数据变化──> present + OBJECT_CHANGED
```

### 15.2 启动和周期恢复

服务启动时扫描 `uploading` 和 `unknown` 任务。每个任务使用创建时绑定的 storage config revision；`ctyun_zos` adapter 通过对应 Endpoint、Bucket 和凭证执行恢复：

- `HeadObject` 确认对象存在：更新为 `succeeded` 和 `present`，并写入对象大小、ETag、可选 VersionId 和既有 URL。
- Storage Provider 明确返回对象不存在；`ctyun_zos` 为 `404 NoSuchKey`：更新为 `failed`，`error_code=SERVICE_RESTARTED_OBJECT_NOT_FOUND`。
- Storage Provider 超时、网络异常或 5xx：保持或更新为 `unknown`，`error_code=RECOVERY_PENDING`。
- 周期恢复默认每 60 秒重试 `unknown` 和超过陈旧阈值的 `uploading` 任务。
- 周期恢复同时扫描 `delete_unknown` 和超过删除陈旧阈值的 `deleting` 任务；确认目标不存在时更新为 `deleted`，确认原对象仍存在时恢复为 `present`。

旧 revision 的凭证失效且同一预设的当前 active revision 指向相同 Provider、Endpoint 和 Bucket 时，恢复器可以使用该 revision 再尝试一次；不得改用默认预设或其他预设。

正常上传在 Provider 上传方法成功返回后执行 `HeadObject`，取得严格删除所需元数据后才返回 `201` 和 `delete_token`。

### 15.3 任务级错误码

以下错误码可能出现在任务的 `error_code` 字段中：

| `error_code` | 含义 |
|---|---|
| `FILE_EMPTY` | 文件为空 |
| `FILE_TOO_LARGE` | 文件超过上限 |
| `CLIENT_DISCONNECTED` | 接收文件时客户端断开 |
| `UPLOAD_FAILED` | Storage Provider 明确拒绝或上传失败 |
| `STORAGE_TIMEOUT` | Storage Provider 请求超时 |
| `DATABASE_ERROR` | 任务持久化出现异常 |
| `SERVICE_RESTARTED_OBJECT_NOT_FOUND` | 重启恢复时确认远端对象不存在 |
| `RECOVERY_PENDING` | 当前无法确认远端结果，等待恢复重试 |
| `INTERNAL_ERROR` | 未分类内部异常 |

`CLIENT_DISCONNECTED` 场景通常无法向已经断开的客户端返回 HTTP 响应，调用方可通过任务查询和日志查看结果。

删除相关错误保存在 `delete_error_code`，不覆盖原上传 `error_code`：

| `delete_error_code` | 含义 |
|---|---|
| `OBJECT_CHANGED` | 删除前元数据与上传记录不一致 |
| `DELETE_FAILED` | Provider 明确拒绝删除 |
| `DELETE_PENDING` | 删除结果无法确认，等待恢复 |
| `STORAGE_CONFIG_UNAVAILABLE` | 任务原 storage config 无法加载 |

## 16. 幂等与重试语义

### 16.1 幂等键格式

```http
Idempotency-Key: opaque-key-up-to-128-chars
```

规则：

- Header 可选。
- 长度范围为 1 至 128 个可见 ASCII 字符。
- 值区分大小写。
- 唯一性范围为当前服务实例使用的任务数据库。
- 幂等保证持续到对应任务记录被保留策略删除，默认最长保留 180 天。
- 幂等键绑定第一次请求意图，服务不读取完整文件来比较重复请求内容。
- 幂等键绑定第一次新任务解析得到的 `storage_preset`。

### 16.2 各状态行为

| 已有任务状态 | HTTP | 行为 |
|---|---:|---|
| `succeeded` | 200 | 返回原任务和对象元数据，`delete_token=null`，`Idempotency-Replayed: true` |
| `uploading` | 409 | `UPLOAD_IN_PROGRESS`，返回已有 `task_id` |
| `unknown` | 409 | `UPLOAD_IN_PROGRESS`，返回已有 `task_id` |
| `failed` | 409 | `IDEMPOTENCY_KEY_REUSED`，返回已有 `task_id` |

失败任务需要新的幂等键才能发起新的上传。

重放请求未携带 `X-Storage-Preset` 时，始终按原任务返回，不受当前默认项变化影响。重放请求显式提交与原任务不同的预设时返回 `409 IDEMPOTENCY_SCOPE_MISMATCH`，不读取文件体、不创建任务。

### 16.3 未传幂等键

每次 `POST /v1/uploads` 都会生成新的任务 UUID 和对象 Key。同一个文件重复上传会产生不同任务和 URL。

### 16.4 调用方重试建议

- 收到 `201` 或幂等重放 `200`：保存结果，停止重试。
- 收到 `409 UPLOAD_IN_PROGRESS`：查询返回的 `task_id`，等待任务进入终态。
- 收到 `409 IDEMPOTENCY_KEY_REUSED`：确认业务意图后使用新幂等键。
- 收到 `409 IDEMPOTENCY_SCOPE_MISMATCH`：使用原预设重放，或为新的目标预设使用新幂等键。
- 收到 `503 UPLOAD_CAPACITY_EXCEEDED`：遵循 `Retry-After`。
- 客户端等待响应时发生网络超时：使用相同幂等键重新请求，或先查询已知 `task_id`。
- 未使用幂等键的自动重试会产生重复对象风险。
- 首次 `201` 响应丢失时，幂等重放可以恢复 URL 和对象元数据，但不能恢复删除凭证；在统一内部认证或受控管理恢复能力实现前，该对象不能通过公开删除 API 删除。
- 删除接口按任务状态天然幂等，不使用 `Idempotency-Key`；网络超时后使用相同任务 ID 和 `delete_token` 重试或查询任务详情。
- 删除返回 `202 DELETE_PENDING` 时停止主动重试 Provider 调用，轮询任务详情等待 `object_status` 变为 `deleted` 或 `present`。

## 17. HTTP 错误码总表

| HTTP | `code` | 说明 |
|---:|---|---|
| 400 | `FILE_REQUIRED` | 新上传缺少 `file` 字段 |
| 400 | `FILE_EMPTY` | 文件内容为空 |
| 400 | `BAD_REQUEST` | 请求头、multipart、UUID、时间或查询参数错误 |
| 400 | `STORAGE_PRESET_INVALID` | `preset_key` 或 `X-Storage-Preset` 格式不合法 |
| 400 | `STORAGE_CONFIG_INVALID` | Provider、schema version、设置字段、URL、Bucket、凭证格式或参数不合法 |
| 400 | `STORAGE_CREDENTIALS_REQUIRED` | 首次配置或 Provider/schema version/Endpoint 变化时缺少完整 AK/SK |
| 403 | `DELETE_TOKEN_INVALID` | 删除凭证缺失、错误或与任务不匹配 |
| 404 | `TASK_NOT_FOUND` | 指定任务不存在 |
| 404 | `STORAGE_PRESET_NOT_FOUND` | 指定存储预设不存在 |
| 409 | `CONFIG_REVISION_CONFLICT` | 设置更新基于过期 revision |
| 409 | `PRESET_STATE_CONFLICT` | 预设状态更新冲突或试图禁用默认预设 |
| 409 | `DEFAULT_PRESET_CONFLICT` | 默认项切换的并发条件不匹配或目标未启用 |
| 409 | `STORAGE_PRESET_DISABLED` | 上传显式指定了已禁用预设 |
| 409 | `IDEMPOTENCY_SCOPE_MISMATCH` | 幂等键已绑定其他存储预设 |
| 409 | `UPLOAD_IN_PROGRESS` | 幂等键对应任务仍在处理或待确认 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 幂等键已经绑定失败任务 |
| 409 | `OBJECT_NOT_DELETABLE` | 任务或对象状态不允许严格删除 |
| 409 | `OBJECT_CHANGED` | 删除前对象元数据与上传记录不一致 |
| 409 | `DELETE_IN_PROGRESS` | 对象正在删除或等待确认 |
| 413 | `FILE_TOO_LARGE` | 文件或请求体超过上限 |
| 500 | `DATABASE_ERROR` | SQLite 创建、读取或更新失败 |
| 500 | `SETTINGS_STORAGE_ERROR` | 设置加密、持久化或 Client 切换失败 |
| 500 | `INTERNAL_ERROR` | 未分类服务异常 |
| 502 | `UPLOAD_FAILED` | Storage Provider 明确拒绝或上传失败 |
| 502 | `STORAGE_TIMEOUT` | Storage Provider 请求超时 |
| 502 | `DELETE_FAILED` | Storage Provider 明确拒绝删除 |
| 502 | `STORAGE_ENDPOINT_UNREACHABLE` | 设置测试无法连接 Endpoint |
| 502 | `STORAGE_CREDENTIALS_REJECTED` | 设置测试中的 AK/SK 被拒绝 |
| 502 | `STORAGE_BUCKET_UNAVAILABLE` | 设置测试无法访问 Bucket |
| 503 | `UPLOAD_CAPACITY_EXCEEDED` | 上传并发槽位已满 |
| 503 | `STORAGE_NOT_CONFIGURED` | 显式预设尚未激活配置，或兼容设置接口没有默认配置 |
| 503 | `STORAGE_DEFAULT_NOT_CONFIGURED` | 未指定预设且没有可用默认预设 |
| 503 | `STORAGE_CONFIG_UNAVAILABLE` | 删除任务绑定的原配置无法加载或解密 |
| 503 | `NOT_READY` | 服务依赖项未达到就绪条件 |
| 503 | `STORAGE_METRICS_UNAVAILABLE` | 可选 Storage Provider 原生指标暂时不可用且无缓存 |

`202 DELETE_PENDING` 是已接受但结果不确定的删除状态，不属于成功响应，也不放入 4xx/5xx 错误表。

## 18. 调用示例

### 18.1 带幂等键上传

```bash
curl --fail-with-body \
  -X POST \
  -H 'X-Request-ID: example-001' \
  -H 'Idempotency-Key: ingest-job-20260729-001' \
  -H 'X-Storage-Preset: zos-main' \
  -F 'file=@./report.pdf;type=application/pdf' \
  http://zos-upload-service:8000/v1/uploads
```

### 18.2 查询任务列表

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/upload-tasks?limit=50&offset=0&status=succeeded'
```

### 18.3 查询任务详情

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/upload-tasks/550e8400-e29b-41d4-a716-446655440000'
```

### 18.4 删除上传对象

```bash
curl --fail-with-body \
  -X DELETE \
  -H 'X-Request-ID: delete-example-001' \
  -H 'X-Delete-Token: <上传成功响应中的 delete_token>' \
  'http://zos-upload-service:8000/v1/upload-tasks/550e8400-e29b-41d4-a716-446655440000/object'
```

### 18.5 查询就绪状态

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/readyz'
```

### 18.6 查询 24 小时上传概览

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/dashboard/summary'
```

### 18.7 查询按小时流量

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/dashboard/traffic?interval=hour'
```

### 18.8 查询 ERROR 及以上日志

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/dashboard/logs?min_level=ERROR&limit=100'
```

### 18.9 查询存储预设

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/settings/storage/presets'
```

### 18.10 查询当前默认存储设置

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/settings/storage'
```

### 18.11 测试 ZOS 候选设置

```bash
curl --fail-with-body \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-Settings-Request: true' \
  --data @storage-test.json \
  'http://zos-upload-service:8000/v1/settings/storage/test'
```

### 18.12 保存并激活默认 ZOS 设置

```bash
curl --fail-with-body \
  -X PUT \
  -H 'Content-Type: application/json' \
  -H 'X-Settings-Request: true' \
  --data @storage-update.json \
  'http://zos-upload-service:8000/v1/settings/storage'
```

### 18.13 查询 Provider 原生指标

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/dashboard/storage'
```

## 19. 调用方注意事项

- 上传请求使用 `multipart/form-data`，字段名固定为 `file`。
- 需要特定 Endpoint 与 Bucket 时提交服务端预先分配的 `X-Storage-Preset`；不要在请求中传原始存储参数。
- 未传 `X-Storage-Preset` 时使用当前默认项；显式预设失败不会自动回退。
- 文件内容直接放入 multipart，避免 Base64 编码和 JSON 包装。
- 接近 200 MiB 的文件需要预留足够客户端超时时间。
- `url` 作为不透明字符串保存和传递。
- 需要删除能力的调用方必须同时安全保存 `task_id` 和 `delete_token`；其他对象元数据可用于审计，但不得自行构造 ZOS 删除请求。
- `delete_token` 只发送到本服务的 `X-Delete-Token` Header，不写入 URL、普通日志、浏览器存储或公网 API 请求。
- 调用方不能通过删除接口指定 Bucket、Key、URL、Provider 或 VersionId。
- `202 DELETE_PENDING` 不是删除成功；继续查询任务的 `object_status`。
- `deleted` 只确认 ZOS 源对象或精确版本不存在，不代表 CDN 和第三方缓存已经失效。
- `unknown` 属于可恢复的非终态，调用方应继续查询。
- 任务列表、Dashboard 和日志包含原始文件名、对象 Key 和公网 URL，只能在受控局域网访问。
- 排查问题时提供 `request_id` 和 `task_id`。
- 业务上传调用方永远不传递对象存储凭证；ZOS AK/SK 只通过 storage settings API 写入服务。
- storage settings GET、页面和日志只返回 masked 凭证状态。
- 设置客户端更新配置前读取当前 `revision`，并把它作为 `expected_revision` 提交。
- Provider、`provider_schema_version` 或 Endpoint 变化时重新提交完整 AK/SK；三者均不变时才允许省略凭证对象或字段。
- 设置请求优先通过局域网 HTTPS 发送，反向代理和调用方禁止记录请求体。
- 局域网内任何可访问服务端口的客户端都能修改设置，网络边界需要覆盖 Dashboard 与设置 API。
- Dashboard 本地统计表示本服务处理的上传；Provider 原生指标表示整个 Bucket 的活动。
- 对象仍可能存在的任务不会按年龄清理；只有对象状态为 `absent` 或 `deleted` 的终态任务才适用默认 180 天保留期。日志默认保留 30 天或最多 100000 条。
