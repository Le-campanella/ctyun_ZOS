# ZOS 轻量上传服务接口文档

> 面向对象：局域网内调用本服务的其他服务，以及负责接入的 Agent。
>
> 当前版本：`v1`

## 1. 能力范围

本服务提供：

1. 上传单个文件到 ZOS。
2. 返回上传任务 ID、ZOS 对象 Key 和 URL。
3. 分页查询上传任务历史。
4. 返回服务健康状态。

任务历史只记录上传过程，不提供 ZOS 文件的下载、更新、删除或管理能力。

## 2. 通用约定

- 示例地址：`http://zos-upload-service:8000`
- 请求和响应使用 UTF-8。
- 第一版没有调用方认证，只允许在受控局域网内访问。
- 单文件上限为 200 MiB，即 `209715200` 字节。
- 接受所有文件类型。
- 每次请求只能上传一个文件。
- 调用方可以传入 `X-Request-ID`；未传时由服务生成。
- 服务在响应头中返回最终使用的 `X-Request-ID`。
- 时间字段使用 UTC ISO 8601 格式，例如 `2026-07-28T08:30:00Z`。
- URL 权限、有效期和实际公网可访问性由 ZOS Bucket 配置决定。

## 3. 上传文件

### 请求

```http
POST /v1/uploads HTTP/1.1
Host: zos-upload-service:8000
Content-Type: multipart/form-data; boundary=...
X-Request-ID: optional-request-id
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 二进制文件 | 是 | 单个文件，最大 200 MiB |

文件部分的 `Content-Type` 会保存到 ZOS；未提供时使用 `application/octet-stream`。

原始文件名会写入任务记录，未提供时记为 `unnamed`，但不会直接作为 ZOS 对象名称。ZOS 对象按下面的格式生成：

```text
YYYY/MM/DD/{task_id}.{安全扩展名}
```

示例：

```text
2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf
```

没有安全扩展名时，对象名称只有任务 UUID。

### 成功响应

```http
HTTP/1.1 201 Created
Content-Type: application/json
X-Request-ID: 82d1f9d8-...
```

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 上传任务 UUID |
| `key` | string | ZOS 对象 Key |
| `url` | string | 与该对象对应的 URL |

收到 `201` 表示 S3 Client 已确认上传成功，任务状态已经更新为 `succeeded`。调用方应直接保存和使用 `url`，不要自行拼接或修改。

### 失败响应

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "文件不能超过 200 MiB",
    "request_id": "82d1f9d8-..."
  }
}
```

| HTTP 状态码 | `code` | 含义 | 是否产生任务记录 |
|---|---|---|---|
| `400` | `FILE_REQUIRED` | 缺少 `file` 字段 | 否 |
| `400` | `FILE_EMPTY` | 文件内容为空 | 是，状态为 `failed` |
| `400` | `BAD_REQUEST` | multipart 请求格式错误 | 否 |
| `413` | `FILE_TOO_LARGE` | 文件超过 200 MiB | 是，状态为 `failed` |
| `502` | `UPLOAD_FAILED` | ZOS 上传失败 | 是，状态为 `failed` |
| `502` | `ZOS_TIMEOUT` | ZOS 请求超时 | 是，状态为 `failed` |
| `500` | `DATABASE_ERROR` | 任务数据库异常 | 不保证 |
| `500` | `INTERNAL_ERROR` | 服务内部异常 | 视异常阶段而定 |

- 已经创建任务时，响应包含 `task_id`。
- 尚未创建任务时，响应不包含 `task_id`。
- 失败响应不包含 `key` 或 `url`。
- 错误响应中的 `message` 仅供阅读，程序逻辑应判断 `code`。

## 4. 查询上传任务

### 请求

```http
GET /v1/upload-tasks?limit=50&offset=0 HTTP/1.1
Host: zos-upload-service:8000
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `limit` | integer | 否 | `50` | 返回数量，范围 `1` 至 `200` |
| `offset` | integer | 否 | `0` | 跳过的任务数量，必须大于等于 `0` |

任务按 `created_at` 从新到旧排列。

### 成功响应

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "items": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "filename": "report.pdf",
      "public_url": "https://public-bucket.example.com/2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf",
      "status": "succeeded",
      "size_bytes": 125678,
      "error_code": null,
      "created_at": "2026-07-28T08:30:00Z",
      "finished_at": "2026-07-28T08:30:02Z"
    },
    {
      "id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "filename": "archive.zip",
      "public_url": null,
      "status": "failed",
      "size_bytes": null,
      "error_code": "FILE_TOO_LARGE",
      "created_at": "2026-07-28T08:20:00Z",
      "finished_at": "2026-07-28T08:20:01Z"
    }
  ],
  "limit": 50,
  "offset": 0
}
```

任务字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 任务 UUID |
| `filename` | string | 原始文件名 |
| `public_url` | string 或 null | 成功时的 URL |
| `status` | string | `uploading`、`succeeded` 或 `failed` |
| `size_bytes` | integer 或 null | 成功时为实际文件大小；失败且无法确定时为空 |
| `error_code` | string 或 null | 失败错误码 |
| `created_at` | string | 任务创建时间 |
| `finished_at` | string 或 null | 任务完成时间 |

调用方可以不断增加 `offset`，直到 `items` 为空。第一版不返回任务总数，不提供状态筛选、任务详情、修改或删除。

### 参数错误

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json
```

```json
{
  "error": {
    "code": "BAD_REQUEST",
    "message": "limit 必须在 1 到 200 之间",
    "request_id": "82d1f9d8-..."
  }
}
```

## 5. 健康检查

### 请求

```http
GET /healthz HTTP/1.1
Host: zos-upload-service:8000
```

### 成功响应

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "status": "ok"
}
```

该接口只表示服务进程可以响应，不保证 ZOS 当前可用。

## 6. 任务状态

```text
uploading ──上传成功──> succeeded
    │
    ├──校验或上传失败──> failed
    │
    └──服务异常重启────> failed (SERVICE_RESTARTED)
```

`succeeded` 和 `failed` 都是终态，第一版不支持重新执行任务。

## 7. 重试语义

第一版不提供幂等保证：

- 每次 `POST /v1/uploads` 都生成新的任务 UUID 和对象 Key。
- 同一个文件重复上传会产生不同任务和 URL。
- 调用方等待响应时发生网络超时，文件可能已经上传成功；再次调用可能产生重复对象。
- 调用方只应在接受重复风险时自动重试。

重复对象由 ZOS 生命周期策略处理，本服务不提供删除接口。

## 8. 调用示例

### 上传

```bash
curl --fail-with-body \
  -X POST \
  -H 'X-Request-ID: example-001' \
  -F 'file=@./report.pdf;type=application/pdf' \
  http://zos-upload-service:8000/v1/uploads
```

### 查看任务

```bash
curl --fail-with-body \
  'http://zos-upload-service:8000/v1/upload-tasks?limit=50&offset=0'
```

## 9. 调用方注意事项

- 上传请求必须是 `multipart/form-data`，字段名必须是 `file`。
- 不要把文件编码成 Base64 后放进 JSON。
- 为接近 200 MiB 的文件预留足够的客户端超时时间。
- 将 `url` 当作不透明字符串使用。
- 任务列表包含原始文件名和公网 URL，只能在受控局域网内访问。
- 排查失败时提供 `request_id` 和 `task_id`，不要记录或传递 ZOS AK/SK。
