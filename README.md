# TestFlow CI

面向测试团队的 **Web 自动化持续回归与质量协同平台**。

只服务测试人员和测试团队。不是 Jenkins / GitLab / Jira / 禅道的替代品，GitLab CI 在这里只是触发来源。

---

## 当前进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M1** | 工程底座 + 测试资产 + Playwright 真执行 + 证据 + 最小 UI | ✅ 已完成 |
| **M2** | 执行中心：TestSuite / 真实 Stop / Retry 与 Flaky / 失败用例重跑 / Video 与 HAR | ✅ 已完成 |
| **M3** | 失败中心：确定性指纹 / 规则分类 / 失败聚合 / Flaky 统计 / 重试策略 | ✅ 已完成 |
| **M4** | CI 接入：CLI / CI API / GitLab 触发 / 幂等 / Exit Code 0-1-2 | ✅ 已完成 |
| **M5** | AI 失败分析 + 告警引擎 + 企业微信通知 + 人工确认 / 重新验证 | ✅ 已完成 |
| M6 | Dashboard + 安全加固（生产保护 / 证据清理） | 待开始 |
| M7 | 完整 Demo + 全量验收 | 待开始 |

### 已验证结果

- **M1**：3 用例 25 步骤全 PASSED，产出 91 个证据文件；`selftest_m1.py` 93/93
- **M2**：`selftest_m2.py` **98/98**；真实 Stop 延迟 0.6s（12s 等待中途中断）；FAIL→PASS 判定 FLAKY；
  失败用例重跑原 Run 零改动；Video 356 KB / HAR 13 KB 真实生成
- **M3**：`selftest_m3.py` **160/160**；一次回归 15 个失败用例聚合为 3 个 FailureGroup
- **M4**：`selftest_m4.py` **76/76**；CLI 真进程退出码 0/1/2 全部验证；10 个并发相同请求只产生 1 个 Run
- **M5**：`selftest_m5.py` **165/165**（含真实浏览器 UI 验收）；10 个用例同一个根因 → 1 条告警；AI 分析幂等 + 人工确认 + 重新验证闭环
- 后端测试 **169 passed**；M1/M2/M3/M4 全部验收项回归通过

详细交付说明见 `docs/`：
`01_现有资产盘点报告.md`、`02_V1改造计划.md`、`03_M1交付说明.md`、`04_M2交付说明.md`、
`05_M3交付说明.md`、`06_M4交付说明.md`、`07_M5影响分析.md`、`08_M5交付说明.md`

---

## M5 新增能力速览

| 能力 | 怎么用 | 说明 |
|---|---|---|
| **AI 失败分析** | 失败中心 → 分组详情 → 「开始分析」 | 默认**只有 UNKNOWN 进 AI**；确定性分类已经够用的不送模型去重判 |
| **结构化结论** | 详情里看 Summary / 可能原因 / 证据 / 置信度 / 不确定的部分 / 建议 | 必须带 `uncertainty` —— 不肯说「我不确定」的结论是有害的 |
| **人工确认** | 详情里「确认 / 修改 / 忽略」 | 确认 ≠ 问题已解决，不会关闭 FailureGroup |
| **告警** | 详情「告警」区块 | 按 FailureGroup 聚合：10 个用例同一个根因只产生 **1 条**告警 |
| **告警状态机** | 确认 → 开始处理 → 进入重新验证 → 标记已解决 | 白名单式；**不允许 OPEN 直接解决**，引擎也从不自动置 RESOLVED |
| **重新验证** | 告警里点「进入重新验证」 | 复用 M2 的失败用例重跑：只跑原失败用例，新 Run 记录 `source_run_id` |
| **企业微信通知** | 计划里打开「发送企业微信通知」 | 一条告警一条消息，含 AI 分析与证据摘要；失败最多重试 3 次，异步发送 |
| **通知隔离** | 无需操作 | AI 挂 / 企微挂 / 超时，**都不影响测试结果**，只写各自的记录表 |

---

## M4 新增能力速览

