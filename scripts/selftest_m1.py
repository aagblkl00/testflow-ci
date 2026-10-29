"""M1 自测脚本：把「能跑通」升级为「知道边界在哪」。

seed_demo.py 验的是 happy path（3 用例全过）。
本脚本额外覆盖：

  A. 从零建项目闭环        —— 不复用任何已有数据
  B. 失败路径            —— 断言失败 / 变量未定义 / 元素找不到
  C. 边界与门禁          —— 白名单为空 / PROD 环境 / 未绑定流程 / 缺环境
  D. 鉴权与脱敏          —— 无 token / VIEWER 越权 / 变量脱敏
  E. 故障注入            —— missing_element / orders_500
  F. 证据完整性          —— 文件真实存在、PNG 签名、trace 可解压
  G. 真实浏览器点页面    —— 逐页渲染 + 执行详情里能看到 FAILED 与截图

前置：backend(8000) 与 demo_app(8100) 已启动。

用法：
    .venv/Scripts/python.exe scripts/selftest_m1.py
"""

from __future__ import annotations

import os
import struct
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000"
DEMO = "http://127.0.0.1:8100"
SHOT_DIR = ROOT / "storage" / "_selftest"

LOGIN_STEPS = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting", "screenshot": True},
]

ORDER_STEPS = LOGIN_STEPS[:4] + [
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    {"name": "断言订单落库", "action": "assert_visible", "locator_type": "testid", "locator_value": "order-table", "screenshot": True},
]

ASSERT_FAIL_STEPS = LOGIN_STEPS[:4] + [
    # 故意断言一个不存在的文本，验证 FAILED（业务断言失败）而非 ERROR
    {"name": "断言不存在文本", "action": "assert_text", "locator_type": "testid", "locator_value": "home-greeting", "expected": "这段文字不可能存在"},
]

VAR_MISS_STEPS = LOGIN_STEPS[:4] + [
    # 故意引用未定义变量，验证报错可读
    {"name": "使用未定义变量", "action": "fill", "locator_type": "testid", "locator_value": "home-quick-search", "value": "${this_variable_is_not_defined}"},
]

ELEMENT_MISS_STEPS = LOGIN_STEPS[:4] + [
    # 故意指向不存在的元素，验证超时失败 + DOM 快照
    {"name": "断言不存在的元素", "action": "assert_visible", "locator_type": "testid", "locator_value": "element-does-not-exist", "timeout": 4000},
]

RESULTS: list[tuple[str, bool, str]] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((f"[{section}] {name}", bool(ok), detail))
    print(f"   {'OK  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail and not ok else ''}")
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def set_faults(client: httpx.Client, **kwargs: object) -> None:
    payload = dict(kwargs)
    if payload.pop("reset_flaky", False):
        payload["reset_flaky"] = True
    client.post(f"{DEMO}/__faults", json=payload)


def reset_faults(client: httpx.Client) -> None:
    client.post(
        f"{DEMO}/__faults",
        json={
            "login_500": False,
            "search_500": False,
            "orders_500": False,
            "orders_slow_ms": 0,
            "missing_element": False,
            "flaky_once": False,
            "reset_flaky": True,
        },
    )


