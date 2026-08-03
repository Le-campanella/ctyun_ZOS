# 局域网 ZOS 文件上传服务

这是一个单容器内部文件网关：上传数据面供受控局域网服务调用，Dashboard、设置、日志和完整任务查询由管理员密钥保护。服务临时接收文件并上传到选定对象存储，记录 SQLite 任务台账并返回对象 Key 和公网 URL；请求结束后不保留文件本体。

## 当前发布基线

- 当前 HTTP 路径命名空间为 `/v1`；首次上传成功返回任务、对象元数据和一次性 `delete_token`，幂等重放不会补发 token。
- 当前仓库数据库：schema v5；任务保存 `client_id`，幂等键按调用方隔离。上传调用 Provider 前先持久化已接收大小，成功返回前用 HeadObject 校验远端大小，并保存 ETag、可选 VersionId 和对象状态。
- 数据库与 Runtime 已支持独立的多预设配置 revision、默认切换和按 config ID 缓存 Provider；`/v1/settings/storage/presets` 已开放局域网管理 API。上传接口可通过 `X-Storage-Preset` 选择已启用预设，未传时使用默认项。
- Dashboard 设置页已支持多服务预设的创建、测试、更新、启停和默认切换；每项预设独立绑定 Provider、Endpoint、Bucket 和凭证。监控页可选择预设执行真实上传测试，并只读展示对象与删除状态。
- 内置 Provider 包括 `ctyun_zos` 和 `s3_compatible`。后者适用于支持 S3 API、HeadObject、DeleteObject 和 `public-read` ACL 的其他对象存储服务；不声称兼容所有厂商私有协议。
- `DELETE /v1/upload-tasks/{task_id}/object` 已开放严格删除：只接受任务级 `X-Delete-Token`，使用任务原配置校验对象元数据、精确删除 VersionId 并再次确认对象不存在。
- 后台探测、恢复和维护任务由 supervisor 自动重试；任一后台任务异常时 `/readyz` 降级并记录 CRITICAL 日志。
- [docs/API.md](docs/API.md) v3 与 [docs/PLAN.md](docs/PLAN.md) v6 已同步当前仓库；服务器尚未部署本提交时，以服务器自身的 OpenAPI 为准。
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

首次启动前必须在 `.env` 设置至少 32 字符的 `ADMIN_API_KEYS`。浏览器访问 Dashboard 时使用 HTTP Basic，用户名固定为 `admin`、密码为当前管理员 key；API 客户端可以使用 `Authorization: Bearer <key>` 或 `X-Admin-Key: <key>`。轮换时先用逗号同时配置新旧 key，调用方切换后再移除旧 key。

`CLIENT_API_KEYS` 可配置为 `service-a:<至少32字符密钥>,service-b:<至少32字符密钥>`。配置后，上传和接收验证必须携带 `X-Client-ID` 与 `X-Client-Key`；未配置时保持兼容模式，所有任务归入 `legacy`。来源 IP 默认每分钟 60 次上传尝试；每个调用方默认最多保有 10000 个可能存在的对象、总计 1 TiB。Dashboard 会显示最大调用方占用及 80% 容量告警。

`STORAGE_ENDPOINT_ALLOWLIST` 使用逗号配置允许的主机、域名后缀或 CIDR；默认只允许 `.zos.ctyun.cn`。通用 S3 服务必须显式加入其域名或网段。loopback、link-local 和 metadata 地址始终拒绝，HTTP Endpoint 仅可在开发环境显式开启。

服务未配置对象存储时仍可访问 Dashboard 和设置页；`/readyz` 返回 `STORAGE_NOT_CONFIGURED`，未指定预设的上传接口返回 `STORAGE_DEFAULT_NOT_CONFIGURED`。在设置页选择 Provider，填写 S3 API Endpoint、Bucket、公网访问根地址及 AK/SK，先测试连接，再保存激活。

启动恢复默认只处理最多 25 项，并以 4 路并发和 5 秒初始预算执行；剩余 backlog 由后台轮次继续处理。`/readyz` 暴露待恢复上传/删除数量、最旧任务年龄、最近恢复成功时间和事件日志持久化状态。恢复使用独立的短 Provider timeout，`DASHBOARD_ENABLED=false` 时 Dashboard 页面、静态资源和 Dashboard API 返回 404。未生效的 `REQUEST_TIMEOUT_SECONDS` 已移除。

## 数据库升级

服务启动时使用 `PRAGMA user_version` 检测数据库版本：

- 空数据库直接创建 schema v5。
- schema v1 至 v4 依次事务升级到 v5。
- 升级前使用 SQLite Online Backup 在数据库旁创建 `zos-upload.db.pre-v5-<timestamp>`，包括 WAL 中已提交数据。
- 升级后执行 `integrity_check`、外键、默认预设、active revision 和引用完整性检查；失败时停止启动。

schema v5 保留原 task ID、storage config ID 和 revision，并把历史任务归入 `client_id=legacy`。历史成功任务继续标记为 `legacy_unverified`；恢复确认远端对象存在但没有删除 token hash 时标记为 `present_unclaimed` 并在 Dashboard 告警。不要让旧镜像直接打开已经升级的数据库；需要回滚旧镜像时，必须同时恢复升级前备份。

## 调用上传接口

只验证局域网接收链路、不上传 ZOS：

```bash
curl -fsS -F 'file=@./example.pdf' \
  -H 'X-Client-ID: service-a' \
  -H 'X-Client-Key: <client-key>' \
  http://<内网IP>:8000/v1/uploads/validate
```

同一功能也可以在 Dashboard 的“局域网文件接收测试”区域操作。“真实上传到 ZOS”开关默认关闭，此时只执行 multipart、大小限制和临时文件读写；开启后可以选择任一已启用预设，调用正式上传接口、创建任务并返回公网链接。

