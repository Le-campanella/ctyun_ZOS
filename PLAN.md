# ZOS 轻量上传服务实施计划

> 状态：方案已确认，待实现。
>
> 调用方接口契约见 [API.md](API.md)。

## 1. 目标

提供一个内部 HTTP 服务：

1. 接收局域网其他服务上传的单个文件。
2. 将文件同步上传到固定的天翼云 ZOS Bucket。
3. 上传成功后返回对象 Key 和公网 URL，失败时返回明确错误。
4. 使用 SQLite 保存最小上传任务台账，并提供任务列表查询。

## 2. 范围边界

服务只管理“上传任务记录”，不管理 ZOS 中的资料：

- 不持久化保存文件本体。
- 不提供 ZOS 文件的下载、更新、删除或列表接口。
- 不提供任务修改、删除、重试或统计接口。
- 不建设资料管理后台。
- 不做去重、版本管理、内容搜索或文件分类。
- 不引入消息队列、异步 Worker、ORM 或数据库迁移框架。
- 不允许调用方指定 Bucket、对象 ACL 或任意对象 Key。
- 第一版不做调用方认证；服务端口只暴露在受控局域网内。

文件保留和过期清理由 ZOS 生命周期规则负责。

## 3. 技术方案

- Python 3
- FastAPI
- S3 Client
- Python 标准库 `sqlite3`
- 单容器、单进程、单实例
- SQLite 文件挂载到持久化目录

ZOS 客户端和 SQLite 连接在进程内使用，不为单一实现增加额外分层。

## 4. 服务结构

```text
局域网服务
    │ POST 文件
    ▼
轻量上传服务 ─────任务状态────> SQLite
    │ PutObject / 分段上传
    ▼
ZOS Bucket
    │ 公网 HTTPS URL
    ▼
第三方公网 API
```

文件只在请求处理期间以流或临时缓冲方式存在，上传完成或失败后不保留文件本体。

## 5. 文件与对象约定

- 单文件最大 200 MiB（`209715200` 字节）。
- 接受所有文件类型。
- 每次请求只允许上传一个文件。
- 对象名称使用任务 UUID，不使用原始文件名。
- 对象按 `年/月/日` 分目录。
- 日期按 `Asia/Shanghai` 计算。
- 有安全扩展名时保留扩展名，没有时只使用 UUID。

对象 Key 示例：

```text
2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf
```

Bucket 权限、链接有效性和实际公网可访问性由 ZOS 配置负责，本服务不检查或修改。

## 6. 上传任务台账

SQLite 只建立一张表：

```sql
CREATE TABLE upload_tasks (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    public_url  TEXT,
    status      TEXT NOT NULL CHECK (status IN ('uploading', 'succeeded', 'failed')),
    size_bytes  INTEGER,
    error_code  TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
```

字段约定：

| 字段 | 说明 |
|---|---|
| `id` | 任务 UUID，同时用于生成 ZOS 对象名称 |
| `filename` | 调用方提交的原始文件名；未提供时记为 `unnamed` |
| `public_url` | 上传成功后返回的完整 URL，否则为空 |
| `status` | `uploading`、`succeeded` 或 `failed` |
| `size_bytes` | 成功时为实际文件大小；失败且无法确定时为空 |
| `error_code` | 失败时的短错误码，否则为空 |
| `created_at` | UTC ISO 8601 创建时间 |
| `finished_at` | UTC ISO 8601 完成时间，处理中为空 |

不保存 Content-Type、文件哈希、调用方、重试次数、Bucket 或文件内容。

服务启动时将遗留的 `uploading` 任务更新为 `failed`，错误码设为 `SERVICE_RESTARTED`。

## 7. API

### 上传文件

```http
POST /v1/uploads
Content-Type: multipart/form-data

file=<binary>
```