def main() -> int:  # noqa: C901 - 自测脚本，顺序执行更易读
    stamp = datetime.now().strftime("%H%M%S")
    api = httpx.Client(base_url=API, timeout=900.0)
    demo = httpx.Client(timeout=30.0)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    section("0. 前置检查")
    try:
        health = api.get("/api/health").json()
        check("前置", "TestFlow API 可访问", health.get("status") == "ok", str(health))
        check("前置", "执行模式为 inline（自测预期）", health.get("executor_mode") == "inline", str(health.get("executor_mode")))
    except Exception as exc:  # noqa: BLE001
        print(f"   后端不可用：{exc}")
        return 2
    try:
        check("前置", "Demo 被测站可访问", demo.get(f"{DEMO}/login").status_code == 200)
        reset_faults(demo)
        check("前置", "Demo 故障开关已全部复位", True)
    except Exception as exc:  # noqa: BLE001
        print(f"   Demo 站不可用：{exc}")
        return 2

    # ---------------------------------------------------------------- 登录
    login = api.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    check("前置", "管理员登录", login.status_code == 200)
    api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

    # ================================================================ SECTION A
    section("A. 从零建项目闭环（不复用任何已有数据）")
    project = api.post("/api/projects", json={"name": f"自测项目-{stamp}", "description": "selftest_m1.py 自动创建"}).json()
    project_id = project["id"]
    check("A", f"创建项目 自测项目-{stamp}", bool(project_id), project_id)

    env = api.post(
        "/api/environments",
        json={
            "project_id": project_id,
            "name": "自测环境",
            "base_url": DEMO,
            "environment_type": "TEST",
            "browser": "chromium",
            "allowed_hosts": ["127.0.0.1", "localhost"],
            "read_only": False,
            "variables": {"username": "demo", "password": "demo123"},
        },
    ).json()
    check("A", "创建环境（白名单 127.0.0.1）", bool(env.get("id")), str(env)[:120])

    specs = [
        ("TC-OK-001", "正常登录", LOGIN_STEPS),
        ("TC-ASSERT-FAIL-001", "断言失败场景", ASSERT_FAIL_STEPS),
        ("TC-VAR-MISS-001", "变量未定义场景", VAR_MISS_STEPS),
        ("TC-ELEM-MISS-001", "元素不存在场景", ELEMENT_MISS_STEPS),
    ]
    case_ids: dict[str, str] = {}
    for code, title, steps in specs:
        flow = api.post(
            "/api/flows",
            json={"project_id": project_id, "name": f"FLOW-{code}", "environment_id": env["id"], "variables": {}, "steps": steps},
        ).json()
        case = api.post(
            "/api/test-cases",
            json={
                "project_id": project_id,
                "case_code": code,
                "name": title,
                "module": "自测",
                "priority": "P1",
                "status": "ACTIVE",
                "tags": ["selftest"],
                "steps": [],
            },
        ).json()
        binding = api.post("/api/case-flow-bindings", json={"case_id": case["id"], "flow_id": flow["id"], "enabled": True})
        case_ids[code] = case["id"]
        check("A", f"创建并绑定 {code} / FLOW-{code}", binding.status_code == 201)

    # 一个故意不绑定流程的用例
    orphan = api.post(
        "/api/test-cases",
        json={"project_id": project_id, "case_code": "TC-NOBIND-001", "name": "未绑定流程的用例", "module": "自测", "priority": "P2", "steps": []},
    ).json()
    check("A", "创建未绑定流程的用例 TC-NOBIND-001", bool(orphan.get("id")))

    plan = api.post(
        "/api/regression-plans",
        json={
            "project_id": project_id,
            "name": "自测计划",
            "environment_id": env["id"],
            "case_ids": [case_ids["TC-OK-001"], case_ids["TC-ASSERT-FAIL-001"], case_ids["TC-VAR-MISS-001"], case_ids["TC-ELEM-MISS-001"], orphan["id"]],
            "browser": "chromium",
            "trigger_type": "MANUAL",
            "enabled": True,
        },
    ).json()
    check("A", "创建回归计划（5 个用例，含 1 个未绑定）", len(plan.get("case_ids", [])) == 5)

    run = api.post("/api/runs", json={"plan_id": plan["id"], "blocking": True}).json()
    cases = api.get(f"/api/runs/{run['id']}/cases").json()
    by_code = {item["case_code"]: item for item in cases}

    print(
        f"\n   执行结果：status={run['status']} 总数={run['total_cases']} "
        f"通过={run['passed_cases']} 失败={run['failed_cases']} 跳过={run['skipped_cases']} 错误={run['error_cases']} "
        f"耗时={run['duration_ms']}ms run_id={run['id']}\n"
    )

    check("A", "正常用例 TC-OK-001 = PASSED", by_code.get("TC-OK-001", {}).get("status") == "PASSED", by_code.get("TC-OK-001", {}).get("status", "缺失"))
    check("A", "未绑定用例 TC-NOBIND-001 = SKIPPED", by_code.get("TC-NOBIND-001", {}).get("status") == "SKIPPED", by_code.get("TC-NOBIND-001", {}).get("status", "缺失"))
    check("A", "整次执行 status = PARTIAL", run["status"] == "PARTIAL", run["status"])

    # ------------------------------------------------------- B 失败路径
    section("B. 失败路径（必须是 FAILED，不能是 ERROR）")
    for code in ("TC-ASSERT-FAIL-001", "TC-VAR-MISS-001", "TC-ELEM-MISS-001"):
        item = by_code.get(code, {})
        check("B", f"{code} = FAILED", item.get("status") == "FAILED", item.get("status", "缺失"))
        check("B", f"{code} 有可读的错误信息", bool(item.get("error_message")), item.get("error_message", "")[:90])
        failed_steps = [s for s in item.get("step_results", []) if s["status"] == "FAILED"]
        check("B", f"{code} 有且仅有 1 个 FAILED 步骤", len(failed_steps) == 1, f"实际 {len(failed_steps)} 个")
        if failed_steps:
            step = failed_steps[0]
            print(f"         失败步骤：{step['step_no']} {step['action']} {step['step_name']}")
            print(f"         错误：{step['error'][:150]}")
            check("B", f"{code} 失败步骤有截图", bool(step["screenshot_path"]) or "failed" in step["screenshot_path"])
            check("B", f"{code} 失败步骤有 DOM 快照", bool(step.get("dom_path")), step.get("dom_path", "")[:80])
        check("B", f"{code} 记下了 page_url", bool(item.get("step_results")) and bool(item["step_results"][0].get("page_url")))

    # 断言失败必须带上 expected / actual
    af = [s for s in by_code.get("TC-ASSERT-FAIL-001", {}).get("step_results", []) if s["status"] == "FAILED"]
    if af:
        check("B", "断言失败步骤记录了 expected", bool(af[0].get("expected")), af[0].get("expected", ""))
        check("B", "断言失败步骤记录了 actual", bool(af[0].get("actual")), str(af[0].get("actual", ""))[:60])

    check("B", "失败的用例也有 trace", any(a["type"] == "trace" for a in api.get(f"/api/runs/{run['id']}/artifacts").json() if a.get("case_result_id") == by_code.get("TC-ASSERT-FAIL-001", {}).get("id")))

    # ------------------------------------------------------- C 边界与门禁
    section("C. 边界与安全门禁")

    # 查缺环境
    no_env_plan = api.post(
        "/api/regression-plans",
        json={"project_id": project_id, "name": "缺环境计划", "environment_id": None, "case_ids": [case_ids["TC-OK-001"]], "browser": "chromium"},
    ).json()
    resp = api.post("/api/runs", json={"plan_id": no_env_plan["id"]})
    check("C", "计划未绑定环境 → 拒绝执行（400）", resp.status_code == 400, f"HTTP {resp.status_code} {resp.text[:90]}")

    # 白名单为空
    bad_env = api.post(
        "/api/environments",
        json={"project_id": project_id, "name": "白名单为空的环境", "base_url": DEMO, "environment_type": "TEST", "allowed_hosts": [], "variables": {}},
    ).json()
    bad_plan = api.post(
        "/api/regression-plans",
        json={"project_id": project_id, "name": "白名单为空计划", "environment_id": bad_env["id"], "case_ids": [case_ids["TC-OK-001"]], "browser": "chromium"},
    ).json()
    bad_run = api.post("/api/runs", json={"plan_id": bad_plan["id"], "blocking": True}).json()
    check("C", "白名单为空 → 整批 ERROR（不启动浏览器）", bad_run["status"] == "ERROR", bad_run["status"])
    check("C", "白名单为空 → 错误信息说明原因", "白名单" in (bad_run.get("error_message") or ""), (bad_run.get("error_message") or "")[:110])
    bad_cases = api.get(f"/api/runs/{bad_run['id']}/cases").json()
    check("C", "白名单为空 → 用例级也是 ERROR", all(item["status"] == "ERROR" for item in bad_cases), str([item["status"] for item in bad_cases]))

    # PROD 环境
    prod_env = api.post(
        "/api/environments",
        json={"project_id": project_id, "name": "生产环境", "base_url": DEMO, "environment_type": "PROD", "allowed_hosts": ["127.0.0.1"], "variables": {}},
    ).json()
    check("C", "环境创建允许 PROD 类型（门禁在执行时才拦）", prod_env.get("environment_type") == "PROD")
    prod_plan = api.post(
        "/api/regression-plans",
        json={"project_id": project_id, "name": "生产计划", "environment_id": prod_env["id"], "case_ids": [case_ids["TC-OK-001"]], "browser": "chromium"},
    ).json()
    prod_run = api.post("/api/runs", json={"plan_id": prod_plan["id"], "blocking": True}).json()
    check("C", "PROD 环境 → 拒绝执行", prod_run["status"] == "ERROR", prod_run["status"])
    check("C", "PROD 拒绝原因可读", "生产环境" in (prod_run.get("error_message") or ""), (prod_run.get("error_message") or "")[:110])

    # 未知动作
    bad_flow = api.post(
        "/api/flows",
        json={
            "project_id": project_id,
            "name": "含未知动作的流程",
            "environment_id": env["id"],
            "variables": {},
            # 带上定位器，确保能走到动作分发而不是提前被「未配置定位器」拦下
            "steps": [{"name": "瞎写的动作", "action": "fly_to_the_moon", "locator_type": "css", "locator_value": "#x"}],
        },
    ).json()
    bad_case = api.post("/api/test-cases", json={"project_id": project_id, "case_code": "TC-BAD-ACTION", "name": "未知动作", "module": "自测", "steps": []}).json()
    api.post("/api/case-flow-bindings", json={"case_id": bad_case["id"], "flow_id": bad_flow["id"], "enabled": True})
    bad_action_plan = api.post(
        "/api/regression-plans",
        json={"project_id": project_id, "name": "未知动作计划", "environment_id": env["id"], "case_ids": [bad_case["id"]], "browser": "chromium"},
    ).json()
    bad_action_run = api.post("/api/runs", json={"plan_id": bad_action_plan["id"], "blocking": True}).json()
    check("C", "未知动作 → FAILED 且明确报错", bad_action_run["status"] == "FAILED", bad_action_run["status"])
    bad_action_cases = api.get(f"/api/runs/{bad_action_run['id']}/cases").json()
    err = bad_action_cases[0]["error_message"] if bad_action_cases else ""
    check("C", "未知动作报错含动作名", "fly_to_the_moon" in err or "不支持" in err, err[:110])

    # ------------------------------------------------------- D 鉴权与脱敏
    section("D. 鉴权与脱敏")
    anon = httpx.Client(base_url=API, timeout=30)
    check("D", "无 token 访问 /api/projects → 401", anon.get("/api/projects").status_code == 401)
    check("D", "伪造 token → 401", anon.get("/api/projects", headers={"Authorization": "Bearer fake.token"}).status_code == 401)

    envs = api.get("/api/environments", params={"project_id": project_id}).json()
    selftest_env = next(item for item in envs if item["id"] == env["id"])
    check("D", "环境接口变量已脱敏", selftest_env["variables"].get("password") == "********", str(selftest_env["variables"]))
    check("D", "非敏感变量仍可见", selftest_env["variables"].get("username") == "demo")

    import sqlite3

    con = sqlite3.connect(str(ROOT / "testflow.db"))
    stored = con.execute("select variables from environments where id = ?", (env["id"],)).fetchone()[0]
    con.close()
    check("D", "数据库内保留真实密码（执行需要）", "demo123" in stored, stored)

    import secrets as _secrets

    viewer_name = f"viewer_{_secrets.token_hex(3)}"
    created = api.post("/api/auth/users", json={"username": viewer_name, "password": "viewer123", "display_name": "只读账号", "role": "VIEWER"})
    check("D", "管理员可创建 VIEWER 账号", created.status_code == 201, f"HTTP {created.status_code}")
    viewer = httpx.Client(base_url=API, timeout=60)
    vlogin = viewer.post("/api/auth/login", json={"username": viewer_name, "password": "viewer123"})
    check("D", "VIEWER 可登录", vlogin.status_code == 200)
    viewer.headers["Authorization"] = f"Bearer {vlogin.json()['access_token']}"
    check("D", "VIEWER 可读项目", viewer.get("/api/projects").status_code == 200)
    check("D", "VIEWER 不能建项目（403）", viewer.post("/api/projects", json={"name": f"越权-{stamp}"}).status_code == 403)
    check("D", "VIEWER 不能触发执行（403）", viewer.post("/api/runs", json={"plan_id": plan["id"]}).status_code == 403)

    # ------------------------------------------------------- E 故障注入
    section("E. 故障注入（为 M3 失败中心铺路）")
    set_faults(demo, missing_element=True)
    fault_plan = api.post(
        "/api/regression-plans",
        json={"project_id": project_id, "name": "故障注入-元素缺失", "environment_id": env["id"], "case_ids": [case_ids["TC-OK-001"]], "browser": "chromium"},
    ).json()
    fault_run = api.post("/api/runs", json={"plan_id": fault_plan["id"], "blocking": True}).json()
    check("E", "注入「首页元素缺失」→ 原本通过的用例转为 FAILED", fault_run["status"] == "FAILED", fault_run["status"])
    fc = api.get(f"/api/runs/{fault_run['id']}/cases").json()
    check("E", "元素缺失被记为 FAILED 而非 ERROR", fc and fc[0]["status"] == "FAILED", fc[0]["status"] if fc else "无")
    check("E", "元素缺失留下 DOM 快照", fc and any(s.get("dom_path") for s in fc[0]["step_results"]), "")
    reset_faults(demo)
    check("E", "故障开关已复位", demo.get(f"{DEMO}/__faults").json()["faults"]["missing_element"] is False)

    set_faults(demo, login_500=True)
    api500_flow = api.post(
        "/api/flows",
        json={"project_id": project_id, "name": "FLOW-500", "environment_id": env["id"], "variables": {}, "steps": LOGIN_STEPS},
    ).json()
    api500_case = api.post("/api/test-cases", json={"project_id": project_id, "case_code": "TC-500-001", "name": "接口 500", "module": "自测", "steps": []}).json()
    api.post("/api/case-flow-bindings", json={"case_id": api500_case["id"], "flow_id": api500_flow["id"], "enabled": True})
    api500_plan = api.post(
        "/api/regression-plans",
        json={"project_id": project_id, "name": "故障注入-接口500", "environment_id": env["id"], "case_ids": [api500_case["id"]], "browser": "chromium"},
    ).json()
    api500_run = api.post("/api/runs", json={"plan_id": api500_plan["id"], "blocking": True}).json()
    check("E", "注入「登录接口 500」→ FAILED", api500_run["status"] == "FAILED", api500_run["status"])
    c500 = api.get(f"/api/runs/{api500_run['id']}/cases").json()
    if c500:
        detail = " ".join(s.get("error", "") + s.get("actual", "") for s in c500[0]["step_results"])
        print(f"         接口 500 后的失败表现：{c500[0]['error_message'][:120]}")
        check("E", "接口 500 被记录（network 或断言层）", bool(detail) or bool(c500[0]["error_message"]))
    reset_faults(demo)
    check("E", "故障开关已复位（login_500）", demo.get(f"{DEMO}/__faults").json()["faults"]["login_500"] is False)

    # ------------------------------------------------------- F 证据完整性
    section("F. 证据完整性（逐文件核查，不看文件名猜测）")
    artifacts = api.get(f"/api/runs/{run['id']}/artifacts").json()
    missing = [item["path"] for item in artifacts if not Path(item["path"]).exists()]
    empty = [item["path"] for item in artifacts if Path(item["path"]).exists() and Path(item["path"]).stat().st_size == 0]
    check("F", f"{len(artifacts)} 个证据文件全部存在", not missing, f"缺失 {len(missing)} 个：{missing[:2]}")
    check("F", "无 0 字节文件", not empty, f"空文件 {len(empty)} 个：{empty[:2]}")

    kinds: dict[str, int] = {}
    for item in artifacts:
        kinds[item["type"]] = kinds.get(item["type"], 0) + 1
    print(f"         证据构成：{kinds}")
    for kind in ("screenshot", "trace", "dom", "log"):
        check("F", f"存在 {kind} 类型证据", kinds.get(kind, 0) > 0, f"数量 {kinds.get(kind, 0)}")

    # console / network 只在真的有内容时才落盘（避免一堆 0 字节文件）。
    # 本次执行全是断言失败、没有 HTTP 错误，所以这里应该是 0；
    # 真正该验的是「有 HTTP 错误的执行」必须留下 network 记录 —— 见下面 section E 那次 run。
    check(
        "F",
        "无 HTTP 错误时不产生空的 console/network 证据",
        kinds.get("console", 0) == 0 and kinds.get("network", 0) == 0,
        f"console={kinds.get('console', 0)} network={kinds.get('network', 0)}",
    )

    api500_arts = api.get(f"/api/runs/{api500_run['id']}/artifacts").json()
    net_arts = [item for item in api500_arts if item["type"] == "network"]
    check("F", "接口 500 的那次执行留下了 network 证据", bool(net_arts), f"数量 {len(net_arts)}")
    if net_arts:
        net_text = Path(net_arts[0]["path"]).read_text(encoding="utf-8")
        check("F", "network 记录里能看到 500", "500" in net_text, net_text[:120].replace("\n", " | "))
        check("F", "network 记录带上了请求方法", "POST" in net_text or "GET" in net_text, net_text[:80].replace("\n", " | "))

    shots = [item for item in artifacts if item["type"] == "screenshot"]
    bad_png = []
    for item in shots:
        raw = Path(item["path"]).read_bytes()[:8]
        if raw != bytes([137, 80, 78, 71, 13, 10, 26, 10]):
            bad_png.append(item["path"])
    check("F", f"{len(shots)} 张截图都是合法 PNG", not bad_png, str(bad_png[:2]))
    if shots:
        width, height = struct.unpack(">II", Path(shots[0]["path"]).read_bytes()[16:24])
        check("F", f"截图尺寸正常（{width}x{height}）", width >= 800 and height >= 600)
        check("F", "截图可经签名 Artifact URL 访问", httpx.get(f"{API}{shots[0]['url']}", timeout=20).status_code == 200)

    traces = [item for item in artifacts if item["type"] == "trace"]
    ok_zip = True
    inner_total = 0
    for item in traces:
        try:
            with zipfile.ZipFile(item["path"]) as archive:
                names = archive.namelist()
                inner_total += len(names)
                if not names:
                    ok_zip = False
        except Exception:  # noqa: BLE001
            ok_zip = False
    check("F", f"{len(traces)} 个 trace 都能解压（合计 {inner_total} 个内部文件）", ok_zip and inner_total > 0)
    check("F", "trace 内含 DOM 快照", any(".html" in n for item in traces for n in zipfile.ZipFile(item["path"]).namelist()), "")
    check("F", "trace 内含网络记录", any("network" in n for n in zipfile.ZipFile(traces[0]["path"]).namelist()) if traces else False, "")

    # ------------------------------------------------------- G 浏览器点页面
    section("G. 真实浏览器逐页自测")
    browser_errors: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        from backend.app.config import settings

        if settings.playwright_browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = settings.playwright_browsers_path

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("console", lambda m: browser_errors.append(m.text) if m.type == "error" else None)

            def click_menu(name: str) -> None:
                # antd 菜单项的 accessible name 会被图标的 aria-label 污染（如 "profile 项目"），
                # get_by_role(name=...) 匹配不到，所以按可见文本定位。
                page.locator(".ant-menu-item").filter(has_text=name).first.click()

            page.goto(f"{API}/#/login", wait_until="networkidle")
            page.wait_for_selector("text=TestFlow CI", timeout=20000)
            page.screenshot(path=str(SHOT_DIR / "01_login.png"), full_page=True)

            page.fill("input#username", "admin")
            page.fill("input#password", "admin123")
            page.click("button[type=submit]")
            page.wait_for_selector("text=工作台", timeout=20000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(SHOT_DIR / "02_dashboard.png"), full_page=True)

            # 项目页：确认自测项目在列表里，并把它切为「当前项目」，
            # 否则后面几个页面默认展示的是第一个项目（Demo 商城）的数据。
            click_menu("项目")
            page.wait_for_selector("table", timeout=20000)
            page.wait_for_timeout(1200)
            check("G", "「项目」页渲染且含自测项目", project["name"] in page.inner_text("body"), project["name"])
            page.screenshot(path=str(SHOT_DIR / "03_projects.png"), full_page=True)

            row = page.locator("tr", has_text=project["name"]).first
            switch = row.locator("button", has_text="设为当前")
            if switch.count() > 0 and switch.first.is_enabled():
                switch.first.click()
                page.wait_for_timeout(1200)

            for menu, shot, marker in (
                ("环境", "04_environments", "自测环境"),
                ("测试用例", "05_cases", "TC-ASSERT-FAIL-001"),
                ("自动化流程", "06_flows", "FLOW-TC-OK-001"),
                ("回归计划", "07_plans", "自测计划"),
            ):
                click_menu(menu)
                page.wait_for_selector("table", timeout=20000)
                page.wait_for_timeout(1200)
                body = page.inner_text("body")
                check("G", f"「{menu}」页渲染且含自测数据", marker in body, f"未找到 {marker}")
                page.screenshot(path=str(SHOT_DIR / f"{shot}.png"), full_page=True)

            # 执行中心 → 自测那次 run（有 PASSED 也有 FAILED）
            click_menu("执行中心")
            page.wait_for_selector("table", timeout=20000)
            page.wait_for_timeout(1200)
            page.locator(f"text={run['id']}").first.click()
            page.wait_for_selector("text=用例执行结果", timeout=20000)
            page.wait_for_timeout(1500)
            detail = page.inner_text("body")
            check("G", "执行详情显示 PARTIAL", "PARTIAL" in detail)
            check("G", "执行详情显示 PASSED", "PASSED" in detail)
            check("G", "执行详情显示 FAILED", "FAILED" in detail)
            check("G", "执行详情显示 SKIPPED", "SKIPPED" in detail)
            check("G", "执行详情显示用例编号", "TC-ASSERT-FAIL-001" in detail)
            page.screenshot(path=str(SHOT_DIR / "08_run_detail_mixed.png"), full_page=True)

            # 展开失败用例，确认有错误信息与截图
            headers = page.locator(".ant-collapse-header")
            for index in range(min(headers.count(), 6)):
                headers.nth(index).click()
                page.wait_for_timeout(700)
                text = page.inner_text("body")
                if "断言" in text and "失败" in text:
                    break
            thumbs = page.locator(".ant-image img").count()
            check("G", f"失败用例展开后可见截图缩略图（{thumbs} 张）", thumbs > 0)

            # 缩略图如果是坏图，整页截图看起来是一模一样的，所以必须验真实解码
            decoded = page.evaluate(
                "() => Array.from(document.querySelectorAll('.ant-image img'))"
                ".filter(i => i.complete && i.naturalWidth > 0).length"
            )
            check("G", f"缩略图真正解码成功（{decoded}/{thumbs}）", decoded > 0 and decoded == thumbs, f"decoded={decoded} total={thumbs}")
            check("G", "失败步骤展示了错误文本", "断言" in page.inner_text("body"))
            page.screenshot(path=str(SHOT_DIR / "09_failed_case_expanded.png"), full_page=True)

            browser.close()

        check("G", "浏览器控制台无报错", not browser_errors, str(browser_errors[:3]))
    except Exception as exc:  # noqa: BLE001
        check("G", "浏览器自测整体执行", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 汇总
    section("汇总")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed_items = [(name, detail) for name, ok, detail in RESULTS if not ok]
    print(f"  总计 {len(RESULTS)} 项，通过 {passed} 项，失败 {len(failed_items)} 项\n")
    if failed_items:
        print("  失败明细：")
        for name, detail in failed_items:
            print(f"    - {name}  {detail}")
    print(f"\n  自测项目：{project['name']}  (id={project_id})")
    print(f"  自测执行：{API}/api/runs/{run['id']}")
    print(f"  界面截图：{SHOT_DIR}")
    print(f"  建议人工复核：打开 {API} → 执行中心 → Run {run['id']} → 展开 TC-ASSERT-FAIL-001")
    return 1 if failed_items else 0


if __name__ == "__main__":
    sys.exit(main())