真实上传：

```bash
curl -fsS \
  -H 'X-Client-ID: service-a' \
  -H 'X-Client-Key: <client-key>' \
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
  "delete_capability_available": true,
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

测试覆盖严格删除、token 隔离、VersionId、元数据变化、并发删除、未知结果、重启恢复、永久审计和数据库失败，以及管理员/调用方认证、来源限流、配额、作用域幂等、多预设路由、SQLite v1/v2/v3/v4→v5 迁移与回滚、Provider 有界缓存和 Dashboard。

## 部署到局域网服务器

服务器需要安装 Docker 和 Compose，并允许部署用户直接使用 Docker。复制本地部署配置：

```bash
cp .deploy.env.example .deploy.env
```

填写目标、远程目录、健康/就绪 URL 和 SSH 私钥绝对路径后执行：

```bash
./deploy.sh
```

脚本只部署已提交代码。远程 `flock` 防止并发发布；已有服务先按
`DEPLOY_DRAIN_SECONDS` 优雅停服，再生成并校验 SQLite 快照。新镜像先以不映射端口的候选
容器检查 `/healthz` 和 `/readyz`，通过后重建正式服务并检查局域网入口。任一步失败都会
恢复旧镜像和发布前数据库，同 schema 也一样；首次发布失败会清理不健康服务。

`deploy-backups/` 默认分别保留最近 10 份普通发布快照、3 份跨 schema 快照，并受 1 GiB
总容量限制；最新快照和最新跨 schema 快照不会因容量限制被删除。可在 `.deploy.env` 中
调整 `DEPLOY_BACKUP_KEEP_RELEASES`、`DEPLOY_BACKUP_KEEP_MIGRATIONS` 和
`DEPLOY_BACKUP_MAX_BYTES`。

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

2026-07-31 已在真实 ZOS 上通过：1 MiB 单任务、4 个并发 20 MiB multipart、190 MiB
近上限文件和显式 `default` 预设；每个对象均通过公网 HEAD，随后严格删除。

## 私有异地备份

备份不经过普通文件上传 API。它使用 SQLite Online Backup 创建一致性快照，把数据库和
`SETTINGS_ENCRYPTION_KEY` 放入同一归档，再使用独立备份密码加密并以 `private` ACL
上传到专用 ZOS Bucket。

服务器首次配置：

```bash
cp .backup.env.example .backup.env
chmod 600 .backup.env
```

在 `.backup.env` 中填写专用 AK/SK 和至少 32 字符的 `BACKUP_PASSPHRASE`。密码必须
离线保存，不能只存在被备份的服务器或同一个 Bucket。

手动创建备份并验证下载、解密、摘要和 SQLite 完整性：

```bash
./scripts/zos-backup.sh create
./scripts/zos-backup.sh verify <create 返回的 object_key>
```

`BACKUP_MAX_DATABASE_BYTES` 和 `BACKUP_MAX_BLOB_BYTES` 会在全量内存加密或解密前执行大小
与可用内存预检。定时任务使用 `create-verify`，每次上传后立即重新下载并完成受控校验。

恢复演练会导出数据库和密钥，但不会覆盖运行中的数据库：

```bash
./scripts/zos-backup.sh restore <object_key> ./restore-drill
```

`verify/restore` 不依赖正在运行的上传服务。在新 Linux 主机准备 Docker、此仓库和权限为
`600` 的 `.backup.env` 后，可以固定使用已验证的工具镜像：

```bash
BACKUP_IMAGE=zos-upload-service:<已验证标签或摘要> \
  ./scripts/zos-backup.sh restore <object_key> ./restore-drill
```

未提供 `BACKUP_IMAGE` 且没有运行容器时，脚本会从当前 checkout 构建本地 runtime 工具
镜像。生产恢复优先使用已记录摘要的镜像，避免源码版本与备份格式不匹配。

输出目录必须不存在；目录权限为 `700`，数据库和密钥文件权限为 `600`。验证首次手动
备份后，安装每天 02:17 执行的用户 crontab：

```bash
./scripts/install-backup-cron.sh
```

备份脚本不删除对象。版本保留、30 天合规保留和过期清理由私有 Bucket 策略负责。备份
Bucket 必须保持私有：关闭匿名读写，若平台支持则启用 Block Public Access，并让 Bucket
Policy 只授权备份账号所需的 PutObject、HeadObject、GetObject 和 ListBucket。首次配置及
策略变更后，从无凭证网络对一个真实备份对象执行 `curl -I <对象URL>`，结果必须是
`403` 或不泄露对象存在性的 `404`，不得是 `200`；随后再运行凭证化 `verify`。匿名验收
失败时不要继续安装定时任务。

## 运维边界

- 上传数据面仍只允许绑定可信局域网 IP；Dashboard、设置、日志、OpenAPI 和完整任务查询必须提供管理员凭证。不要把端口暴露到公网。
- 设置请求会携带 AK/SK，正式环境应放在内网 HTTPS 反向代理后，且禁止代理日志记录请求体。
- `zos-database` 保存 SQLite 和加密后的配置；`zos-temporary` 只保存请求期临时文件。异地备份必须同时包含数据库和 `SETTINGS_ENCRYPTION_KEY`。
- 服务为上传对象请求 `public-read` canned ACL；Bucket Policy、账号权限、生命周期、未完成 multipart 清理和对象删除仍在 ZOS 侧管理。
- 当前标准 S3 Client 覆盖 HeadBucket、上传、multipart 和 HeadObject。ZOS 扩展 Bucket 指标依赖兼容 SDK；关闭指标不会影响核心上传与本地 Dashboard。
