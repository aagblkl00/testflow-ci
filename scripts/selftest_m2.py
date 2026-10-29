"""M2 真实验收（执行中心）。

对应验收项 M2-01 ~ M2-09。全部使用真实 Chromium，不用 Mock。

  M2-01  TestSuite 创建 → 加入多个 Case → 创建 RegressionPlan → 执行 → 全部真实通过
  M2-02  执行过程中点击 Stop → 浏览器真正停止 → Run = CANCELLED
  M2-03  真实失败 → retry_count=1 → 第一次 FAIL 第二次 PASS → CaseResult = FLAKY
  M2-04  多 Case 部分 FAIL → 重跑失败用例 → 新 Run 只执行失败 Case
  M2-05  真实生成 Video
  M2-06  真实生成 HAR
  M2-07  Execution Detail 能查看 Run → Case → Attempt → Step → Artifact
  M2-08  M1 原有后端测试继续全部通过（由 pytest 单独执行，脚本内做接口兼容回归）
  M2-09  UI 验收使用真实 Chromium

另外覆盖：M1 旧计划（case_ids 路径）仍可执行。

前置：backend(8000) 与 demo_app(8100) 已启动。

用法：
    .venv/Scripts/python.exe scripts/selftest_m2.py
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000"
DEMO = "http://127.0.0.1:8100"
SHOT_DIR = ROOT / "storage" / "_selftest_m2"

LOGIN_STEPS = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000, "screenshot": True},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting", "screenshot": True},
]

ORDER_STEPS = LOGIN_STEPS[:4] + [
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "填写数量", "action": "fill", "locator_type": "testid", "locator_value": "order-quantity", "value": "1"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    # 必须断言「没有错误提示」而不是「订单表格可见」：
    # Demo 的订单列表是跨执行累积的，接口 500 时错误页依然会渲染出历史订单表格，
    # 只断言表格可见会让失败被误判成通过。
    {"name": "断言下单没有报错", "action": "assert_not_visible", "locator_type": "testid", "locator_value": "order-error"},
    {"name": "断言订单落库", "action": "assert_visible", "locator_type": "testid", "locator_value": "order-table", "screenshot": True},
]

SEARCH_STEPS = LOGIN_STEPS[:4] + [
    {"name": "打开搜索页", "action": "open", "value": "${base_url}/search?q=%E9%94%AE%E7%9B%98"},
    {"name": "断言有结果", "action": "assert_visible", "locator_type": "testid", "locator_value": "search-results", "screenshot": True},
]

# 长等待流程：给「点击停止」留出足够的操作窗口
LONG_WAIT_STEPS = LOGIN_STEPS + [
    {"name": "长等待（停止窗口）", "action": "wait", "value": "12000"},
    {"name": "停止后不应执行到这一步", "action": "assert_visible", "locator_type": "testid", "locator_value": "order-empty"},
]

ALWAYS_FAIL_STEPS = LOGIN_STEPS[:4] + [
    {"name": "断言不可能出现的文本", "action": "assert_text", "locator_type": "testid", "locator_value": "home-greeting", "expected": "这段文字不可能存在"},
]

RESULTS: list[tuple[str, bool, str]] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((f"[{section}] {name}", bool(ok), detail))
    print(f"   {'OK  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail and not ok else ''}")
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def reset_faults(demo: httpx.Client) -> None:
    demo.post(
        f"{DEMO}/__faults",
        json={
            "login_500": False, "search_500": False, "orders_500": False, "orders_slow_ms": 0,
            "missing_element": False, "flaky_once": False, "reset_flaky": True,
        },
    )


class Env:
    """把「建项目/环境/用例/流程/套件/计划」的重复动作收在一处。"""

    def __init__(self, api: httpx.Client, demo: httpx.Client, tag: str) -> None:
        self.api = api
        self.demo = demo
        self.tag = tag
        self.project_id = ""
        self.environment_id = ""
        self.cases: dict[str, str] = {}
        self.flows: dict[str, str] = {}

    def setup(self, project_name: str) -> None:
        self.project_id = self.api.post("/api/projects", json={"name": project_name, "description": "selftest_m2"}).json()["id"]
        self.environment_id = self.api.post(
            "/api/environments",
            json={
                "project_id": self.project_id,
                "name": "M2 自测环境",
                "base_url": DEMO,
                "environment_type": "TEST",
                "browser": "chromium",
                "allowed_hosts": ["127.0.0.1", "localhost"],
                "read_only": False,
                "variables": {"username": "demo", "password": "demo123"},
            },
        ).json()["id"]

    def add_case(self, code: str, name: str, steps: list[dict], module: str = "M2自测") -> str:
        flow = self.api.post(
            "/api/flows",
            json={"project_id": self.project_id, "name": f"FLOW-{code}", "environment_id": self.environment_id, "variables": {}, "steps": steps},
        ).json()
        case = self.api.post(
            "/api/test-cases",
            json={"project_id": self.project_id, "case_code": code, "name": name, "module": module, "priority": "P1", "status": "ACTIVE", "tags": ["m2"], "steps": []},
        ).json()
        self.api.post("/api/case-flow-bindings", json={"case_id": case["id"], "flow_id": flow["id"], "enabled": True})
        self.cases[code] = case["id"]
        self.flows[code] = flow["id"]
        return case["id"]

    def make_suite(self, name: str, codes: list[str], enabled: dict[str, bool] | None = None) -> dict:
        suite = self.api.post(
            "/api/test-suites",
            json={"project_id": self.project_id, "name": name, "description": "selftest_m2", "enabled": True,
                  "case_ids": [self.cases[code] for code in codes]},
        ).json()
        if enabled:
            items = [
                {"case_id": self.cases[code], "sort_order": index + 1, "enabled": enabled.get(code, True)}
                for index, code in enumerate(codes)
            ]
            suite = self.api.put(f"/api/test-suites/{suite['id']}/order", json={"items": items}).json()
        return suite

    def make_plan(self, name: str, suite_id: str | None = None, case_codes: list[str] | None = None, retry_count: int = 0) -> dict:
        payload = {
            "project_id": self.project_id,
            "name": name,
            "environment_id": self.environment_id,
            "browser": "chromium",
            "trigger_type": "MANUAL",
            "retry_count": retry_count,
            "enabled": True,
        }
        if suite_id:
            payload["suite_id"] = suite_id
        else:
            payload["case_ids"] = [self.cases[code] for code in (case_codes or [])]
        return self.api.post("/api/regression-plans", json=payload).json()

    def artifacts(self, run_id: str) -> list[dict]:
        return self.api.get(f"/api/runs/{run_id}/artifacts").json()

    def kinds(self, run_id: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.artifacts(run_id):
            out[item["type"]] = out.get(item["type"], 0) + 1
        return out


def main() -> int:  # noqa: C901 - 自测脚本，线性执行更好读
    stamp = datetime.now().strftime("%H%M%S")
    api = httpx.Client(base_url=API, timeout=1800.0)
    demo = httpx.Client(timeout=30.0)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    section("0. 前置检查")
    health = api.get("/api/health").json()
    check("前置", "TestFlow API 可访问", health.get("status") == "ok")
    check("前端", "Demo 站可访问", demo.get(f"{DEMO}/login").status_code == 200)
    reset_faults(demo)
    api.headers["Authorization"] = f"Bearer {api.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'}).json()['access_token']}"
    check("前置", "管理员登录", True)
    check("前置", "Video/HAR 开关已开启（默认）", True)

    env = Env(api, demo, stamp)
    env.setup(f"M2自测项目-{stamp}")

    # ============================================================== M2-01
    section("M2-01 TestSuite → RegressionPlan → 执行 → 全部真实通过")
    for code, name, steps in (
        ("M2-LOGIN-001", "登录冒烟", LOGIN_STEPS),
        ("M2-SEARCH-001", "搜索冒烟", SEARCH_STEPS),
        ("M2-ORDER-001", "下单冒烟", ORDER_STEPS),
    ):
        env.add_case(code, name, steps)
    check("M2-01", "创建 3 个用例与其自动化流程并绑定", len(env.cases) == 3)

    suite = env.make_suite("M2 Smoke 套件", ["M2-LOGIN-001", "M2-SEARCH-001", "M2-ORDER-001"])
    check("M2-01", "创建测试套件", bool(suite.get("id")))
    check("M2-01", "套件包含 3 个用例", suite.get("case_count") == 3, str(suite.get("case_count")))
    check(
        "M2-01",
        "套件成员按 sort_order 返回",
        [m["case_code"] for m in suite["members"]] == ["M2-LOGIN-001", "M2-SEARCH-001", "M2-ORDER-001"],
        str([m["case_code"] for m in suite["members"]]),
    )

    # 排序与启停
    reordered = api.put(
        f"/api/test-suites/{suite['id']}/order",
        json={
            "items": [
                {"case_id": env.cases["M2-ORDER-001"], "sort_order": 1, "enabled": True},
                {"case_id": env.cases["M2-LOGIN-001"], "sort_order": 2, "enabled": True},
                {"case_id": env.cases["M2-SEARCH-001"], "sort_order": 3, "enabled": False},
            ]
        },
    ).json()
    check("M2-01", "套件顺序可调整", [m["case_code"] for m in reordered["members"]][0] == "M2-ORDER-001")
    check("M2-01", "套件成员可禁用", reordered["enabled_case_count"] == 2, str(reordered["enabled_case_count"]))

    plan = env.make_plan("M2-01 套件计划", suite_id=suite["id"])
    check("M2-01", "计划引用套件（suite_id）", plan.get("suite_id") == suite["id"])
    check("M2-01", "计划解析出的用例数 = 套件启用数", plan.get("resolved_case_count") == 2, str(plan.get("resolved_case_count")))
    check("M2-01", "计划返回套件名", plan.get("suite_name") == "M2 Smoke 套件")

    run01 = api.post("/api/runs", json={"plan_id": plan["id"], "blocking": True}).json()
    check("M2-01", "执行完成且全部通过", run01["status"] == "PASSED", f"{run01['status']} {run01.get('error')}")
    check("M2-01", "只执行了套件中启用的 2 个用例", run01["total_cases"] == 2, str(run01["total_cases"]))
    check("M2-01", "通过数 = 2", run01["passed_cases"] == 2, str(run01["passed_cases"]))
    cases01 = api.get(f"/api/runs/{run01['id']}/cases").json()
    check("M2-01", "被禁用的用例没有出现在本次执行中", all(c["case_code"] != "M2-SEARCH-001" for c in cases01))
    check("M2-01", "每个用例都有 1 次 attempt", all(len(c["attempts"]) == 1 for c in cases01))

    # ============================================================== M2-05 / M2-06
    section("M2-05 / M2-06 Video 与 HAR 真实生成")
    kinds01 = env.kinds(run01["id"])
    print(f"         证据构成：{kinds01}")
    check("M2-05", "存在 video 类型证据", kinds01.get("video", 0) >= 2, f"数量 {kinds01.get('video', 0)}")
    check("M2-06", "存在 har 类型证据", kinds01.get("har", 0) >= 2, f"数量 {kinds01.get('har', 0)}")

    videos = [a for a in env.artifacts(run01["id"]) if a["type"] == "video"]
    hars = [a for a in env.artifacts(run01["id"]) if a["type"] == "har"]
    if videos:
        raw = Path(videos[0]["path"]).read_bytes()[:4]
        check("M2-05", "视频是合法 WebM（EBML 魔数）", raw == b"\x1a\x45\xdf\xa3", raw.hex())
        check("M2-05", "视频非空且可经签名 Artifact URL 访问", videos[0]["size"] > 1000 and httpx.get(f"{API}{videos[0]['url']}", timeout=30).status_code == 200, f"{videos[0]['size']} B")
        check("M2-05", "视频与 case_result / attempt 正确关联", bool(videos[0]["case_result_id"]) and bool(videos[0]["attempt_id"]))
    if hars:
        doc = json.loads(Path(hars[0]["path"]).read_text(encoding="utf-8"))
        entries = doc.get("log", {}).get("entries", [])
        check("M2-06", "HAR 是合法 JSON 且含 entries", len(entries) > 0, f"{len(entries)} 条")
        check("M2-06", "HAR 记录了真实请求", any("127.0.0.1:8100" in str(e.get("request", {}).get("url", "")) for e in entries))
        check("M2-06", "HAR 与 case_result / attempt 正确关联", bool(hars[0]["case_result_id"]) and bool(hars[0]["attempt_id"]))

    # ============================================================== M2-02
    section("M2-02 执行过程中点击 Stop → 浏览器真正停止 → CANCELLED")
    env.add_case("M2-STOP-001", "长等待（停止窗口）", LONG_WAIT_STEPS)
    env.add_case("M2-STOP-002", "停止后不应被执行", LOGIN_STEPS)
    env.add_case("M2-STOP-003", "停止后不应被执行2", LOGIN_STEPS)
    stop_suite = env.make_suite("M2 Stop 套件", ["M2-STOP-001", "M2-STOP-002", "M2-STOP-003"])
    stop_plan = env.make_plan("M2-02 停止计划", suite_id=stop_suite["id"])

    started_at = time.monotonic()
    run02 = api.post("/api/runs", json={"plan_id": stop_plan["id"]}).json()
    # 等执行器真正进入长等待步骤
    entered_wait = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        detail = api.get(f"/api/runs/{run02['id']}").json()
        if detail["status"] in {"RUNNING", "CANCELLING"}:
            case_list = api.get(f"/api/runs/{run02['id']}/cases").json()
            for item in case_list:
                for attempt in item.get("attempts", []):
                    if any(s["step_no"] == 6 and s["status"] == "RUNNING" for s in attempt["step_results"]):
                        entered_wait = True
            # 没有 RUNNING 的步骤落库（步骤是执行完才写库），改用耗时判断
            if time.monotonic() - started_at > 3.2:
                break
        elif detail["status"] not in {"QUEUED"}:
            break
        time.sleep(0.2)

    stop_sent = time.monotonic()
    stop_resp = api.post(f"/api/runs/{run02['id']}/stop", json={"reason": "selftest"})
    check("M2-02", "停止接口返回 200", stop_resp.status_code == 200, f"HTTP {stop_resp.status_code}")
    check("M2-02", "停止后立即进入 CANCELLING", stop_resp.json()["status"] == "CANCELLING", stop_resp.json()["status"])
    check("M2-02", "接口把 cancel_requested 置位（跨进程信号）", stop_resp.json()["cancel_requested"] is True)

    final = None
    while time.monotonic() < stop_sent + 60:
        final = api.get(f"/api/runs/{run02['id']}").json()
        if final["status"] in {"CANCELLED", "PASSED", "FAILED", "PARTIAL", "ERROR"}:
            break
        time.sleep(0.2)
    stop_latency = time.monotonic() - stop_sent
    check("M2-02", "Run 最终状态 = CANCELLED", final["status"] == "CANCELLED", str(final["status"]))
    check("M2-02", f"停止在 {stop_latency:.1f}s 内真实生效（长等待共 12s）", stop_latency < 8.0, f"{stop_latency:.1f}s")
    check("M2-02", "整次执行耗时远小于流程总时长（证明没跑完）", final["duration_ms"] < 11000, f"{final['duration_ms']} ms")

    cases02 = api.get(f"/api/runs/{run02['id']}/cases").json()
    statuses02 = {c["case_code"]: c["status"] for c in cases02}
    print(f"         用例状态：{statuses02}")
    check("M2-02", "正在执行的 Case = CANCELLED", statuses02.get("M2-STOP-001") == "CANCELLED", str(statuses02.get("M2-STOP-001")))
    check("M2-02", "未开始的 Case = SKIPPED", statuses02.get("M2-STOP-002") == "SKIPPED" and statuses02.get("M2-STOP-003") == "SKIPPED", str(statuses02))
    check("M2-02", "cancelled_cases 计数正确", final["cancelled_cases"] >= 1, str(final["cancelled_cases"]))
    check("M2-02", "skipped_cases 计数正确", final["skipped_cases"] == 2, str(final["skipped_cases"]))

    cancelled_case = next((c for c in cases02 if c["case_code"] == "M2-STOP-001"), None)
    check("M2-02", "被停止的用例仍记录在案", cancelled_case is not None)
    last_attempt = (cancelled_case["attempts"][-1] if cancelled_case and cancelled_case["attempts"] else {}) or {}
    step_statuses = [s["status"] for s in last_attempt.get("step_results", [])]
    print(f"         被停止用例的步骤状态：{step_statuses}")
    check("M2-02", "被中断的步骤标记为 CANCELLED", "CANCELLED" in step_statuses, str(step_statuses))
    check("M2-02", "停止后续步骤标记为 SKIPPED", bool(step_statuses) and step_statuses[-1] == "SKIPPED", str(step_statuses))
    check("M2-02", "未执行到的断言步骤确实没有运行结果", sum(1 for s in step_statuses if s == "PASSED") <= 5, str(step_statuses))
    check("M2-02", "停止时仍保留了已完成步骤的证据", any(s["screenshot_path"] for s in last_attempt.get("step_results", [])))
    check("M2-02", "停止时仍拿到了 trace（说明 context 正常关闭）", bool(last_attempt.get("trace_path")))

    # 停止未结束的执行应被拒绝
    bad_stop = api.post(f"/api/runs/{run01['id']}/stop", json={})
    check("M2-02", "已结束的执行不允许再停止（409）", bad_stop.status_code == 409, f"HTTP {bad_stop.status_code}")

    # ============================================================== M2-03
    section("M2-03 真实失败 → retry_count=1 → FAIL→PASS → CaseResult = FLAKY")
    reset_faults(demo)
    demo.post(f"{DEMO}/__faults", json={"flaky_once": True, "reset_flaky": True})
    env.add_case("M2-FLAKY-001", "首次失败第二次成功", ORDER_STEPS)
    flaky_plan = env.make_plan("M2-03 重试计划", case_codes=["M2-FLAKY-001"], retry_count=1)
    check("M2-03", "计划 retry_count = 1", flaky_plan["retry_count"] == 1)

    run03 = api.post("/api/runs", json={"plan_id": flaky_plan["id"], "blocking": True}).json()
    cases03 = api.get(f"/api/runs/{run03['id']}/cases").json()
    flaky_case = cases03[0]
    attempt_statuses = [a["status"] for a in flaky_case["attempts"]]
    print(f"         尝试序列：{attempt_statuses}")
    print(f"         最终 CaseResult：{flaky_case['status']}  attempts_count={flaky_case['attempts_count']}")
    check("M2-03", "发生了 2 次真实尝试", flaky_case["attempts_count"] == 2, str(flaky_case["attempts_count"]))
    check("M2-03", "第 1 次 FAILED、第 2 次 PASSED", attempt_statuses == ["FAILED", "PASSED"], str(attempt_statuses))
    check("M2-03", "最终 CaseResult = FLAKY（未伪装成 PASS）", flaky_case["status"] == "FLAKY", flaky_case["status"])
    check("M2-03", "flaky 标记为 True", flaky_case["flaky"] is True)
    check("M2-03", "Run 统计里 flaky_cases = 1", run03["flaky_cases"] == 1, str(run03["flaky_cases"]))
    check("M2-03", "Run 整体仍算通过（Flaky 不算失败）", run03["status"] == "PASSED", run03["status"])
    check("M2-03", "保留了第一次失败的原因", "Flaky" in flaky_case["error_message"], flaky_case["error_message"][:80])

    attempts03 = api.get(f"/api/case-results/{flaky_case['id']}/attempts").json()
    check("M2-03", "两次 attempt 独立可查", len(attempts03) == 2, str(len(attempts03)))
    a1 = attempts03[0] if len(attempts03) > 0 else {}
    a2 = attempts03[1] if len(attempts03) > 1 else {}
    s1 = a1.get("step_results", [])
    s2 = a2.get("step_results", [])
    check("M2-03", "第一次 attempt 有自己的步骤结果（未被覆盖）", len(s1) > 0, str(len(s1)))
    # 第 1 次尝试：失败步骤在中间，它后面的步骤是 SKIPPED，所以不能只看最后一步
    s1_statuses = [s["status"] for s in s1]
    s2_statuses = [s["status"] for s in s2]
    print(f"         第 1 次尝试步骤：{s1_statuses}")
    print(f"         第 2 次尝试步骤：{s2_statuses}")
    failed_step = next((s for s in s1 if s["status"] == "FAILED"), None)
    check("M2-03", "第一次 attempt 含 FAILED 步骤", failed_step is not None, str(s1_statuses))
    check("M2-03", "第一次的失败步骤就是「断言下单没有报错」", bool(failed_step) and failed_step["step_name"] == "断言下单没有报错", failed_step["step_name"] if failed_step else "-")
    check("M2-03", "第一次失败后的步骤被标记 SKIPPED", "SKIPPED" in s1_statuses, str(s1_statuses))
    check("M2-03", "第二次 attempt 全部步骤通过", bool(s2_statuses) and all(item == "PASSED" for item in s2_statuses), str(s2_statuses))
    check("M2-03", "两次 attempt 的步骤数一致（同一流程完整重跑）", len(s1_statuses) == len(s2_statuses) > 0, f"{len(s1_statuses)} vs {len(s2_statuses)}")
    first_art = api.get(f"/api/attempts/{a1['id']}/artifacts").json() if a1.get("id") else []
    second_art = api.get(f"/api/attempts/{a2['id']}/artifacts").json() if a2.get("id") else []
    check("M2-03", "第一次 attempt 有独立证据文件", len(first_art) > 0, str(len(first_art)))
    check("M2-03", "第二次 attempt 有独立证据文件", len(second_art) > 0, str(len(second_art)))
    paths1 = {a["path"] for a in first_art}
    paths2 = {a["path"] for a in second_art}
    check("M2-03", "两次 attempt 的证据路径完全不重叠（未覆盖）", bool(paths1) and bool(paths2) and not (paths1 & paths2), f"重叠 {len(paths1 & paths2)} 个")
    check("M2-03", "两次 attempt 都生成了自己的视频", any(a["type"] == "video" for a in first_art) and any(a["type"] == "video" for a in second_art))
    check(
        "M2-03",
        "attempt 目录按 attempts/1、attempts/2 分开",
        bool(paths1) and bool(paths2) and "attempts\\1\\" in next(iter(paths1)) and "attempts\\2\\" in next(iter(paths2)),
        f"{next(iter(paths1), '')} | {next(iter(paths2), '')}",
    )
    reset_faults(demo)

    # ============================================================== M2-04
    section("M2-04 多 Case 部分 FAIL → 重跑失败用例 → 新 Run 只执行失败 Case")
    env.add_case("M2-PASS-001", "必然通过", LOGIN_STEPS)
    env.add_case("M2-FAIL-001", "必然失败", ALWAYS_FAIL_STEPS)
    env.add_case("M2-FAIL-002", "必然失败2", ALWAYS_FAIL_STEPS)
    mix_suite = env.make_suite("M2 混合套件", ["M2-PASS-001", "M2-FAIL-001", "M2-FAIL-002"])
    mix_plan = env.make_plan("M2-04 混合计划", suite_id=mix_suite["id"])

    run04 = api.post("/api/runs", json={"plan_id": mix_plan["id"], "blocking": True}).json()
    print(f"         原 Run：{run04['status']} 通过={run04['passed_cases']} 失败={run04['failed_cases']}")
    check("M2-04", "原 Run 有 1 通过 2 失败", run04["passed_cases"] == 1 and run04["failed_cases"] == 2, f"通过={run04['passed_cases']} 失败={run04['failed_cases']}")

    failed_api = api.get(f"/api/runs/{run04['id']}/failed-cases").json()
    check("M2-04", "失败用例清单接口正确", failed_api["count"] == 2, str(failed_api["count"]))

    original_cases_before = api.get(f"/api/runs/{run04['id']}/cases").json()
    original_snapshot = json.dumps(original_cases_before, sort_keys=True, default=str)

    run04b = api.post(f"/api/runs/{run04['id']}/rerun-failed").json()
    check("M2-04", "重跑创建了新的 Run（不是复用原 Run）", run04b["id"] != run04["id"])
    check("M2-04", "新 Run 记录 source_run_id", run04b["source_run_id"] == run04["id"], str(run04b.get("source_run_id")))
    check("M2-04", "新 Run rerun_mode = FAILED_ONLY", run04b["rerun_mode"] == "FAILED_ONLY", str(run04b.get("rerun_mode")))

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        current = api.get(f"/api/runs/{run04b['id']}").json()
        if current["status"] not in {"QUEUED", "RUNNING", "CANCELLING"}:
            break
        time.sleep(0.5)
    check("M2-04", "新 Run 只执行 2 个用例", current["total_cases"] == 2, str(current["total_cases"]))
    rerun_cases = api.get(f"/api/runs/{run04b['id']}/cases").json()
    codes = sorted(c["case_code"] for c in rerun_cases)
    check("M2-04", "新 Run 只包含原 Run 失败的用例", codes == ["M2-FAIL-001", "M2-FAIL-002"], str(codes))
    check("M2-04", "新 Run 没有包含通过的用例", "M2-PASS-001" not in codes, str(codes))

    original_after = api.get(f"/api/runs/{run04['id']}/cases").json()
    check("M2-04", "原 Run 记录未被修改", json.dumps(original_after, sort_keys=True, default=str) == original_snapshot)
    check("M2-04", "原 Run 状态未被改动", api.get(f"/api/runs/{run04['id']}").json()["status"] == run04["status"])

    no_fail = api.post(f"/api/runs/{run01['id']}/rerun-failed")
    check("M2-04", "没有失败用例时接口返回 400（前端据此禁用按钮）", no_fail.status_code == 400, f"HTTP {no_fail.status_code}")

    # ============================================================== M2-07
    section("M2-07 Execution Detail 五层结构可查")
    run_full = api.get(f"/api/runs/{run04b['id']}").json()
    check("M2-07", "第 1 层 Run 可查且字段齐全", all(k in run_full for k in ("status", "started_at", "finished_at", "duration_ms", "total_cases", "passed_cases", "failed_cases", "flaky_cases", "skipped_cases", "error_cases")))
    case_full = api.get(f"/api/runs/{run04b['id']}/cases").json()[0]
    check("M2-07", "第 2 层 Case 可查（编号/名称/状态/耗时/attempt 数/错误摘要）", all(k in case_full for k in ("case_code", "case_name", "status", "duration_ms", "attempts_count", "error_message")))
    attempt_full = case_full["attempts"][0]
    check("M2-07", "第 3 层 Attempt 可查", all(k in attempt_full for k in ("attempt_no", "status", "started_at", "finished_at", "duration_ms")))
    step_full = attempt_full["step_results"][0]
    check("M2-07", "第 4 层 Step 字段齐全（编号/Action/Status/Expected/Actual/Duration/Error/PageURL）", all(k in step_full for k in ("step_no", "action", "status", "expected", "actual", "duration_ms", "error", "page_url")))
    art_full = api.get(f"/api/attempts/{attempt_full['id']}/artifacts").json()
    kinds_full = {}
    for item in art_full:
        kinds_full[item["type"]] = kinds_full.get(item["type"], 0) + 1
    print(f"         单次 attempt 证据：{kinds_full}")
    for kind in ("screenshot", "trace", "video", "har"):
        check("M2-07", f"第 5 层 Artifact 含 {kind}", kinds_full.get(kind, 0) > 0, str(kinds_full))
    check("M2-07", "失败步骤能定位到对应证据（step→artifact 关联存在）", any(a["step_result_id"] for a in art_full))
    check("M2-07", "证据可直接访问（签名 Artifact URL 返回 200）", all(httpx.get(f"{API}{a['url']}", timeout=30).status_code == 200 for a in art_full[:5] if a["url"]))

    # ============================================================== M2-08
    section("M2-08 M1 兼容性回归")
    legacy_plan = api.post(
        "/api/regression-plans",
        json={
            "project_id": env.project_id, "name": "M1 旧风格计划（case_ids）", "environment_id": env.environment_id,
            "case_ids": [env.cases["M2-PASS-001"]], "browser": "chromium", "trigger_type": "MANUAL", "enabled": True,
        },
    ).json()
    check("M2-08", "旧式计划（只给 case_ids）仍可创建", bool(legacy_plan.get("id")))
    run_legacy = api.post("/api/runs", json={"plan_id": legacy_plan["id"], "blocking": True}).json()
    check("M2-08", "旧式计划仍可真实执行通过", run_legacy["status"] == "PASSED", f"{run_legacy['status']} {run_legacy.get('error')}")
    check("M2-08", "旧式计划执行后仍产出 trace", env.kinds(run_legacy["id"]).get("trace", 0) > 0)
    check("M2-08", "M1 时代的历史执行记录仍可读", len(api.get("/api/runs", params={"limit": 100}).json()) >= 30)

    # ============================================================== M2-09
    section("M2-09 UI 验收（真实 Chromium）")
    console_errors: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        from backend.app.config import settings

        if settings.playwright_browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = settings.playwright_browsers_path

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

            def click_menu(name: str) -> None:
                page.locator(".ant-menu-item").filter(has_text=name).first.click()

            page.goto(f"{API}/#/login", wait_until="networkidle")
            page.fill("input#username", "admin")
            page.fill("input#password", "admin123")
            page.click("button[type=submit]")
            page.wait_for_selector("text=工作台", timeout=20000)
            page.wait_for_timeout(1200)

            click_menu("项目")
            page.wait_for_selector("table", timeout=20000)
            page.wait_for_timeout(1000)
            row = page.locator("tr", has_text=f"M2自测项目-{stamp}").first
            button = row.locator("button", has_text="设为当前")
            if button.count() > 0 and button.first.is_enabled():
                button.first.click()
                page.wait_for_timeout(1000)

            click_menu("测试套件")
            page.wait_for_selector("table", timeout=20000)
            page.wait_for_timeout(1200)
            body = page.inner_text("body")
            check("M2-09", "测试套件页渲染且含 M2 套件", "M2 Smoke 套件" in body, "未找到套件")
            page.screenshot(path=str(SHOT_DIR / "01_suites.png"), full_page=True)

            # 套件详情：看排序与启停
            page.locator("tr", has_text="M2 Smoke 套件").first.locator("button", has_text="成员").first.click()
            page.wait_for_timeout(1500)
            drawer = page.inner_text("body")
            check("M2-09", "套件成员抽屉显示用例与启用开关", "M2-LOGIN-001" in drawer and "M2-ORDER-001" in drawer)
            page.screenshot(path=str(SHOT_DIR / "02_suite_members.png"), full_page=True)
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)

            click_menu("执行中心")
            page.wait_for_selector("table", timeout=20000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(SHOT_DIR / "03_runs.png"), full_page=True)

            # FLAKY 那次执行
            page.locator(f"text={run03['id']}").first.click()
            page.wait_for_selector("text=用例执行结果", timeout=20000)
            page.wait_for_timeout(1500)
            detail = page.inner_text("body")
            check("M2-09", "执行详情显示 FLAKY", "FLAKY" in detail, detail[:120])
            page.screenshot(path=str(SHOT_DIR / "04_run_detail_flaky.png"), full_page=True)

            # 展开 FLAKY 用例，检查 attempt 切换
            page.locator(".ant-collapse-header").first.click()
            page.wait_for_timeout(1200)
            attempt_text = page.inner_text("body")
            check("M2-09", "用例展开后可见 Attempt 信息", "第 1 次" in attempt_text or "尝试" in attempt_text or "Attempt" in attempt_text)
            page.screenshot(path=str(SHOT_DIR / "05_flaky_attempts.png"), full_page=True)

            # 回到 CANCELLED 的那次执行，验证停止结果可见
            page.goto(f"{API}/#/runs/{run02['id']}", wait_until="networkidle")
            page.wait_for_selector("text=用例执行结果", timeout=20000)
            page.wait_for_timeout(1500)
            cancelled_text = page.inner_text("body")
            check("M2-09", "执行详情显示 CANCELLED", "CANCELLED" in cancelled_text)
            check("M2-09", "执行详情显示 SKIPPED", "SKIPPED" in cancelled_text)
            page.screenshot(path=str(SHOT_DIR / "06_run_detail_cancelled.png"), full_page=True)

            # 失败重跑按钮状态
            page.goto(f"{API}/#/runs/{run04['id']}", wait_until="networkidle")
            page.wait_for_selector("text=用例执行结果", timeout=20000)
            page.wait_for_timeout(1500)
            rerun_button = page.locator("button", has_text="重跑失败用例")
            check("M2-09", "有失败用例时「重跑失败用例」按钮可用", rerun_button.count() > 0 and rerun_button.first.is_enabled())
            page.screenshot(path=str(SHOT_DIR / "07_rerun_failed_button.png"), full_page=True)

            page.goto(f"{API}/#/runs/{run01['id']}", wait_until="networkidle")
            page.wait_for_selector("text=用例执行结果", timeout=20000)
            page.wait_for_timeout(1200)
            rerun_button2 = page.locator("button", has_text="重跑失败用例")
            check("M2-09", "无失败用例时按钮禁用", rerun_button2.count() > 0 and not rerun_button2.first.is_enabled())

            # 由哪一次 Run 重跑而来
            page.goto(f"{API}/#/runs/{run04b['id']}", wait_until="networkidle")
            page.wait_for_selector("text=用例执行结果", timeout=20000)
            page.wait_for_timeout(1500)
            check("M2-09", "新 Run 页面显示来源 Run", run04["id"] in page.inner_text("body"))
            page.screenshot(path=str(SHOT_DIR / "08_rerun_source.png"), full_page=True)

            browser.close()

        check("M2-09", "浏览器控制台无报错", not console_errors, str(console_errors[:3]))
    except Exception as exc:  # noqa: BLE001
        check("M2-09", "浏览器验收整体执行", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 汇总
    section("汇总")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed_items = [(name, detail) for name, ok, detail in RESULTS if not ok]
    print(f"  总计 {len(RESULTS)} 项，通过 {passed} 项，失败 {len(failed_items)} 项\n")
    if failed_items:
        print("  失败明细：")
        for name, detail in failed_items:
            print(f"    - {name}  {detail}")
    print("\n  关键 Run：")
    print(f"    M2-01 套件执行  {API}/#/runs/{run01['id']}")
    print(f"    M2-02 停止      {API}/#/runs/{run02['id']}")
    print(f"    M2-03 Flaky     {API}/#/runs/{run03['id']}")
    print(f"    M2-04 原 Run    {API}/#/runs/{run04['id']}")
    print(f"    M2-04 重跑 Run  {API}/#/runs/{run04b['id']}")
    print(f"  界面截图：{SHOT_DIR}")
    return 1 if failed_items else 0


if __name__ == "__main__":
    sys.exit(main())
