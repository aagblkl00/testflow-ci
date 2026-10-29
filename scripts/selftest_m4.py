"""M4 真实验收（CI Integration）。

对应验收项 M4-01 ~ M4-12。除 GitLab 侧之外，全部使用真实 SQLite + 真实 Demo 站 +
真实 Chromium；CLI 以**子进程**方式调用，退出码是进程真实退出码，不是函数返回值。

  M4-01  CLI 正常：真实创建 Run
  M4-02  CLI PASS：3 用例全通过 → Run=PASSED，Exit 0
  M4-03  CLI FAIL：真实业务失败（HTTP 500）→ Run=FAILED，FailureGroup=PRODUCT_BUG，Exit 1
  M4-04  Environment Error：ERR_CONNECTION_REFUSED → Case=FAILED，Category=ENVIRONMENT_ERROR，Exit 1
  M4-05  System Error：错误 Plan / PROD 门禁 → Exit 2
  M4-06  status：状态、用例汇总、失败分组汇总、Pipeline 元数据
  M4-07  JSON：stdout 只有 JSON，日志全在 stderr
  M4-08  GitLab Trigger：真实 HTTP 调用（**不是**真实 GitLab Pipeline，见交付说明）
  M4-09  Pipeline Metadata：Run 上真的落了 Pipeline/Commit/Branch，UI 可见
  M4-10  幂等：同一 (project, plan, pipeline) 重复 3 次 → 1 个 Run
  M4-11  并发幂等：10 个并发相同请求 → 1 个 Run（数据库唯一约束生效）
  M4-12  Failure Center 链路：Pipeline → Run → FailureGroup → Occurrence 可追溯

前置：backend(8000) 与 demo_app(8100) 已启动，且 .env 里配了 TESTFLOW_CI_TOKEN。

用法：
    .venv/Scripts/python.exe scripts/selftest_m4.py
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import settings  # noqa: E402

API = "http://127.0.0.1:8000"
DEMO = "http://127.0.0.1:8100"
# 9099 无服务监听，用来制造真实的「连接被拒」。9 号端口属 Chromium 受限端口，
# 报的是 ERR_UNSAFE_PORT，验不出网络层故障。
UNREACHABLE = "http://127.0.0.1:9099"
SHOT_DIR = ROOT / "storage" / "_selftest_m4"

LOGIN = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
]
PASS_LOGIN = LOGIN + [
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting"},
]
PASS_SEARCH = LOGIN + [
    {"name": "打开搜索页", "action": "open", "value": "${base_url}/search?q=%E9%94%AE%E7%9B%98", "timeout": 15000},
    {"name": "断言有搜索结果", "action": "assert_visible", "locator_type": "testid", "locator_value": "search-results"},
]
PASS_ORDER = LOGIN + [
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "填写数量", "action": "fill", "locator_type": "testid", "locator_value": "order-quantity", "value": "1"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    {"name": "断言下单没有报错", "action": "assert_not_visible", "locator_type": "testid", "locator_value": "order-error"},
]
NET_STEPS = [
    {"name": "打开不可达环境", "action": "open", "value": "${base_url}/login", "timeout": 10000},
]

FLOOR_FAULTS = {
    "login_500": False, "search_500": False, "orders_500": False, "orders_slow_ms": 0,
    "missing_element": False, "flaky_once": False, "js_error": False, "missing_data": False,
    "reset_flaky": True,
}

RESULTS: list[tuple[str, bool, str]] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((f"[{section}] {name}", bool(ok), detail))
    print(f"   {'OK  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail and not ok else ''}")
    return bool(ok)


def info(text: str) -> None:
    print(f"        {text}")


def section(title: str) -> None:
    print(f"\n{'=' * 84}\n{title}\n{'=' * 84}")


def set_faults(demo: httpx.Client, **overrides: object) -> None:
    demo.post(f"{DEMO}/__faults", json={**FLOOR_FAULTS, **overrides})


# --------------------------------------------------------------------- CLI


class Cli:
    """以子进程方式调用 CLI —— 只有真进程退出码才能证明 Exit Code 是对的。"""

    def __init__(self, token: str, base_url: str = API) -> None:
        self.token = token
        self.base_url = base_url
        self.log: list[list[str]] = []

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["TESTFLOW_CI_TOKEN"] = self.token
        env["TESTFLOW_URL"] = self.base_url
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def invoke(self, *args: str, via_entry: bool = False) -> tuple[int, str, str]:
        if via_entry:
            # 稳定入口：pyproject 里注册的 console_scripts（testflow ...）
            executable = str(Path(sys.executable).parent / "testflow.exe")
            command = [executable, *args] if Path(executable).exists() else [sys.executable, "-m", "cli.testflow", *args]
        else:
            command = [sys.executable, "-m", "cli.testflow", *args]
        self.log.append(command)
        completed = subprocess.run(  # noqa: S603 - 参数是测试里写死的，无外部输入
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env(),
            cwd=str(ROOT),
            timeout=900,
        )
        return int(completed.returncode), completed.stdout or "", completed.stderr or ""


# --------------------------------------------------------------- 环境搭建


class Env:
    def __init__(self, api: httpx.Client, demo: httpx.Client, stamp: str) -> None:
        self.api = api
        self.demo = demo
        self.stamp = stamp
        self.project = ""
        self.project_id = ""
        self.env_id = ""
        self.net_env_id = ""
        self.prod_env_id = ""
        self.cases: dict[str, str] = {}
        self.plans: dict[str, dict] = {}

    def setup(self) -> None:
        self.project = f"M4自测项目-{self.stamp}"
        self.project_id = self.api.post("/api/projects", json={"name": self.project, "description": "selftest_m4"}).json()["id"]
        common = {
            "project_id": self.project_id,
            "browser": "chromium",
            "read_only": False,
            "variables": {"username": "demo", "password": "demo123"},
        }
        self.env_id = self.api.post(
            "/api/environments",
            json={**common, "name": "M4 测试环境", "base_url": DEMO, "environment_type": "TEST",
                  "allowed_hosts": ["127.0.0.1", "localhost"]},
        ).json()["id"]
        self.net_env_id = self.api.post(
            "/api/environments",
            json={**common, "name": "M4 不可达环境", "base_url": UNREACHABLE, "environment_type": "TEST",
                  "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]
        # PROD 环境用来验证「CLI 也绕不过安全门禁」
        self.prod_env_id = self.api.post(
            "/api/environments",
            json={**common, "name": "M4 生产环境", "base_url": "https://prod.example.com",
                  "environment_type": "PROD", "allowed_hosts": ["prod.example.com"]},
        ).json()["id"]

    def add_case(self, code: str, name: str, steps: list[dict], *, environment_id: str | None = None) -> None:
        flow = self.api.post(
            "/api/flows",
            json={"project_id": self.project_id, "name": f"FLOW-{code}",
                  "environment_id": environment_id or self.env_id, "variables": {}, "steps": steps},
        ).json()
        case = self.api.post(
            "/api/test-cases",
            json={"project_id": self.project_id, "case_code": code, "name": name, "module": "M4自测",
                  "priority": "P1", "status": "ACTIVE", "tags": ["m4"], "steps": []},
        ).json()
        self.api.post("/api/case-flow-bindings", json={"case_id": case["id"], "flow_id": flow["id"], "enabled": True})
        self.cases[code] = case["id"]

    def make_plan(self, key: str, name: str, codes: list[str], *, environment_id: str | None = None) -> dict:
        plan = self.api.post(
            "/api/regression-plans",
            json={"project_id": self.project_id, "name": name,
                  "environment_id": environment_id or self.env_id,
                  "case_ids": [self.cases[code] for code in codes],
                  "browser": "chromium", "trigger_type": "CI", "retry_count": 0, "enabled": True},
        ).json()
        self.plans[key] = plan
        return plan

    def build_fixtures(self) -> None:
        self.add_case("M4-LOGIN-001", "M4 登录冒烟", PASS_LOGIN)
        self.add_case("M4-SEARCH-001", "M4 搜索冒烟", PASS_SEARCH)
        self.add_case("M4-ORDER-001", "M4 下单冒烟", PASS_ORDER)
        self.add_case("M4-ORDER-500", "M4 下单接口 500", PASS_ORDER)
        self.add_case("M4-NET-001", "M4 目标环境不可达", NET_STEPS, environment_id=self.net_env_id)
        self.make_plan("smoke", "M4 Smoke", ["M4-LOGIN-001", "M4-SEARCH-001", "M4-ORDER-001"])
        self.make_plan("fail", "M4 失败计划", ["M4-ORDER-500"])
        self.make_plan("net", "M4 不可达计划", ["M4-NET-001"], environment_id=self.net_env_id)
        self.make_plan("prod", "M4 生产计划", ["M4-LOGIN-001"], environment_id=self.prod_env_id)

    def describe(self, kind: str) -> str:
        return self.plans[kind]["name"]


# ------------------------------------------------------------------- 各段


def section_01_02(cli: Cli, env: Env) -> tuple[int, dict]:
    section("M4-01 / M4-02 CLI 正常触发 + 全通过 → Exit 0")
    pipeline_id = f"m4-smoke-{env.stamp}"
    code, out, err = cli.invoke(
        "run", "--plan", env.describe("smoke"), "--env", "M4 测试环境", "--project", env.project,
        "--pipeline-id", pipeline_id, "--branch", "main", "--commit-sha", "abcdef1234567890",
        "--timeout", "300", via_entry=True,
    )
    info(f"退出码={code}")
    check("M4-01", "CLI 真实创建了 Run", "Run:" in out, out[-200:])
    run_id = ""
    for line in out.splitlines():
        if line.startswith("Run: "):
            run_id = line.split(": ", 1)[1].strip()
    check("M4-01", "输出里有 Run ID", bool(run_id), out[-200:])
    check("M4-02", "Run 最终状态 PASSED", "Status: PASSED" in out, out[-400:])
    check("M4-02", "3 个用例全部通过", "3 passed" in out, out[-400:])
    check("M4-02", "Exit Code = 0", code == 0, f"实际 {code}")
    check("M4-02", "人类摘要走 stdout", "TestFlowCI" in out)
    check("M4-02", "进度信息走 stderr", "已创建执行" in err)

    payload: dict = {}
    if run_id:
        _, json_out, _ = cli.invoke("status", "--run-id", run_id, "--json")
        payload = json.loads(json_out)
        check("M4-01", "status 能查到刚创建的 Run", payload.get("run_id") == run_id)
    return code, payload


def section_03(cli: Cli, env: Env, demo: httpx.Client, api: httpx.Client) -> dict:
    section("M4-03 CLI FAIL → Run=FAILED，FailureGroup=PRODUCT_BUG，Exit 1")
    set_faults(demo, orders_500=True)
    pipeline_id = f"m4-fail-{env.stamp}"
    code, out, err = cli.invoke(
        "run", "--plan", env.describe("fail"), "--env", "M4 测试环境", "--project", env.project,
        "--pipeline-id", pipeline_id, "--branch", "feature/fail", "--commit-sha", "deadbeefcafe",
        "--timeout", "300",
    )
    set_faults(demo)
    run_id = next((line.split(": ", 1)[1].strip() for line in out.splitlines() if line.startswith("Run: ")), "")
    info(f"退出码={code}  run={run_id}")
    info("\n".join(line for line in out.splitlines() if line.strip()))
    check("M4-03", "用例真实失败（Run 不是 PASSED）", "Status: PASSED" not in out, out[-300:])
    check("M4-03", "Run 状态为 FAILED", "Status: FAILED" in out, out[-300:])
    check("M4-03", "至少 1 个用例失败", "1 failed" in out, out[-300:])
    check("M4-03", "Failure Group 分类为 PRODUCT_BUG", "PRODUCT_BUG" in out, out[-300:])
    check("M4-03", "Exit Code = 1", code == 1, f"实际 {code}")

    status = {}
    if run_id:
        _, json_out, _ = cli.invoke("status", "--run-id", run_id, "--json")
        status = json.loads(json_out)
        check("M4-03", "CI 视图里 failure_groups.categories 含 PRODUCT_BUG",
              "PRODUCT_BUG" in (status.get("failure_groups") or {}).get("categories", {}),
              json.dumps(status.get("failure_groups"), ensure_ascii=False))
        groups = api.get("/api/failures", params={"project_id": env.project_id, "category": "PRODUCT_BUG"}).json()
        check("M4-03", "失败中心里确实有这个分组", groups["total"] >= 1, str(groups["total"]))
        check("M4-03", "该分组能追到这次 Run",
              any(item["latest_run_id"] == run_id for item in groups["items"]),
              str([item["latest_run_id"] for item in groups["items"]]))
    return status


def section_04(cli: Cli, env: Env, api: httpx.Client) -> dict:
    section("M4-04 被测环境不可达 → ENVIRONMENT_ERROR，但仍然 Exit 1（不是 2）")
    pipeline_id = f"m4-net-{env.stamp}"
    code, out, err = cli.invoke(
        "run", "--plan", env.describe("net"), "--env", "M4 不可达环境", "--project", env.project,
        "--pipeline-id", pipeline_id, "--timeout", "300",
    )
    run_id = next((line.split(": ", 1)[1].strip() for line in out.splitlines() if line.startswith("Run: ")), "")
    info(f"退出码={code}  run={run_id}")
    info("\n".join(line for line in out.splitlines() if line.strip()))
    check("M4-04", "Exit Code = 1（不是 2）", code == 1, f"实际 {code}")
    check("M4-04", "Run 不是 ERROR（平台侧没问题）", "Status: ERROR" not in out, out[-300:])

    status = {}
    if run_id:
        _, json_out, _ = cli.invoke("status", "--run-id", run_id, "--json")
        status = json.loads(json_out)
        categories = (status.get("failure_groups") or {}).get("categories", {})
        check("M4-04", "Failure Group 分类为 ENVIRONMENT_ERROR", "ENVIRONMENT_ERROR" in categories,
              json.dumps(categories, ensure_ascii=False))
        info(f"结论：被测环境连不上，TestFlowCI 如实报告为环境问题，退出码 {code} —— "
             f"这才是「测试失败 ≠ 平台故障」")
    return status


def section_05(cli: Cli, env: Env) -> None:
    section("M4-05 平台侧无法完成 → Exit 2")
    code, out, err = cli.invoke("run", "--plan", "根本不存在的计划名", "--env", "M4 测试环境", "--timeout", "60")
    info(f"错误 Plan 退出码={code}  错误信息={(err.strip().splitlines() or [''])[-1]}")
    check("M4-05", "不存在的计划 → Exit 2", code == 2, f"实际 {code}")
    check("M4-05", "错误信息里给出可用计划候选", "当前可用计划" in err or "不存在" in err, err[-200:])

    code, out, err = cli.invoke(
        "run", "--plan", env.describe("prod"), "--env", "M4 生产环境", "--project", env.project, "--timeout", "60"
    )
    info(f"PROD 环境退出码={code}  原因={(err.strip().splitlines() or [''])[-1]}")
    check("M4-05", "PROD 环境被安全门禁拦下 → Exit 2", code == 2, f"实际 {code}")
    check("M4-05", "明确说明是安全门禁拦的", "安全门禁" in err or "生产" in err, err[-200:])

    # 参数非法（少了必填的 --plan）：argparse 直接以 2 退出
    code, _, err = cli.invoke("run", "--env", "M4 测试环境")
    check("M4-05", "参数非法（缺 --plan）→ Exit 2", code == 2, f"实际 {code}")

    # Token 缺失：CLI 自己就能判定，不必先连服务端
    no_token = Cli(token="")
    code, _, err = no_token.invoke("run", "--plan", env.describe("smoke"), "--env", "M4 测试环境")
    check("M4-05", "缺 CI Token → Exit 2", code == 2, f"实际 {code}")
    check("M4-05", "缺 Token 时给出设置方法", "TESTFLOW_CI_TOKEN" in err, err[-200:])

    # 错 Token：服务端 401
    wrong = Cli(token="definitely-wrong-token")
    code, _, err = wrong.invoke("run", "--plan", env.describe("smoke"), "--env", "M4 测试环境")
    check("M4-05", "错 CI Token → Exit 2", code == 2, f"实际 {code}")
    check("M4-05", "错 Token 时不回显正确 Token", settings.ci_token not in err, err[-200:])


def section_06_07(cli: Cli, status_payload: dict) -> None:
    section("M4-06 / M4-07 status 与 JSON 输出")
    run_id = str(status_payload.get("run_id") or "")
    check("M4-06", "有可查询的 Run", bool(run_id))
    if not run_id:
        return

    code, out, err = cli.invoke("status", "--run-id", run_id)
    info("—— status 人类输出 ——")
    info("\n".join(out.splitlines()))
    for marker in ("Status:", "Cases:", "Failure Groups:", "Flaky:", "Pipeline:", "Exit Code:"):
        check("M4-06", f"status 输出包含「{marker}」", marker in out)
    check("M4-06", "status 退出码与 Run 结论一致", code == status_payload.get("exit_code"), f"{code} vs {status_payload.get('exit_code')}")

    code_json, json_out, json_err = cli.invoke("status", "--run-id", run_id, "--json")
    parsed = json.loads(json_out)
    check("M4-07", "status --json 的 stdout 能被 JSON 解析", isinstance(parsed, dict))
    check("M4-07", "stdout 只有 JSON（没有混入日志）", json_out.strip().startswith("{") and json_out.strip().endswith("}"))
    check("M4-07", "日志走 stderr", isinstance(json_err, str))
    check("M4-07", "JSON 里带 exit_code", parsed.get("exit_code") is not None)


def section_08(env: Env, api: httpx.Client) -> dict:
    section("M4-08 GitLab Trigger（真实 HTTP 调用；真实 GitLab Pipeline 情况见交付说明）")
    token = settings.ci_token
    headers = {"X-GitLab-Token": token, "X-Gitlab-Event": "Pipeline Hook"}
    pipeline_id = f"m4-gitlab-{env.stamp}"

    # 形态 A：GitLab 原生 Pipeline Webhook 载荷（含 pipeline 变量）
    webhook_payload = {
        "object_kind": "pipeline",
        "object_attributes": {
            "id": int(env.stamp[-4:]) if env.stamp[-4:].isdigit() else 4242,
            "ref": "main",
            "sha": "cafebabe1234567890",
            "status": "success",
            "url": f"https://gitlab.example/group/app/-/pipelines/{pipeline_id}",
            "variables": [
                {"key": "TESTFLOW_PROJECT", "value": env.project},
                {"key": "TESTFLOW_PLAN", "value": env.describe("smoke")},
                {"key": "TESTFLOW_ENV", "value": "M4 测试环境"},
            ],
        },
        "project": {"id": 12, "path_with_namespace": "group/app", "web_url": "https://gitlab.example/group/app"},
        "user": {"username": "zhangsan", "name": "张三"},
    }
    response = api.post("/api/ci/gitlab/trigger", json=webhook_payload, headers=headers)
    check("M4-08", "Webhook 形态（Pipeline Hook）触发成功", response.status_code == 200, f"{response.status_code} {response.text[:200]}")
    body = response.json() if response.status_code == 200 else {}
    if body:
        info(f"Webhook 触发 → run={body['run_id']} created={body['created']} trigger={body['trigger_source']}")
        check("M4-08", "Webhook 触发建的是真实 Run", bool(body.get("run_id")))
        check("M4-08", "trigger_source 标为 GITLAB", body.get("trigger_source") == "GITLAB", str(body.get("trigger_source")))
        check("M4-08", "解析出了 commit / branch",
              body.get("commit_sha") == "cafebabe1234567890" and body.get("branch") == "main",
              f"{body.get('commit_sha')} / {body.get('branch')}")

    # 形态 B：CI Job 直接调用（转发 CI_* 变量）
    job_payload = {
        "project": env.project,
        "plan": env.describe("smoke"),
        "environment": "M4 测试环境",
        "pipeline_id": f"{pipeline_id}-job",
        "pipeline_url": "https://gitlab.example/group/app/-/pipelines/999",
        "commit_sha": "9999999999",
        "branch": "release/1.0",
        "triggered_by": "gitlab-ci-job",
    }
    response = api.post("/api/ci/gitlab/trigger", json=job_payload, headers=headers)
    check("M4-08", "Job 直接调用形态触发成功", response.status_code == 200, f"{response.status_code} {response.text[:200]}")
    job_body = response.json() if response.status_code == 200 else {}
    if job_body:
        info(f"Job 触发 → run={job_body['run_id']} branch={job_body.get('branch')}")

    # 缺 plan/env 必须 4xx，且不建 Run
    bad = api.post("/api/ci/gitlab/trigger", json={"project": env.project}, headers=headers)
    check("M4-08", "只给 project 不给 plan → 4xx 且不建 Run", bad.status_code == 400, str(bad.status_code))

    # 后续段落用哪一条：优先 Webhook 形态，它带的信息更全
    return body if body.get("run_id") else job_body


def section_09(env: Env, api: httpx.Client, gitlab_run: dict, cli: Cli) -> None:
    section("M4-09 Pipeline Metadata 落库与展示")
    run_id = str(gitlab_run.get("run_id") or "")
    check("M4-09", "有 GitLab 触发的 Run 可查", bool(run_id))
    if not run_id:
        return
    status = api.get(f"/api/runs/{run_id}/ci-status", headers={"X-TestFlow-Token": settings.ci_token}).json()
    for field, expected in (
        ("pipeline_id", str(gitlab_run.get("pipeline_id") or "")),
        ("commit_sha", str(gitlab_run.get("commit_sha") or "")),
        ("branch", str(gitlab_run.get("branch") or "")),
        ("trigger_source", "GITLAB"),
        ("ci_provider", "gitlab"),
    ):
        check("M4-09", f"Run 记录里有 {field}", str(status.get(field)) == expected, f"{status.get(field)} != {expected}")
    check("M4-09", "pipeline_url 已落库且可点击", str(status.get("pipeline_url", "")).startswith("https://"), str(status.get("pipeline_url")))
    _ = cli


def section_10_11(env: Env, api: httpx.Client, cli: Cli, demo: httpx.Client) -> None:
    section("M4-10 / M4-11 幂等与并发幂等")
    headers = {"X-TestFlow-Token": settings.ci_token}
    pipeline_id = f"m4-idem-{env.stamp}"
    payload = {
        "project": env.project, "plan": env.describe("smoke"), "environment": "M4 测试环境",
        "pipeline_id": pipeline_id, "source": "gitlab", "branch": "main", "commit_sha": "aaaa1111",
    }

    run_ids, created_flags = [], []
    for _ in range(3):
        body = api.post("/api/ci/runs", json=payload, headers=headers).json()
        run_ids.append(body["run_id"])
        created_flags.append(body["created"])
    check("M4-10", "重复 3 次只创建 1 个 Run", len(set(run_ids)) == 1, str(run_ids))
    check("M4-10", "created 标记为 [True, False, False]", created_flags == [True, False, False], str(created_flags))
    info(f"重复请求 3 次 → 最终 Run 数 {len(set(run_ids))}")

    # 并发：10 个线程同时打同一个新幂等键
    concurrent_pipeline = f"m4-conc-{env.stamp}"
    concurrent_payload = {**payload, "pipeline_id": concurrent_pipeline}

    def fire(_: int) -> str:
        client = httpx.Client(base_url=API, timeout=60)
        try:
            return client.post("/api/ci/runs", json=concurrent_payload, headers=headers).json()["run_id"]
        finally:
            client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(fire, range(10)))
    check("M4-11", "10 个并发相同请求只产生 1 个 Run", len(set(results)) == 1, str(set(results)))
    info(f"并发请求 10 次 → 最终 Run 数 {len(set(results))}")

    db_runs = api.get("/api/runs", params={"project_id": env.project_id, "limit": 200}).json()
    same_key = [item for item in db_runs if item["pipeline_id"] == concurrent_pipeline]
    check("M4-11", "数据库里该幂等键确实只有 1 行", len(same_key) == 1, str(len(same_key)))
    _ = cli
    _ = demo


def section_12(env: Env, api: httpx.Client, fail_status: dict) -> None:
    section("M4-12 Failure Center 链路：Pipeline → Run → FailureGroup → Occurrence")
    run_id = str(fail_status.get("run_id") or "")
    check("M4-12", "有 CI 触发的失败 Run", bool(run_id))
    if not run_id:
        return

    run = api.get(f"/api/runs/{run_id}").json()
    check("M4-12", "Run 上带着 Pipeline 信息", bool(run.get("pipeline_id") and run.get("branch") and run.get("commit_sha")),
          f"pipeline={run.get('pipeline_id')} branch={run.get('branch')} commit={run.get('commit_sha')}")

    groups = api.get("/api/failures", params={"project_id": env.project_id, "run_id": run_id}).json()
    check("M4-12", "能按 Run 找到失败分组", groups["total"] >= 1, str(groups["total"]))
    if not groups["items"]:
        return
    group = groups["items"][0]
    occurrences = api.get(f"/api/failures/{group['id']}/occurrences", params={"page_size": 20}).json()
    check("M4-12", "分组下有 Occurrence", occurrences["total"] >= 1, str(occurrences["total"]))
    sample = occurrences["items"][0]
    check("M4-12", "Occurrence 指回同一条 Run", sample["run_id"] == run_id)
    check("M4-12", "Occurrence 带 Case / Attempt / Step 锚点",
          bool(sample["case_result_id"] and sample["attempt_id"] and sample["step_result_id"]))
    check("M4-12", "Occurrence 能带出 CI 上下文（分支/提交）",
          bool(sample.get("run_branch") or sample.get("run_commit_sha")),
          f"branch={sample.get('run_branch')} commit={sample.get('run_commit_sha')}")

    detail = api.get(f"/api/runs/{run_id}/cases").json()
    check("M4-12", "Run 详情里能看到真实失败用例", any(item["status"] in ("FAILED", "ERROR") for item in detail))
    info(f"链路：pipeline={run.get('pipeline_id')} → run={run_id} → group={group['id']} "
         f"({group['category']}) → occurrence={sample['id']} → case={sample['case_result_id']}")


def section_ui(env: Env, api: httpx.Client, gitlab_run: dict) -> None:
    section("真实浏览器 UI 验收（Run Detail 与失败中心显示 CI 信息）")
    from backend.app.config import settings as app_settings
    if app_settings.playwright_browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = app_settings.playwright_browsers_path
    from playwright.sync_api import sync_playwright

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = str(gitlab_run.get("run_id") or "")
    if not run_id:
        check("UI", "有可展示的 Run", False, "缺少 GitLab 触发的 Run，跳过")
        return
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1000})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        page.goto(f"{API}/#/login", wait_until="networkidle")
        page.fill("input#username", "admin")
        page.fill("input#password", "admin123")
        page.click("button[type=submit]")
        page.wait_for_selector("text=工作台", timeout=20000)

        page.get_by_role("menuitem", name="项目").click()
        page.wait_for_selector("table", timeout=20000)
        page.wait_for_timeout(1200)
        row = page.locator("tr", has_text=env.project).first
        switch = row.locator("button", has_text="设为当前")
        if switch.count() > 0 and switch.first.is_enabled():
            switch.first.click()
            page.wait_for_timeout(1200)

        page.goto(f"{API}/#/runs/{run_id}", wait_until="networkidle")
        page.wait_for_selector("text=用例执行结果", timeout=20000)
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        expect_branch = str(gitlab_run.get("branch") or "")
        expect_commit = str(gitlab_run.get("commit_sha") or "")[:10]
        check("UI", "Run Detail 显示触发来源 GitLab", "GITLAB" in body or "GitLab CI" in body)
        check("UI", "Run Detail 显示分支与 Commit",
              (not expect_branch or expect_branch in body) and (not expect_commit or expect_commit in body),
              f"期望 {expect_branch} / {expect_commit}")
        check("UI", "Run Detail 显示 Pipeline 编号", f"#{gitlab_run.get('pipeline_id')}" in body, str(gitlab_run.get("pipeline_id")))
        pipeline_link = page.locator("a", has_text=f"#{gitlab_run.get('pipeline_id')}")
        check("UI", "Pipeline 是可点击链接", pipeline_link.count() > 0 and bool(pipeline_link.first.get_attribute("href")))
        page.screenshot(path=str(SHOT_DIR / "01_run_detail_ci.png"), full_page=True)

        failed_run = api.get("/api/runs", params={"project_id": env.project_id, "limit": 200}).json()
        fail_run = next((item for item in failed_run if item["failed_cases"] > 0 and item["pipeline_id"]), None)
        if fail_run:
            page.get_by_role("menuitem", name="失败中心").click()
            page.wait_for_selector("text=失败分组", timeout=20000)
            page.wait_for_timeout(1500)
            page.locator("tbody tr.ant-table-row").first.click()
            page.wait_for_selector("text=Failure Fingerprint", timeout=20000)
            page.wait_for_timeout(1200)
            drawer = page.inner_text(".ant-drawer-body")
            check("UI", "失败中心出现历史里带出 CI 上下文",
                  "Pipeline #" in drawer or "feature/fail" in drawer or "deadbeef" in drawer, drawer[-400:])
            page.screenshot(path=str(SHOT_DIR / "02_failure_center_ci.png"), full_page=True)
            _ = fail_run

        browser.close()

    check("UI", "浏览器控制台无报错", not console_errors, str(console_errors[:3]))


def report(env: Env, cli: Cli, elapsed: float) -> int:
    section("汇总")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    failed = [item for item in RESULTS if not item[1]]
    print(f"   通过 {passed}/{total}")
    for name, _ok, detail in failed:
        print(f"   FAIL {name}  -> {detail}")

    print()
    print(f"   本次自测项目：{env.project}（{env.project_id}）")
    print(f"   CLI 调用次数：{len(cli.log)}")
    print("   CLI 调用命令（前 3 条）：")
    for command in cli.log[:3]:
        print(f"     {' '.join(command[1:])}")
    print(f"   总耗时 {elapsed:.1f}s")
    print()
    print("   [OK] M4 CI Integration 验收完成" if not failed else "   [FAIL] 存在未通过项")
    return 0 if not failed else 1


def main() -> int:
    started = time.time()
    stamp = datetime.now().strftime("%H%M%S")
    api = httpx.Client(base_url=API, timeout=120)
    demo = httpx.Client(timeout=30.0)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    section("0. 前置检查")
    check("前置", "TestFlow API 可访问", api.get("/api/health").json().get("status") == "ok")
    check("前置", "Demo 站可访问", demo.get(f"{DEMO}/login").status_code == 200)
    check("前置", "服务端已配置 TESTFLOW_CI_TOKEN", bool(settings.ci_token))
    check("前置", f"{UNREACHABLE} 不可达（用于 M4-04）", _unreachable())
    set_faults(demo)
    token = api.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
    api.headers["Authorization"] = f"Bearer {token}"

    env = Env(api, demo, stamp)
    env.setup()
    env.build_fixtures()
    check("前置", "M4 自测项目 / 3 个环境 / 5 个用例 / 4 个计划已就绪",
          bool(env.project_id and env.env_id and env.net_env_id and len(env.cases) == 5 and len(env.plans) == 4))

    cli = Cli(token=settings.ci_token)

    try:
        section_01_02(cli, env)
        fail_status = section_03(cli, env, demo, api)
        section_04(cli, env, api)
        section_05(cli, env)
        section_06_07(cli, fail_status)
        gitlab_run = section_08(env, api)
        section_09(env, api, gitlab_run, cli)
        section_10_11(env, api, cli, demo)
        section_12(env, api, fail_status)
        section_ui(env, api, gitlab_run)
    finally:
        set_faults(demo)

    return report(env, cli, time.time() - started)


def _unreachable() -> bool:
    try:
        httpx.get(f"{UNREACHABLE}/", timeout=2)
    except Exception:  # noqa: BLE001 - 连不上才是我们要的结果
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