| 能力 | 怎么用 | 说明 |
|---|---|---|
| **CLI** | `testflow run --plan smoke --env test` | 需要 `pip install -e .`；也可 `python -m cli.testflow ...`。只走 HTTP，不碰数据库 |
| **退出码语义** | CI 直接看退出码 | **0** 全通过 · **1** 有未通过用例（含被测环境连不上）· **2** 平台自身没能完成 |
| **查结果** | `testflow status --run-id <ID>` | 状态 + 用例汇总 + 失败分组分类 + Pipeline 元数据，不输出 step 明细 |
| **JSON 输出** | 加 `--json` | JSON 只走 stdout，日志全走 stderr，可以直接 `\| jq` |
| **CI 触发 API** | `POST /api/ci/runs` | 需要 `X-TestFlow-Token`；幂等键 `(project, plan, pipeline_id)` |
| **GitLab 触发** | `POST /api/ci/gitlab/trigger` | 支持原生 Pipeline Webhook 与 CI Job 两种形态；示例见根目录 `.gitlab-ci.yml` |
| **Pipeline 关联** | 执行详情页 | 触发来源 / Commit / 分支 / Pipeline（可点击直达 GitLab） |
| **幂等** | 无需操作 | 同一个 Pipeline + 同一个计划只会有 1 个 Run，数据库唯一索引兜底，并发也挡得住 |

---

## M3 新增能力速览

| 能力 | 怎么用 | 说明 |
|---|---|---|
| **失败中心** | 左侧「失败中心」 | 一次回归几十个失败，按确定性指纹聚合成几个可调查的问题，而不是几百条失败 |
| **确定性指纹** | 失败中心详情抽屉「Failure Fingerprint」 | 纯算法（非 AI）归一化数字 ID / UUID / 时间戳 / token，同一问题永远同一指纹 |
| **规则分类** | 详情抽屉「分类」区 | 六类：产品/环境/自动化/数据/Flaky/未知，每条结论带 Rule ID 与理由，证据不足就标 UNKNOWN |
| **Flaky 统计** | 工作台「Flaky 发生率」 | 显式给出分母与计算定义；Flaky 失败单独成类，不伪装成 PASS、不混进产品问题 |
| **重试策略** | 回归计划「失败自动重试」开关 | 显式重试次数优先，其次按失败类型走策略；配置可覆盖（`config/retry_policy.json`） |
| **分组钻取** | 失败中心 → 出现历史 → 点执行 ID | 直接跳到执行详情并自动展开那条用例，证据（截图/Trace/Video/HAR）复用 M2 系统 |

---

## M2 新增能力速览

| 能力 | 怎么用 | 说明 |
|---|---|---|
| **测试套件** | 左侧「测试套件」→ 新建 → 「成员」抽屉里排序/启停 | 回归计划优先引用套件；旧式直接选用例仍可用 |
| **停止执行** | 执行中心或执行详情页点「停止」 | 真通知执行器，关闭浏览器；正在执行的 Case 记 CANCELLED，未开始的记 SKIPPED |
| **失败重试** | 回归计划里设「失败重试次数」 | 每次重试都是完整重新执行，证据独立存放；FAIL→PASS 记 FLAKY，不伪装成 PASS |
| **重跑失败用例** | 执行中心或详情页点「重跑失败用例」 | 新建一个 Run 只跑失败用例，原 Run 完全不动，新 Run 记录来源 |
| **录屏与 HAR** | 执行详情页点「播放录屏」/「下载 HAR」 | 每个 attempt 独立一份；`.env` 里 `PLAYWRIGHT_RECORD_VIDEO` / `PLAYWRIGHT_RECORD_HAR` 可关 |
| **五次尝试可切换** | 执行详情里用例展开后切 Segmented | 每次尝试的步骤与证据独立可查 |

---

## 数据库迁移（M2 引入，M3/M4 沿用）

M1 只有 `create_all()`，无法给已有表加列。M2 引入带版本表的轻量迁移器：

```bash
.venv\Scripts\python.exe -m backend.app.migrations           # 执行迁移
.venv\Scripts\python.exe -m backend.app.migrations --status  # 查看版本状态
```

- 迁移记录在 `schema_migrations` 表；每个迁移幂等，可反复执行
- SQLite 与 PostgreSQL 通用
- 服务启动时会自动执行（`init_db()` 里 `create_all` + `run_migrations`）
- 对真实 M1 数据库执行后逐表比对：11 张表行数零变化

---

## 一、环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+ | 已在 3.13 上验证 |
| Node.js | 20+ | 已在 22.12 上验证 |
| Playwright Chromium | 必须 | 详见下方「关于浏览器」 |
| Docker | **可选** | 只有切 PostgreSQL + Redis 时才需要 |
| PostgreSQL / Redis | **可选** | M1 不需要，inline 模式跑在 SQLite 上 |

