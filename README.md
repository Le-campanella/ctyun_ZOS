# 局域网 ZOS 文件上传服务

这是一个单容器、无登录的内部文件网关：接收局域网服务提交的单个文件，临时落盘后上传到当前激活的天翼云 ZOS Bucket，记录 SQLite 任务台账，并返回对象 Key 和公网 URL。请求结束后不保留文件本体。

## 当前发布基线

- 当前 HTTP 路径命名空间为 `/v1`；首次上传成功返回任务、对象元数据和一次性 `delete_token`，幂等重放不会补发 token。
- 当前仓库数据库：schema v3；上传返回成功前会用 HeadObject 校验远端大小，并在任务中保存 ETag、可选 VersionId 和对象状态。
- 数据库与 Runtime 已支持独立的多预设配置 revision、默认切换和按 config ID 缓存 Provider；`/v1/settings/storage/presets` 已开放局域网管理 API。上传接口可通过 `X-Storage-Preset` 选择已启用预设，未传时使用默认项。
- Dashboard 设置页已支持多预设创建、测试、更新、启停和默认切换；监控页可选择预设执行真实上传测试，并只读展示对象与删除状态。
- `DELETE /v1/upload-tasks/{task_id}/object` 已开放严格删除：只接受任务级 `X-Delete-Token`，使用任务原配置校验对象元数据、精确删除 VersionId 并再次确认对象不存在。
- 后台探测、恢复和维护任务由 supervisor 自动重试；任一后台任务异常时 `/readyz` 降级并记录 CRITICAL 日志。
- [docs/API.md](docs/API.md) v3 与 [docs/PLAN.md](docs/PLAN.md) v6 仍是未发布目标契约；仓库实现已推进到 Dashboard v3，正式生产行为以部署版本为准。
- 当前服务生成的 `/openapi.json` 是已实现接口的机器可读契约。

实施记录见 [WORKLOG.md](WORKLOG.md)，外部审查与实施建议见 [docs/CTYUN_ZOS_REVIEW_AND_V3_IMPLEMENTATION.md](docs/CTYUN_ZOS_REVIEW_AND_V3_IMPLEMENTATION.md)。

## 启动

要求 Docker Engine 和 Docker Compose。

```bash
cp .env.example .env
openssl rand -base64 32 | tr '+/' '-_'
```

把生成结果写入 `.env` 的 `SETTINGS_ENCRYPTION_KEY`。该密钥用于加密数据库里的 AK/SK，后续必须单独备份；密钥丢失或更换会导致已保存凭证无法解密。

本机验证保持 `LISTEN_IP=127.0.0.1`。局域网使用时，将其改为服务器实际内网 IP，再启动：

```bash
docker compose up -d --build
docker compose ps
```

打开：

- 监控页：`http://<内网IP>:8000/dashboard`
- 存储设置：`http://<内网IP>:8000/dashboard/settings`
- 存活检查：`http://<内网IP>:8000/healthz`
- 就绪检查：`http://<内网IP>:8000/readyz`

服务未配置 ZOS 时仍可访问 Dashboard 和设置页；`/readyz` 返回 `STORAGE_NOT_CONFIGURED`，未指定预设的上传接口返回 `STORAGE_DEFAULT_NOT_CONFIGURED`。在设置页填写 SDK Endpoint、Bucket、公网访问根地址及 AK/SK，先测试连接，再保存激活。

## 数据库升级

服务启动时使用 `PRAGMA user_version` 检测数据库版本：

- 空数据库直接创建 schema v3。
- schema v1 依次事务升级到 v2、v3。
- schema v2 事务升级到 v3。
- 升级前使用 SQLite Online Backup 在数据库旁创建 `zos-upload.db.pre-v3-<timestamp>`，包括 WAL 中已提交数据。
- 升级后执行 `integrity_check`、外键、默认预设、active revision 和引用完整性检查；失败时停止启动。

schema v3 保留原 task ID、storage config ID 和 revision。历史成功任务标记为 `legacy_unverified`，不会获得删除凭证。不要让旧镜像直接打开已经升级的数据库；需要回滚旧镜像时，必须同时恢复升级前备份。

## 调用上传接口

只验证局域网接收链路、不上传 ZOS：

```bash
curl -fsS -F 'file=@./example.pdf' \
  http://<内网IP>:8000/v1/uploads/validate
```

同一功能也可以在 Dashboard 的“局域网文件接收测试”区域操作。“真实上传到 ZOS”开关默认关闭，此时只执行 multipart、大小限制和临时文件读写；开启后可以选择任一已启用预设，调用正式上传接口、创建任务并返回公网链接。

真实上传：

```bash
curl -fsS \
  -H 'X-Request-ID: caller-service-001' \
  -H 'Idempotency-Key: business-job-001' \
  -H 'X-Storage-Preset: archive' \
  -F 'file=@./example.pdf' \
  http://<内网IP>:8000/v1/uploads
```

