# 局域网 ZOS 文件上传服务

这是一个单容器、无登录的内部文件网关：接收局域网服务提交的单个文件，临时落盘后上传到当前激活的天翼云 ZOS Bucket，记录 SQLite 任务台账，并返回对象 Key 和公网 URL。请求结束后不保留文件本体。

完整行为见 [PLAN.md](PLAN.md)，调用方契约见 [API.md](API.md)，实施记录见 [WORKLOG.md](WORKLOG.md)。

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

服务未配置 ZOS 时仍可访问 Dashboard 和设置页，`/readyz` 与上传接口会返回 `STORAGE_NOT_CONFIGURED`。在设置页填写 SDK Endpoint、Bucket、公网访问根地址及 AK/SK，先测试连接，再保存激活。

## 调用上传接口

```bash
curl -fsS \
  -H 'X-Request-ID: caller-service-001' \
  -H 'Idempotency-Key: business-job-001' \
  -F 'file=@./example.pdf' \
  http://<内网IP>:8000/v1/uploads
```

成功响应：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/29/550e8400-e29b-41d4-a716-446655440000.pdf"
}
```

单文件最大 200 MiB，默认接受所有类型。公网 URL 只在上传成功时返回；服务不代理下载，也不管理文件生命周期或匿名访问策略。

## 测试

自动测试使用与生产一致的 Python 3.11 镜像，不需要本机 Python 环境：

```bash
docker build --target test -t zos-upload-service:test .
docker run --rm zos-upload-service:test
```

测试覆盖上传成功/失败/待确认、空文件与超限文件、伪造请求长度、幂等、并发上限、恢复、SQLite WAL 与 revision、凭证加密和清洗、统计、日志、Dashboard 与设置接口。

真实 ZOS 验收需要有效的 Endpoint、Bucket、AK/SK 和可公网访问的 Bucket 域名，至少检查：

1. TXT、PDF、图片、超过 16 MiB 及接近 200 MiB 文件。
2. ZOS 对象 Key、Content-Type、大小与任务记录一致。
3. 从公网环境实际访问返回 URL。
4. 错误 Endpoint、凭证和 Bucket 能返回明确错误。
5. 重启后未决任务可通过 HeadObject 恢复。

## 运维边界

- 服务没有应用层认证，只允许绑定可信局域网 IP；不要把端口暴露到公网。
- 设置请求会携带 AK/SK，正式环境应放在内网 HTTPS 反向代理后，且禁止代理日志记录请求体。
- `zos-database` 保存 SQLite 和加密后的配置；`zos-temporary` 只保存请求期临时文件。数据库卷需要定期快照或在线备份。
- Bucket 的公网读取、生命周期、未完成 multipart 清理、对象删除与权限策略均在 ZOS 侧管理。
- 当前标准 S3 Client 覆盖 HeadBucket、上传、multipart 和 HeadObject。ZOS 扩展 Bucket 指标依赖兼容 SDK；关闭指标不会影响核心上传与本地 Dashboard。