### 关于浏览器（重要，可省一次下载）

如果本机已经有 Playwright 浏览器缓存，直接在 `.env` 里指向它即可，无需重新下载：

```env
PLAYWRIGHT_BROWSERS_PATH=<本机Chromium缓存目录>
```

并且 Playwright 版本要与缓存中的 Chromium 版本匹配。本机缓存为 `chromium-1187`，
对应 `playwright==1.55.0`（`backend/requirements.txt` 已注释说明）。

若使用其他版本，执行一次：

```bash
.venv/Scripts/python.exe -m playwright install chromium
```

---

## 二、启动方式

### 1. 准备配置

```bash
cd <项目根目录>
copy .env.example .env
```

`.env` 关键项：

```env
DATABASE_URL=sqlite:///<项目根目录>/testflow.db
EXECUTOR_MODE=inline
STORAGE_DIR=<项目根目录>/storage
PLAYWRIGHT_BROWSERS_PATH=<本机Chromium缓存目录>
PLAYWRIGHT_HEADLESS=true
TESTFLOW_SECRET=dev-only-secret-change-me
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=admin123
```

### 2. 装依赖

```bash
cd <项目根目录>

# 后端
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 前端
cd frontend
npm install
cd ..
```

### 3. 起三个进程（各自一个终端）

```bash
# ① 被测目标站 Demo（端口 8100）
.venv\Scripts\python.exe -m uvicorn demo_app.main:app --host 127.0.0.1 --port 8100

# ② TestFlow 后端 API（端口 8000）
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# ③ TestFlow 前端（端口 5173）
cd frontend && npm run dev
```

打开 **http://127.0.0.1:5173** ，用 `admin / admin123` 登录。

- TestFlow API 文档：http://127.0.0.1:8000/docs
- Demo 被测站：http://127.0.0.1:8100/login （账号 `demo / demo123`）

---

## 三、M1 验收命令

### ① 闭环验收（真实浏览器执行 happy path）

```bash
cd <项目根目录>
.venv\Scripts\python.exe scripts\seed_demo.py
```

会自动创建项目/环境/用例/流程/绑定/计划，并真实执行。脚本打印每一步动作、用例级与步骤级结果、
证据文件清单，最后给出结论：

```
[OK] 闭环真实跑通：浏览器已启动，步骤已执行，结果与证据已落库
```

### ② 全维度自测（93 项断言）

```bash
.venv\Scripts\python.exe scripts\selftest_m1.py
```

除 happy path 外，还覆盖：从零建项目、失败路径（断言失败/变量未定义/元素不存在）、
边界与门禁（缺环境/白名单为空/PROD/未知动作）、鉴权与脱敏、故障注入、证据逐文件核查、
真实浏览器逐页自测。当前 **93/93 通过**。

### ③ M2 执行中心验收（98 项断言）

```bash
.venv\Scripts\python.exe scripts\selftest_m2.py
```

覆盖 M2-01 ~ M2-09：套件闭环、真实 Stop、Retry 出 FLAKY、失败用例重跑、
Video/HAR 真实性、五层结构可查、M1 兼容性、真实浏览器逐页验收。当前 **98/98 通过**。

### ④ M3 失败中心验收（160 项断言）

```bash
.venv\Scripts\python.exe scripts\selftest_m3.py
```

覆盖 M3-01 ~ M3-14：指纹归一化、同问题聚合、不同 API 分组、六类规则分类、
Flaky、重试策略与显式兼容、跨 Run 聚合、Failure Center 钻取、幂等、脱敏，
以及真实浏览器逐页验收。当前 **160/160 通过**。

### ⑤ M4 CI 接入验收（76 项断言）

```bash
.venv\Scripts\python.exe scripts\selftest_m4.py
```

覆盖 M4-01 ~ M4-12：CLI 真实触发、退出码 0/1/2、Environment Error 与平台错误的边界、
status 与 JSON 输出、GitLab 触发（两种载荷形态）、Pipeline 元数据落库与展示、
幂等、10 并发幂等、失败中心链路。CLI 以**子进程**调用，退出码是进程真实退出码。
当前 **76/76 通过**。

### ⑥ M5 AI 分析 + 告警 + 通知验收（165 项断言）

```bash
.venv\Scripts\python.exe scripts\selftest_m5.py
```

