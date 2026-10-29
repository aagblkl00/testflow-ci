"""M1 闭环验收脚本。

按方案 §6 的顺序真实走一遍，不做任何 mock：
    创建项目 → 创建环境 → 创建测试用例 → 创建自动化流程 → 绑定 →
    创建回归计划 → 触发执行 → Playwright 启动 Chromium → 打开 Demo 站 →
    执行测试步骤 → 落 CaseResult / StepResult / Artifact → 校验磁盘证据

前置：TestFlow API(8000) 与 Demo 站(8100) 均已启动。

用法：
    .venv/Scripts/python.exe scripts/seed_demo.py
    .venv/Scripts/python.exe scripts/seed_demo.py --base-url http://127.0.0.1:8000 --demo-url http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

LOGIN_FLOW_STEPS = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000, "screenshot": True},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting", "screenshot": True},
    {"name": "断言已跳转首页", "action": "assert_url", "expected": "/home"},
]

SEARCH_FLOW_STEPS = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting"},
    {"name": "打开搜索页", "action": "open", "value": "${base_url}/search?q=%E9%94%AE%E7%9B%98"},
    {"name": "断言有搜索结果", "action": "assert_visible", "locator_type": "testid", "locator_value": "search-results", "screenshot": True},
    {"name": "断言结果条数", "action": "assert_count", "locator_type": "testid", "locator_value": "search-row", "expected": "1"},
]

ORDER_FLOW_STEPS = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting"},
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "填写数量", "action": "fill", "locator_type": "testid", "locator_value": "order-quantity", "value": "2"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    {"name": "断言订单落库", "action": "assert_visible", "locator_type": "testid", "locator_value": "order-table", "screenshot": True},
    {"name": "断言订单号为 ORD 开头", "action": "assert_text", "locator_type": "testid", "locator_value": "order-no", "expected": "ORD"},
]


def step(index: int, title: str) -> None:
    print(f"\n[{index}] {title}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--demo-url", default="http://127.0.0.1:8100")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=600.0)

    step(0, "健康检查")
    health = client.get("/api/health")
    health.raise_for_status()
    print("   ", health.json())
    if health.json().get("executor_mode") != "inline":
        print("   注意：当前不是 inline 模式，脚本按 inline 逻辑断言")

    step(1, "登录 TestFlow")
    response = client.post("/api/auth/login", json={"username": args.username, "password": args.password})
    response.raise_for_status()
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    print(f"    登录成功，角色 {response.json()['user']['role']}")

    step(2, "创建项目")
    project = client.post(
        "/api/projects",
        json={"name": "Demo 商城", "description": "M1 闭环验收项目", "repository_url": ""},
    )
    if project.status_code == 409:
        project = client.get("/api/projects")
        project.raise_for_status()
        found = next(item for item in project.json() if item["name"] == "Demo 商城")
        print("    项目已存在，复用", found["id"])
    else:
        project.raise_for_status()
        found = project.json()
        print("    项目已创建", found["id"])
    project_id = found["id"]

    step(3, "创建环境（TEST / 白名单 127.0.0.1）")
    environments = client.get("/api/environments", params={"project_id": project_id}).json()
    environment = next((item for item in environments if item["name"] == "本地 Demo 测试环境"), None)
    if environment is None:
        response = client.post(
            "/api/environments",
            json={
                "project_id": project_id,
                "name": "本地 Demo 测试环境",
                "base_url": args.demo_url,
                "environment_type": "TEST",
                "browser": "chromium",
                "allowed_hosts": ["127.0.0.1", "localhost"],
                "read_only": False,
                "variables": {"username": "demo", "password": "demo123"},
            },
        )
        response.raise_for_status()
        environment = response.json()
    print("    环境", environment["id"], environment["base_url"], "type=", environment["environment_type"])
    print("    接口返回的变量（应已脱敏）:", environment["variables"])

    step(4, "创建自动化流程")
    flows = client.get("/api/flows", params={"project_id": project_id}).json()
    flow_map = {item["name"]: item for item in flows}

    cases_spec = [
        ("TC-LOGIN-001", "登录冒烟：账号密码登录并进入首页", "登录模块", "P0", "FLOW-LOGIN-001", LOGIN_FLOW_STEPS),
        ("TC-SEARCH-001", "搜索冒烟：关键词搜索返回结果", "搜索模块", "P1", "FLOW-SEARCH-001", SEARCH_FLOW_STEPS),
        ("TC-ORDER-001", "订单冒烟：创建订单并落库", "订单模块", "P1", "FLOW-ORDER-001", ORDER_FLOW_STEPS),
    ]

    case_ids: list[str] = []
    for case_code, case_name, module, priority, flow_name, steps in cases_spec:
        flow = flow_map.get(flow_name)
        if flow is None:
            response = client.post(
                "/api/flows",
                json={
                    "project_id": project_id,
                    "name": flow_name,
                    "engine": "playwright",
                    "environment_id": environment["id"],
                    "variables": {},
                    "steps": steps,
                },
            )
            response.raise_for_status()
            flow = response.json()
            flow_map[flow_name] = flow
            print(f"    流程已创建 {flow_name} steps={len(flow['definition']['steps'])}")
        else:
            print(f"    流程已存在 {flow_name} v{flow['version']}")

        existing = client.get("/api/test-cases", params={"project_id": project_id, "q": case_code}).json()
        case = next((item for item in existing if item["case_code"] == case_code), None)
        if case is None:
            response = client.post(
                "/api/test-cases",
                json={
                    "project_id": project_id,
                    "case_code": case_code,
                    "name": case_name,
                    "module": module,
                    "priority": priority,
                    "status": "ACTIVE",
                    "tags": ["smoke"],
                    "preconditions": "Demo 站可访问，账号 demo/demo123 可用",
                    "expected": "流程执行 PASSED",
                    "created_by": "seed_demo",
                    "steps": [],
                },
            )
            response.raise_for_status()
            case = response.json()
            print(f"    用例已创建 {case_code} -> {case['id']}")
        else:
            print(f"    用例已存在 {case_code} -> {case['id']}")

        bindings = client.get("/api/case-flow-bindings", params={"case_id": case["id"]}).json()
        if not any(item["flow_id"] == flow["id"] for item in bindings):
            response = client.post(
                "/api/case-flow-bindings",
                json={"case_id": case["id"], "flow_id": flow["id"], "enabled": True},
            )
            response.raise_for_status()
            print(f"    已绑定 {case_code} -> {flow_name}")
        case_ids.append(case["id"])

    step(5, "创建回归计划")
    plans = client.get("/api/regression-plans", params={"project_id": project_id}).json()
    plan = next((item for item in plans if item["name"] == "Smoke"), None)
    if plan is None:
        response = client.post(
            "/api/regression-plans",
            json={
                "project_id": project_id,
                "name": "Smoke",
                "description": "M1 最小可执行闭环",
                "environment_id": environment["id"],
                "case_ids": case_ids,
                "browser": "chromium",
                "trigger_type": "MANUAL",
                "retry_count": 0,
                "notification_enabled": False,
                "enabled": True,
            },
        )
        response.raise_for_status()
        plan = response.json()
    else:
        response = client.put(
            f"/api/regression-plans/{plan['id']}",
            json={
                "project_id": project_id,
                "name": "Smoke",
                "description": "M1 最小可执行闭环",
                "environment_id": environment["id"],
                "case_ids": case_ids,
                "browser": "chromium",
                "trigger_type": "MANUAL",
                "retry_count": 0,
                "notification_enabled": False,
                "enabled": True,
            },
        )
        response.raise_for_status()
        plan = response.json()
    print(f"    计划 {plan['name']} ({plan['id']}) 用例数 {len(plan['case_ids'])}")

    step(6, "触发执行（阻塞等待真实浏览器跑完）")
    response = client.post("/api/runs", json={"plan_id": plan["id"], "blocking": True}, timeout=900.0)
    response.raise_for_status()
    run = response.json()
    print(f"    run_id={run['id']} status={run['status']}")
    print(f"    总数={run['total_cases']} 通过={run['passed_cases']} 失败={run['failed_cases']} 跳过={run['skipped_cases']} 错误={run['error_cases']}")
    print(f"    耗时={run['duration_ms']} ms")

    step(7, "查看用例结果与步骤详情")
    cases = client.get(f"/api/runs/{run['id']}/cases").json()
    for item in cases:
        print(f"    {item['status']:8s} {item['case_code']:16s} {item['case_name']}  ({item['duration_ms']} ms)")
        for detail in item["step_results"]:
            flag = "" if detail["status"] == "PASSED" else f"  <<< {detail['error'][:110]}"
            print(f"        {detail['step_no']:02d} {detail['status']:8s} {detail['action']:16s} {detail['step_name']}{flag}")

    step(8, "校验证据文件落盘")
    artifacts = client.get(f"/api/runs/{run['id']}/artifacts").json()
    by_type: dict[str, int] = {}
    for item in artifacts:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        path = Path(item["path"])
        if not path.exists():
            print(f"    !! 缺失 {item['path']}")
    print(f"    证据总数 {len(artifacts)}：{by_type}")

    run_dir = ROOT / "storage" / "projects" / project_id / "runs" / run["id"]
    print(f"    证据目录 {run_dir}")
    if run_dir.exists():
        for path in sorted(run_dir.rglob("*")):
            if path.is_file():
                print(f"        {path.relative_to(run_dir).as_posix():60s} {path.stat().st_size:>9d} B")

    step(9, "结论")
    ok = (
        run["status"] in {"PASSED", "PARTIAL", "FAILED"}
        and run["total_cases"] == len(case_ids)
        and by_type.get("screenshot", 0) > 0
        and by_type.get("trace", 0) > 0
    )
    if run["failed_cases"] == 0 and run["error_cases"] == 0 and run["status"] == "PASSED":
        print("    [OK] 闭环真实跑通：浏览器已启动，步骤已执行，结果与证据已落库")
    elif ok:
        print(f"    [WARN] 闭环跑通，但存在失败：failed={run['failed_cases']} error={run['error_cases']}")
    else:
        print("    [FAIL] 闭环未跑通，请检查上面输出")
        return 1
    print(f"    页面查看：http://127.0.0.1:8000/api/runs/{run['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