`X-Storage-Preset` 可省略；省略时使用当前默认预设。显式预设不存在、被禁用或上传失败时直接报错，不会回退到默认预设。幂等键会绑定首次任务使用的预设。

成功响应：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_preset": "default",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "size_bytes": 125678,
  "content_type": "application/pdf",
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "delete_token": "仅首次响应返回的敏感凭证"
}
```

单文件最大 200 MiB，默认接受所有类型。正式上传固定设置 `public-read` 对象 ACL；公网 URL 只在上传成功时返回，服务不代理下载。数据库只保存 `delete_token` 的 SHA-256，调用方需要自行安全保存明文；首次 `201` 丢失后无法补发。

严格删除：

```bash
curl -fsS -X DELETE \
  -H 'X-Delete-Token: <首次上传响应中的 delete_token>' \
  http://<内网IP>:8000/v1/upload-tasks/<task_id>/object
```

删除请求不能提交 Bucket、Key、URL 或 VersionId。服务只使用任务绑定的原存储配置和对象元数据定位目标；预设禁用、默认切换或新增 revision 不改变删除目标。`202 DELETE_PENDING` 表示结果未知，不是删除成功；启动和周期恢复会继续确认 `delete_unknown` 及超过 `STALE_DELETE_SECONDS`（默认 900 秒）的 `deleting` 任务。

删除状态迁移会写入不含 token 的 `object_delete_*` 审计事件。这些事件不受普通日志的期限和条数清理影响；任务台账按保留策略清理后，删除审计仍然保留。

## 测试

自动测试使用与生产一致的 Python 3.11 镜像，不需要本机 Python 环境：

```bash
docker build --target test -t zos-upload-service:test .
docker run --rm zos-upload-service:test
```

测试覆盖严格删除、token 隔离、VersionId、元数据变化、并发删除、未知结果、重启/陈旧任务恢复、永久审计和数据库失败，以及多预设路由和在途快照、上传、SQLite v1/v2/v3 迁移与回滚、凭证加密和清洗、统计、Dashboard 多预设管理与设置接口。

## 部署到局域网服务器

服务器需要安装 Docker 和 Compose，并允许部署用户直接使用 Docker。复制本地部署配置：

```bash
cp .deploy.env.example .deploy.env
```

填写目标、远程目录、健康/就绪 URL 和 SSH 私钥绝对路径后执行：

```bash
./deploy.sh
```

脚本只部署已提交代码。部署前运行完整测试和生产镜像构建，使用 SQLite Online Backup
保存远程数据库并记录 schema；部署后依次检查 `/healthz` 和 `/readyz`。检查失败时回滚
旧镜像；跨 schema 升级还会恢复迁移前数据库，备份继续保留在数据库卷的
`deploy-backups/` 目录。

部署不会覆盖服务器 `.env`，也不会删除 `ctyun_zos` 项目的数据库和临时文件卷。

真实 ZOS 验收需要有效的 Endpoint、Bucket、AK/SK 和可公网访问的 Bucket 域名，至少检查：

1. TXT、PDF、图片、超过 16 MiB 及接近 200 MiB 文件。
2. ZOS 对象 Key、Content-Type、大小与任务记录一致。
3. 从公网环境实际访问返回 URL。
4. 错误 Endpoint、凭证和 Bucket 能返回明确错误。
5. 重启后未决任务可通过 HeadObject 恢复。

可重复的真实 ZOS 上传、公网 HEAD 和严格删除验收：

```bash
ACCEPT_BASE_URL=http://<内网IP>:8000 \
ACCEPT_SIZE_MIB=20 \
ACCEPT_CONCURRENCY=4 \
./scripts/accept-zos.sh
```

默认使用当前默认预设；可通过 `ACCEPT_PRESET` 指定已启用预设。脚本只删除自己刚上传且
持有对应一次性 token 的对象，不输出删除 token。

## 运维边界

- 服务没有应用层认证，只允许绑定可信局域网 IP；不要把端口暴露到公网。
- 设置请求会携带 AK/SK，正式环境应放在内网 HTTPS 反向代理后，且禁止代理日志记录请求体。
- `zos-database` 保存 SQLite 和加密后的配置；`zos-temporary` 只保存请求期临时文件。数据库卷需要定期快照或在线备份。
- 服务为上传对象请求 `public-read` canned ACL；Bucket Policy、账号权限、生命周期、未完成 multipart 清理和对象删除仍在 ZOS 侧管理。
- 当前标准 S3 Client 覆盖 HeadBucket、上传、multipart 和 HeadObject。ZOS 扩展 Bucket 指标依赖兼容 SDK；关闭指标不会影响核心上传与本地 Dashboard。