覆盖 M5-01 ~ M5-20：Provider 与真实 HTTP 服务的交互（本地 stub）、UNKNOWN 真实产出与
AI 分析、非法 JSON 隔离、分析幂等与 revision、人工确认/修改/忽略、告警聚合与去重、
企业微信发送/失败隔离/重试、告警状态机、重新验证闭环、脱敏、真实浏览器 UI。

**它会自己起一个专用 API 实例（端口 8200）**指向本地 stub（8300），
不碰你在用的 8000 端口服务。当前 **164/164 通过**。

> 关于 AI 与企业微信的真实性：本机没有真实 AI Key 与企微 Webhook，
> 所以「真实 AI / 真实企业微信」是 **NOT VERIFIED**；脚本用真实 HTTP stub
> 验证的是「换成符合协议的真实服务，我们的代码对不对」。详见 `docs/08_M5交付说明.md`。

### ⑦ 界面验收（真实浏览器点页面）

```bash
.venv\Scripts\python.exe scripts\verify_ui.py
```

### ⑧ 后端测试

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

前置条件：`demo_app`（8100）与 `backend`（8000）两个进程已启动。

---

## 四、目录结构

```
<项目根目录>\
├── backend\
│   ├── app\
│   │   ├── api\            # 路由：auth / projects / cases / flows / plans / runs
│   │   ├── models\         # SQLAlchemy 模型（M1 共 12 张表）
│   │   ├── schemas\        # Pydantic 出入参
│   │   ├── execution\      # ★ 执行引擎（移植自北极星 Lodestar）
│   │   │   ├── action_spec.py          # ActionSpec + 变量系统
│   │   │   ├── security.py             # 4 道安全门禁
│   │   │   ├── playwright_executor.py  # Playwright 执行器 + 证据采集
│   │   │   └── service.py              # 执行编排
│   │   ├── workers\        # inline / celery 双模式分发
│   │   ├── auth\           # 登录 + 角色
│   │   └── utils\redact.py # 敏感信息脱敏
│   ├── requirements.txt
│   └── app\main.py
├── frontend\               # React + TypeScript + Vite + Ant Design
│   └── src\{pages,components,hooks,api,types}
├── demo_app\               # 被测目标站（含故障注入开关，M3 使用）
├── scripts\seed_demo.py    # M1 闭环验收脚本
├── storage\                # 执行证据（截图 / Trace / 日志 / DOM）
├── docs\                   # 盘点报告与阶段计划
└── docker-compose.yml      # 可选：PostgreSQL + Redis + API + Worker
```

### 证据目录结构

M2 起每次尝试都有独立目录，重试的两次证据不会互相覆盖：

```
storage\projects\{project_id}\runs\{run_id}\
├── run.log
└── cases\{case_result_id}\
    └── attempts\{n}\            # M2：第 n 次尝试（重试时 n=1,2,...）
        ├── screenshots\         # 每步 before / after / failed，1440x900 PNG
        ├── trace\               # trace.zip（含 DOM 快照、网络、截图）
        ├── video\               # recording.webm（M2）
        ├── network\             # network.har（M2）
        ├── logs\                # 每步 JSON 日志 + console.log + network.log
        └── dom\                 # 失败步骤的 HTML 快照
```

---

## 五、切换执行模式

### inline（默认，无需 Redis）

`.env`：`EXECUTOR_MODE=inline`

浏览器在后台线程中执行，Web 请求不阻塞。适合本机开发与单机部署。

### celery（需要 Redis）

```bash
# 1. 起 Redis（或用 docker compose）
# 2. 改 .env
EXECUTOR_MODE=celery
CELERY_BROKER_URL=redis://localhost:6379/0

# 3. 启动 Worker（Windows 必须用 solo pool）
.venv\Scripts\python.exe -m celery -A backend.app.workers.celery_app.celery_app worker --pool=solo -l info
```

两种模式调用的是同一个 `execute_run_sync`，**CaseResult / StepResult / Artifact 的结果语义完全一致**。

### PostgreSQL + Redis（docker compose）

```bash
docker compose up -d
# 然后把 .env 的 DATABASE_URL 改成：
# DATABASE_URL=postgresql+psycopg://testflow:testflow@localhost:5432/testflow
# 并安装驱动：.venv\Scripts\python.exe -m pip install "psycopg[binary]"
```

---

## 六、安全机制（M1 已生效）

执行前会跑四道门禁，任一 block 则整批直接报 ERROR，不会把浏览器放出去：

