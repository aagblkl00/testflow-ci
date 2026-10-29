# TestFlow CI

面向测试团队的 **Web 自动化持续回归与质量协同平台**：把用例、执行、证据、失败分析与 CI 门禁串成一条可持续演进的测试闭环。只服务测试人员，不替代 Jenkins / GitLab / Jira / 禅道——GitLab CI 在这里只是触发来源。

## 核心能力

- **测试资产**：项目 / 环境 / 用例 / 流程 / 回归计划 / 套件，支持 Excel、CSV、JSON 导入
- **执行中心**：真实浏览器（Playwright）执行，支持真实停止、重试与 Flaky 判定、失败用例重跑
- **证据链**：逐步截图 / DOM / Console / Network / Trace / Video / HAR，全部落盘可审计
- **失败中心**：确定性指纹聚合、规则分类、失败分组，告警状态机（确认 → 处理 → 重新验证 → 解决）
- **AI 失败分析（可选）**：接入任意 OpenAI 兼容接口；结论必须带不确定性，需人工确认后才生效
- **企业微信通知（可选）**：一条告警一条消息；发送失败、重试、超时均不影响测试结果
- **CI 集成**：CLI（退出码 0/1/2）、CI 触发 API、GitLab Pipeline 示例；幂等键防重复触发
- **安全**：证据 URL 短时签名、保留策略、PBKDF2 密码哈希、CI Token 未配置即关闭接口

## 技术栈

FastAPI · SQLAlchemy（SQLite / PostgreSQL）· Playwright · Celery / Redis（可选）· React 18 · Ant Design · Vite · TypeScript

## 快速开始

### Docker Compose（推荐）

```bash
cp .env.example .env   # 按需修改配置
docker compose up --build
```

启动后：

| 入口 | 地址 | 默认账号 |
|---|---|---|
| Web UI | http://127.0.0.1:5173 | `admin / admin123` |
| API 文档 | http://127.0.0.1:8000/docs | — |
| 被测 Demo 站 | http://127.0.0.1:8100/login | `demo / demo123` |

### 本地开发

Windows 可直接双击 `start.bat`（关闭窗口前用 `stop.bat` 停止服务）；或手动起三个进程：

```powershell
# 终端一：被测 Demo 站（8100）
.\.venv\Scripts\python.exe -m uvicorn demo_app.main:app --host 127.0.0.1 --port 8100

# 终端二：后端 API（8000）
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# 终端三：前端（5173）
cd frontend
npm install
npm run dev
```

> 首次执行真实浏览器用例前，安装 Chromium：`.\.venv\Scripts\python.exe -m playwright install chromium`

> **生产部署必读**：默认账号密码、`TESTFLOW_SECRET`、`TESTFLOW_CI_TOKEN` 均为开发默认值，上线前务必在 `.env` 中改为随机长串。

## CLI 与 CI 接入

```bash
pip install -e .
testflow run --plan smoke --env test --project 商城系统
testflow status --run-id <RUN_ID>
```

退出码语义：`0` 全通过 · `1` 有用例未通过（含被测环境连不上）· `2` 平台自身未能完成可靠执行。GitLab Pipeline 的完整示例见 `.gitlab-ci.yml`。

## 配置

复制 `.env.example` 为 `.env` 后按需修改，关键项：

- `DATABASE_URL`：默认 SQLite；PostgreSQL 见 docker-compose 注释
- `EXECUTOR_MODE`：`inline`（默认，免 Redis）或 `celery`（需 Redis）
- `TESTFLOW_SECRET`：令牌签名密钥
- `BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD`：初始管理员
- `AI_*`：留空即整体关闭 AI 分析，不影响测试执行
- `WECOM_WEBHOOK_URL`：留空即不发企业微信通知
- `TESTFLOW_CI_TOKEN`：留空即关闭 CI 触发接口（返回 503，不会成为开放入口）

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest tests -q
```

测试自带隔离：`tests/conftest.py` 强制使用临时数据库与临时存储目录，不会读写本机真实数据。

## 目录结构

```text
backend/     FastAPI 服务（API / 执行引擎 / 失败中心 / AI / 告警 / CI）
frontend/    React + Ant Design 界面
demo_app/    内置被测 Demo 站（含故障注入开关）
cli/         testflow 命令行客户端
scripts/     种子数据与验收脚本
docs/        设计与各里程碑交付说明
tests/       pytest 测试（自带隔离）
```

## 文档

`docs/` 目录保存了各里程碑的设计与交付说明（`02_V1改造计划.md`、`03_M1交付说明.md` …），需要了解设计决策与验收口径时可查阅。

## 开源协议

[MIT License](LICENSE)