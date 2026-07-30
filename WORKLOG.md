# MVP 工作记录

## 当前状态

第一版 MVP 已完成本地构建与自动验收。实现以 [PLAN.md](PLAN.md) v4 和 [API.md](API.md) v1 为准。

## 已完成

- 2026-07-29：确认用户整理后的 v4 计划和 v1 API 是当前权威规格。
- 2026-07-29：完整阅读计划、接口文档和 ZOS SDK 包结构。
- 2026-07-29：确定使用 Python 3.11 Docker 环境构建和测试，避免宿主机 Python 3.14 与无 pip 环境造成偏差。
- 2026-07-29：规范当前规格文件名为 `PLAN.md` 和 `API.md`，保留旧版到 `legacy/`。
- 2026-07-29：锁定 FastAPI、Uvicorn、boto3、cryptography、Jinja2 和测试依赖。
- 2026-07-29：完成部署配置解析、Fernet 凭证加密、三张 SQLite 表、WAL 参数和配置 revision 事务。
- 2026-07-29：完成 Provider registry、`ctyun_zos` 配置校验、S3 Client、HeadBucket、上传、HeadObject 和 URL 构造。
- 2026-07-29：完成 JSON 结构化日志、敏感字段清洗和 `NOTIFY` 日志入库。
- 2026-07-29：基础层 10 个自动测试全部通过。
- 2026-07-29：完成 FastAPI 生命周期、请求 ID、统一错误、请求体上限和上传并发守卫。
- 2026-07-29：完成存储 Provider preset、候选连接测试、masked 凭证读取、revision 激活和凭证继承。
- 2026-07-29：完成临时文件、200 MiB 校验、S3 上传、日期 UUID Key、幂等重放和失败持久化。
- 2026-07-29：完成任务列表、任务详情、健康、就绪、概览、流量、日志和可选 Provider 指标 API。
- 2026-07-29：完成启动恢复和周期恢复底座。
- 2026-07-29：完成同源 Dashboard 监控页，展示健康、概览、本地 Chart.js 流量图、近期任务和日志。
- 2026-07-29：完成存储设置页，支持 masked 凭证、连接测试、revision 确认和保存激活。
- 2026-07-29：动态内容使用 DOM `textContent`，页面不加载远程资源且不使用浏览器存储保存凭证。
- 2026-07-29：增加并发容量、伪造请求长度、未知结果、探测过期、跨站设置写入、统计 P95、日志分页与裁剪等边界测试。
- 2026-07-29：优化 31 天流量聚合为单次任务扫描，避免 Dashboard 数据量增长后产生任务数乘时间桶数的开销。
- 2026-07-29：完成非 root 生产镜像、容器健康检查、Compose 持久卷、局域网绑定示例和中文运行说明。
- 2026-07-29：前端 JavaScript 语法、Compose 配置与 Git diff 校验通过。
- 2026-07-29：生产容器 smoke test 通过：`/healthz`、两个 Dashboard 页面正常；未配置状态下 `/readyz` 按约定返回 503。
- 2026-07-29：最终 28 个自动测试全部通过。
- 2026-07-29：增加 `/v1/uploads/validate` 和 Dashboard 局域网接收测试区；复用正式接收限制，但不上传 ZOS、不创建任务。
- 2026-07-29：接收测试区增加默认关闭的真实上传开关；开启后复用正式上传接口并显示其响应。
- 2026-07-29：真实 ZOS 诊断确认 botocore 默认 checksum 导致 `XAmzContentSHA256Mismatch`；Provider 永久改为 `when_required`，上传对象固定使用 `public-read` ACL。
- 2026-07-29：通过正式 `/v1/uploads` 上传 `k3_tech_report.pdf` 验证成功；任务状态为 `succeeded`，匿名公网 HEAD 返回 200，大小 `1795077` 且 Content-Type 为 `application/pdf`。
- 2026-07-30：增加面向 `liyang@192.168.1.150:~/services/ctyun_ZOS` 的 SSH 部署脚本；本地测试、按 Git commit 构建镜像、传输、Compose 重建和健康检查由单条命令完成。
- 2026-07-30：Compose 镜像标签支持 `IMAGE_TAG`，服务器 `.env` 与持久卷不会被部署覆盖。
- 2026-07-30：完成首次局域网服务器部署；版本 `f38067ee0934` 容器健康，Dashboard 返回 200，数据库与临时文件持久卷已创建。

## 进行中

- 无。

## 尚未完成

- 完成接近 200 MiB、multipart、重启恢复等剩余真实 ZOS 压力与故障验收；基础 PDF 上传和公网访问已经通过。
- 在目标局域网部署内网 HTTPS 反向代理、网络访问控制、数据库备份和 Bucket 生命周期规则；这些属于部署环境工作。

## 下一步

按 [README.md](README.md) 在目标主机启动服务，通过设置页激活真实 ZOS 配置，再执行真实对象与公网 URL 验收清单。
