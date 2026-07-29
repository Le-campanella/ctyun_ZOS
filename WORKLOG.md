# MVP 工作记录

## 当前状态

进行中。实现以 [PLAN.md](PLAN.md) v4 和 [API.md](API.md) v1 为准。

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
- 2026-07-29：当前 20 个自动测试全部通过。

## 进行中

- 补齐部署说明、容器健康检查和剩余故障注入测试。

## 尚未完成

- 容器部署文件、完整自动测试与故障注入测试。
- 真实 ZOS 集成测试；需要可用的 Endpoint、Bucket、AK 和 SK。

## 下一步

增加 `.env.example`、Compose、中文运行说明，并执行生产镜像 smoke test。