1. **环境身份**：`environment_type=PROD` 或地址/名称含 `prod / production / 生产 / 正式 / online / live / prd / release` → 拒绝
2. **域名白名单**：请求目标不在 `allowed_hosts` 内 → 拒绝（含 `*.example.com` 通配）
3. **真实支付网关**：`alipay.com / pay.weixin.qq.com / paypal.com / stripe.com / unionpay.com` → 始终拒绝
4. **只读环境**：`read_only=true` 时禁止 `risk` 为 `write / destructive` 的步骤

脱敏规则：`password / passwd / pwd / token / secret / api_key / authorization / cookie / session / credential` 等
在接口响应、步骤日志、console/network 日志中一律显示为 `********`，但**数据库保留原值**（执行时需要真实值）。

---

## 七、故障注入（M3 使用，M1 已预留）

Demo 站内置开关，用来人为制造失败：

```bash
curl -X POST http://127.0.0.1:8100/__faults -H "Content-Type: application/json" -d "{\"orders_500\": true}"
curl http://127.0.0.1:8100/__faults
```

| 开关 | 制造的场景 |
|---|---|
| `login_500` | 接口 500 |
| `search_500` | 搜索接口 500 |
| `orders_500` | 订单接口 500（多用例同一根因，用于验证失败聚合） |
| `orders_slow_ms` | 接口延迟，制造网络超时 |
| `missing_element` | 首页问候语不渲染，制造元素找不到 |
| `flaky_once` | 首次失败、第二次成功，制造 Flaky |
| `js_error` | 首页脚本抛未捕获异常（M3 验证「前端崩溃导致元素找不到」的归因） |
| `missing_data` | 下单前置测试数据缺失（M3 验证 DATA_ERROR） |

---

## 八、CI 接入（M4）

### 1. 安装 CLI

```bash
.venv\Scripts\python.exe -m pip install -e .
```

装上后有两个等价入口，任选：`testflow ...` 或 `python -m cli.testflow ...`。

### 2. 配置

| 环境变量 | 说明 |
|---|---|
| `TESTFLOW_CI_TOKEN` | 服务端 `.env` 里的同名值。**服务端留空 = CI 接口整体关闭（503）** |
| `TESTFLOW_URL` | 平台地址，默认 `http://127.0.0.1:8000` |

CLI **没有 `--token` 参数**：命令行参数会留在 `ps`、shell history、CI job log 里。
`.env` 已被 gitignore，Token 不会进版本库。

### 3. 用

```bash
testflow run --plan smoke --env test
testflow run --plan smoke --env test --json          # JSON 只走 stdout
testflow status --run-id <RUN_ID>
```

`--project` 可以省略（按计划名全局解析，同名多个会报错并列出候选）。

### 4. 退出码

| 码 | 含义 |
|---|---|
| **0** | TestFlowCI 完成执行，全部用例通过 |
| **1** | TestFlowCI 完成执行，但有用例未通过 —— **包括被测环境连不上** |
| **2** | TestFlowCI 自身没能完成可靠执行（平台侧问题） |

> 关键边界：**被测环境挂了 ≠ 测试平台挂了**。目标环境连接被拒时，
> TestFlowCI 是成功跑完测试并如实报告了环境问题，所以是 1 不是 2。

### 5. GitLab

示例流水线见根目录 **`.gitlab-ci.yml`**，两种形态都给了：CI Job 直接调 CLI（推荐）、
原生 Pipeline Webhook。要点：

- Webhook 地址：`POST /api/ci/gitlab/trigger`
- Secret token / 请求头：与服务端 `TESTFLOW_CI_TOKEN` 同一个值
- GitLab 的 Pipeline 载荷不知道「跑哪个计划」，用 pipeline 变量带出来：
  `TESTFLOW_PROJECT` / `TESTFLOW_PLAN` / `TESTFLOW_ENV`
- **幂等**：同一个 Pipeline + 同一个计划只会有一个 Run，Webhook 重试与 GitLab 重跑都不会重复建

---

## 九、开发纪律

- 每完成一个里程碑都要：跑测试 → 修问题 → 跑真实 Demo
- 只写代码不跑、只写接口不跑、只做 Mock、只做页面，都不算完成
- 优先顺序：真实执行能力 > 执行结果 > 失败证据 > 失败聚合 > CI > 企业微信 > AI > UI 美化