成功响应：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf"
}
```

### 查询上传任务

```http
GET /v1/upload-tasks?limit=50&offset=0
```

- 按 `created_at` 倒序返回。
- `limit` 默认 50，最大 200。
- 第一版不提供筛选、详情、修改或删除。

### 健康检查

```http
GET /healthz
```

只检查服务进程，不请求 ZOS。

完整请求和响应格式以 `API.md` 为准。

## 8. 上传状态流

1. 接收并解析 `file`。
2. 生成任务 UUID。
3. 写入 `uploading` 任务记录。
4. 验证文件非空且不超过 200 MiB。
5. 按 `YYYY/MM/DD/{task_id}.{扩展名}` 生成对象 Key。
6. 流式上传；超过 SDK 分段阈值时由 S3 Client 自动分段。
7. 成功时写入 `public_url`、`size_bytes`、`finished_at`，状态改为 `succeeded`。
8. 失败时写入 `error_code`、`finished_at`，状态改为 `failed`。
9. 上传成功才向调用方返回 `201`、`task_id`、`key` 和 `url`。

缺少 `file` 或 multipart 格式错误时尚未生成有效任务，不写入任务表。

不额外执行 `HEAD Object`；S3 Client 的成功响应就是第一版的上传确认。

## 9. 错误约定

| 状态码 | 场景 |
|---|---|
| `400` | 缺少文件、文件为空或请求格式错误 |
| `413` | 文件超过 200 MiB |
| `502` | ZOS 上传失败或超时 |
| `500` | 服务或任务数据库异常 |

已经创建任务时，失败响应包含 `task_id`；尚未创建任务时不包含。

## 10. 配置

```text
ZOS_ENDPOINT
ZOS_PUBLIC_BASE_URL
ZOS_BUCKET
ZOS_ACCESS_KEY
ZOS_SECRET_KEY
DATABASE_PATH
MAX_UPLOAD_BYTES
REQUEST_TIMEOUT_SECONDS
APP_TIMEZONE
```

- `DATABASE_PATH` 指向持久化卷中的 SQLite 文件。
- `MAX_UPLOAD_BYTES=209715200`。
- `APP_TIMEZONE=Asia/Shanghai`。
- AK/SK 通过部署环境的 Secret 注入，不写入数据库或日志。
- ZOS 凭证仅授予目标 Bucket 所需的上传和分段上传权限。

## 11. SDK 策略

当前需求只使用标准 S3 能力，不依赖 ZOS 图片处理、软链接等扩展接口。

1. 先用仍在维护的标准 S3 Client 验证 ZOS 兼容性。
2. 验证小文件、分段上传、200 MiB 边界和 URL 生成。
3. 只有标准客户端不兼容时才使用目录中的 ZOS 官方 Python SDK。

## 12. 日志

每次请求记录：

- 请求 ID 和任务 ID。
- 原始文件名、文件大小和 Content-Type。
- ZOS 对象 Key。
- 上传耗时。
- 成功或失败及短错误码。

使用结构化日志，不额外引入监控 SDK。

## 13. 验证与验收

### 自动检查

- 无文件、空文件和超过 200 MiB 的文件处理正确。
- 对象 Key 符合日期目录和 UUID 命名规则。
- ZOS 失败时任务状态为 `failed`，且不返回 URL。
- ZOS 成功时任务状态为 `succeeded`，URL 与上传响应一致。
- 服务重启后遗留的 `uploading` 任务变为 `failed`。
- 任务列表按时间倒序并正确分页。

### 集成检查

- 上传 TXT、PDF、图片各一个。
- 校验 ZOS 对象和 SQLite 任务记录一致。
- 验证文件超过分段阈值时正常上传。
- 验证服务重启后任务历史仍然存在。

### 完成标准

- 单次调用完成“创建任务、上传、记录结果、返回 URL”。
- 服务不持久化文件本体。
- SQLite 只保存约定的八个字段。
- 可以分页查看全部上传任务及结果。
- 服务不管理 ZOS 对象或链接权限。
- AK/SK 不暴露给调用方、数据库或日志。

## 14. 实施顺序

1. 确认 Bucket、ZOS Endpoint、公网访问根地址和 AK/SK。
2. 用最小脚本验证 S3 Client 可上传并返回对象 URL。
3. 创建 SQLite 单表和启动恢复逻辑。
4. 按 `API.md` 实现上传、任务列表和健康检查。
5. 增加大小限制、错误映射和结构化日志。
6. 完成自动检查和真实 ZOS 集成验证。
7. 以单容器、单进程方式部署，并挂载 SQLite 持久化目录。

## 15. 已确认决策

1. 第一版不验证调用方身份。
2. 技术栈为 Python 3、FastAPI、S3 Client 和 SQLite。
3. 单文件最大 200 MiB，接受所有文件类型。
4. 文件本体不持久化到本服务。
5. ZOS 对象按 `YYYY/MM/DD/{UUID}.{扩展名}` 命名。
6. SQLite 只保存最小任务台账。
7. 服务提供上传任务列表，不提供资料管理功能。
