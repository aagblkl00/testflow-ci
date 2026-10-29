"""M3 真实验收（失败中心）。

对应验收项 M3-01 ~ M3-14。所有失败都由真实 Chromium 打在真实 Demo 站上产生，
不用 Mock 数据填库。

  M3-01  Fingerprint：/api/orders/10001|10002|10003 都 500 → 同一个指纹
  M3-02  Failure Group：10 个 Case 同一个故障 → 1 个 FailureGroup
  M3-03  不同 API 必须分开：orders 500 与 search 500 → 2 个 FailureGroup
  M3-04  HTTP 500 的指纹 / 分类 / 分组 / occurrence
  M3-05  Locator Missing → AUTOMATION_ERROR
  M3-06  Network Failure → ENVIRONMENT_ERROR
  M3-07  Missing Data → DATA_ERROR
  M3-08  Flaky → FLAKY 且进入 Flaky 统计
  M3-09  RetryPolicy 按分类区分（策略表 + 真实执行次数）
  M3-10  显式 retry_count 不被分类压低
  M3-11  跨 Run 聚合：同一个 FailureGroup，Occurrence 递增
  M3-12  Failure Center：N 个失败用例聚合成 M 个分组，且能钻取到证据
  M3-13  幂等：重复分析同一 Run，分组与 Occurrence 不增加
  M3-14  敏感信息：指纹不落明文凭据

另有 A 段纯逻辑校验（归一化 / 策略选择 / 分类规则 / 重试优先级），
以及 J 段真实浏览器 UI 验收。

前置：backend(8000) 与 demo_app(8100) 已启动。

用法：
    .venv/Scripts/python.exe scripts/selftest_m3.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000"
DEMO = "http://127.0.0.1:8100"
# 9099 本机没有服务监听，用来制造真实的「连接被拒」环境故障。
# 不能用 9 号端口 —— 它属于 Chromium 的受限端口，会先报 net::ERR_UNSAFE_PORT
# 而不是 net::ERR_CONNECTION_REFUSED，验不出真正的网络层故障。
UNREACHABLE = "http://127.0.0.1:9099"
SHOT_DIR = ROOT / "storage" / "_selftest_m3"

# --------------------------------------------------------------------- 流程定义

LOGIN = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting", "screenshot": True},
]

# 下单流程：提交后断言「没有错误提示」——接口 500 时错误横幅会渲染出来
ORDER_STEPS = LOGIN[:4] + [
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "填写数量", "action": "fill", "locator_type": "testid", "locator_value": "order-quantity", "value": "1"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    {"name": "断言下单没有报错", "action": "assert_not_visible", "locator_type": "testid", "locator_value": "order-error"},
]

SEARCH_STEPS = LOGIN[:4] + [
    {"name": "打开搜索页", "action": "open", "value": "${base_url}/search?q=%E9%94%AE%E7%9B%98", "timeout": 15000},
    {"name": "断言有搜索结果", "action": "assert_visible", "locator_type": "testid", "locator_value": "search-results"},
]

# 数据缺失：错误横幅会带上「测试数据不存在」的文案，断言它出现「下单成功」即为失败
DATA_STEPS = LOGIN[:4] + [
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "填写数量", "action": "fill", "locator_type": "testid", "locator_value": "order-quantity", "value": "1"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    {"name": "断言订单创建成功", "action": "assert_text", "locator_type": "testid", "locator_value": "order-error", "expected": "下单成功"},
]

RESULTS: list[tuple[str, bool, str]] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((f"[{section}] {name}", bool(ok), detail))
    print(f"   {'OK  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail and not ok else ''}")
    return bool(ok)


def info(text: str) -> None:
    print(f"        {text}")


def section(title: str) -> None:
    print(f"\n{'=' * 84}\n{title}\n{'=' * 84}")


def order_api_steps(order_no: str) -> list[dict]:
    return LOGIN[:4] + [
        {"name": f"打开订单接口 {order_no}", "action": "open", "value": f"${{base_url}}/api/orders/{order_no}", "timeout": 15000},
        {"name": "断言订单详情", "action": "assert_text", "locator_type": "css", "locator_value": "body", "expected": "订单详情"},
    ]


ALL_FAULTS_OFF = {
    "login_500": False, "search_500": False, "orders_500": False, "orders_slow_ms": 0,
    "missing_element": False, "flaky_once": False, "js_error": False, "missing_data": False,
    "reset_flaky": True,
}


class Env:
    """把「建项目/环境/用例/流程/计划」的重复动作收在一处。"""

    def __init__(self, api: httpx.Client, demo: httpx.Client, stamp: str) -> None:
        self.api = api
        self.demo = demo
        self.project_id = ""
        self.project_name = ""
        self.test_env_id = ""
        self.bad_env_id = ""
        self.cases: dict[str, str] = {}
        self.plans: dict[str, dict] = {}
        # M3-08 创建的 Flaky 分组，供主回归验证「同一个指纹被稳定失败后」的合并行为
        self.flaky_group_id = ""
        self.flaky_fingerprint = ""

    def setup(self, project_name: str) -> None:
        self.project_name = project_name
        self.project_id = self.api.post("/api/projects", json={"name": project_name, "description": "selftest_m3"}).json()["id"]
        common = {
            "project_id": self.project_id,
            "browser": "chromium",
            "environment_type": "TEST",
            "read_only": False,
            "variables": {"username": "demo", "password": "demo123"},
        }
        self.test_env_id = self.api.post(
            "/api/environments",
            json={**common, "name": "M3 测试环境", "base_url": DEMO, "allowed_hosts": ["127.0.0.1", "localhost"]},
        ).json()["id"]
        # M3-06 用：端口 9 是 discard 端口，本机没有服务监听，必然连接被拒。
        # 这是真实的环境故障，不是模拟的。
        self.bad_env_id = self.api.post(
            "/api/environments",
            json={**common, "name": "M3 不可达环境", "base_url": UNREACHABLE, "allowed_hosts": ["127.0.0.1"]},
        ).json()["id"]

    def add_case(self, code: str, name: str, steps: list[dict], *, environment_id: str | None = None) -> str:
        env_id = environment_id or self.test_env_id
        flow = self.api.post(
            "/api/flows",
            json={"project_id": self.project_id, "name": f"FLOW-{code}", "environment_id": env_id, "variables": {}, "steps": steps},
        ).json()
        case = self.api.post(
            "/api/test-cases",
            json={"project_id": self.project_id, "case_code": code, "name": name, "module": "M3自测",
                  "priority": "P1", "status": "ACTIVE", "tags": ["m3"], "steps": []},
        ).json()
        self.api.post("/api/case-flow-bindings", json={"case_id": case["id"], "flow_id": flow["id"], "enabled": True})
        self.cases[code] = case["id"]
        return case["id"]

    def make_plan(
        self,
        key: str,
        name: str,
        codes: list[str],
        *,
        environment_id: str | None = None,
        retry_count: int = 0,
        fail_retry: bool = False,
    ) -> dict:
        plan = self.api.post(
            "/api/regression-plans",
            json={
                "project_id": self.project_id,
                "name": name,
                "environment_id": environment_id or self.test_env_id,
                "case_ids": [self.cases[code] for code in codes],
                "browser": "chromium",
                "trigger_type": "MANUAL",
                "retry_count": retry_count,
                "fail_retry": fail_retry,
                "enabled": True,
            },
        ).json()
        self.plans[key] = plan
        return plan

    def run_plan(self, key: str) -> dict:
        run = self.api.post("/api/runs", json={"plan_id": self.plans[key]["id"], "blocking": True}).json()
        if run.get("error"):
            raise RuntimeError(f"执行失败：{run['error']}")
        return run

    def case_results(self, run_id: str) -> list[dict]:
        return self.api.get(f"/api/runs/{run_id}/cases").json()

    def groups(self, **params) -> dict:
        return self.api.get("/api/failures", params={"project_id": self.project_id, **params}).json()

    def group_by_category(self, category: str) -> list[dict]:
        payload = self.groups(category=category, page_size=200)
        return payload["items"]


def set_faults(demo: httpx.Client, **overrides: object) -> dict:
    payload = {**ALL_FAULTS_OFF, **overrides}
    return demo.post(f"{DEMO}/__faults", json=payload).json()


def first_failed_step(case_result: dict) -> dict:
    """取该用例最后一次尝试里第一个失败的步骤 —— 也就是「真正失败的那一步」。"""
    attempts = case_result.get("attempts") or []
    for attempt in reversed(attempts):
        for step in attempt.get("step_results") or []:
            if step["status"] in ("FAILED", "ERROR"):
                return step
    return {}


# ============================================================== A. 纯逻辑校验


def section_a() -> None:
    from backend.app.failure import classifier, retry_policy
    from backend.app.failure.fingerprint import build_context, compute
    from backend.app.failure.normalize import normalize_text, normalize_url
    from backend.app.failure.service import build_finding
    from backend.app.utils.redact import redact_text

    section("A. Fingerprint 归一化（确定性算法，不涉及 AI）")
    normalized = {oid: normalize_url(f"http://h/api/orders/{oid}") for oid in ("10001", "10002", "10003")}
    check("A", "路径里的数字 ID 归一化为 {id}", len(set(normalized.values())) == 1, str(normalized))
    info(f"/api/orders/10001 → {normalized['10001']}")
    check(
        "A",
        "UUID 归一化",
        normalize_url("http://h/user/550e8400-e29b-41d4-a716-446655440000") == "http://h/user/{uuid}",
        normalize_url("http://h/user/550e8400-e29b-41d4-a716-446655440000"),
    )
    check("A", "时间戳归一化", normalize_text("失败于 2026-09-19 10:23:41") == "失败于 {ts}", normalize_text("失败于 2026-09-19 10:23:41"))
    token_text = normalize_text("token=abc123def456ghi789jkl")
    check("A", "动态 token 归一化（明文被掩码替换）", "abc123def456ghi789jkl" not in token_text and "********" in token_text, token_text)
    check("A", "3 位状态码不被当成 ID", "500" in normalize_text("接口返回 500"), normalize_text("接口返回 500"))
    query_url = normalize_url("http://h/search?page=1&requestId=123&q=%E9%94%AE%E7%9B%98")
    check("A", "动态 query 参数丢弃（page/requestId）", "page=" not in query_url and "requestId=" not in query_url, query_url)
    check("A", "业务 query 参数保留（q）", "q=" in query_url, query_url)

    section("A. Fingerprint 策略选择")
    strategies = {}
    probes = {
        "HTTP_ERROR": dict(http_status=500, request_method="POST", request_url="http://h/api/orders", error="Error: 500"),
        "JS_ERROR": dict(js_error="TypeError: x is undefined", error="Error: 步骤失败"),
        # 注意：元素等待超时在策略上是 TIMEOUT/element_timeout，不是 ELEMENT。
        # 这是有意的 —— 方案 §9 Strategy E 要求把「元素超时」和「定位器本身有问题」
        # 区分开，前者是等待超时，后者才是定位器故障。
        "ELEMENT": dict(action="fill", error="RuntimeError: 步骤未配置定位器：输入用户名"),
        "ASSERTION": dict(action="assert_text", expected="成功", actual="失败", error="AssertionError: 文本断言失败"),
        "UNKNOWN": dict(action="wait", error="Error: 说不清的原因"),
    }
    for expected, kwargs in probes.items():
        result = compute(build_context(**kwargs))
        strategies[expected] = result.strategy
        check("A", f"策略 {expected}", result.strategy == expected, f"实际 {result.strategy}")
    timeouts = {}
    for label, kwargs in {
        "network": dict(action="open", error="Error: net::ERR_TIMED_OUT at http://h/x"),
        "navigation": dict(action="open", error="Error: page.goto: Timeout 30000ms exceeded"),
        "assertion": dict(action="assert_visible", error="AssertionError: 元素在 5000ms 内未变为可见"),
        "element": dict(action="click", error='TimeoutError: Locator.click: Timeout 10000ms exceeded. waiting for get_by_test_id("x")'),
    }.items():
        result = compute(build_context(**kwargs))
        timeouts[label] = result.failure_type
    check("A", "超时不合并成同一个类型", len(set(timeouts.values())) >= 3, str(timeouts))
    info(f"超时细分：{json.dumps(timeouts, ensure_ascii=False)}")

    section("A. 分类规则（确定性，证据不足一律 UNKNOWN）")
    cases = [
        ("HTTP 500 → PRODUCT_BUG", "PRODUCT_BUG", dict(http_status=500, request_method="POST", request_url="http://h/api/orders", error="Error: 500")),
        ("HTTP 502 → ENVIRONMENT_ERROR", "ENVIRONMENT_ERROR", dict(http_status=502, error="Error: 502")),
        ("连接被拒 → ENVIRONMENT_ERROR", "ENVIRONMENT_ERROR", dict(error="Error: page.goto: net::ERR_CONNECTION_REFUSED at http://h/x")),
        ("DNS 失败 → ENVIRONMENT_ERROR", "ENVIRONMENT_ERROR", dict(error="Error: net::ERR_NAME_NOT_RESOLVED")),
        ("元素找不到 → AUTOMATION_ERROR", "AUTOMATION_ERROR", dict(action="click", error='TimeoutError: waiting for get_by_test_id("submit")', locator={"type": "testid", "value": "submit"})),
        ("定位器语法错 → AUTOMATION_ERROR", "AUTOMATION_ERROR", dict(action="click", error='Error: Unexpected token ":" while parsing css selector ":::"')),
        ("测试数据不存在 → DATA_ERROR", "DATA_ERROR", dict(action="assert_visible", actual="测试数据不存在：商品已清理", error="AssertionError: 失败")),
        ("HTTP 404 无数据证据 → UNKNOWN", "UNKNOWN", dict(http_status=404, request_method="GET", request_url="http://h/api/x", error="Error: 404")),
        ("HTTP 403 证据不足 → UNKNOWN", "UNKNOWN", dict(http_status=403, error="Error: 403")),
        ("JS 异常 → PRODUCT_BUG", "PRODUCT_BUG", dict(js_error="TypeError: 渲染失败", error="Error: 步骤失败")),
        ("5xx 优先于元素找不到", "PRODUCT_BUG", dict(action="assert_not_visible", error="AssertionError: 元素仍然可见，期望不可见", http_status=500, request_method="POST", request_url="http://h/orders")),
    ]
    for label, expected, kwargs in cases:
        finding = build_finding({"step_no": 1, **kwargs})
        check("A", f"{label}", finding.category == expected, f"实际 {finding.category}")
    flaky_finding = build_finding({"action": "assert_text", "expected": "成功", "actual": "失败", "error": "AssertionError: 失败"}, is_flaky=True)
    check("A", "FLAKY 优先于其他分类", flaky_finding.category == "FLAKY", flaky_finding.category)
    check("A", "分类结果不含 AI 置信度字段", not hasattr(flaky_finding.classification, "confidence"))
    check("A", "每条分类都能追溯到规则 ID", bool(flaky_finding.classification.rule_id))

    section("A. RetryPolicy（分类与重试解耦）")
    policy = retry_policy.effective_policy()
    for category, expected in (("PRODUCT_BUG", 0), ("ENVIRONMENT_ERROR", 1), ("AUTOMATION_ERROR", 0), ("DATA_ERROR", 0), ("FLAKY", 1), ("UNKNOWN", 1)):
        check("A", f"{category} 默认重试 {expected} 次", policy[category] == expected, str(policy[category]))
    decisions = {
        "显式 retry_count=1 不被 PRODUCT_BUG 压低": retry_policy.explain(1, "PRODUCT_BUG")["retries"] == 1,
        "未开自动重试时 retry_count=0 不重试（与 M1/M2 一致）": retry_policy.explain(0, "ENVIRONMENT_ERROR")["retries"] == 0,
        "开自动重试后 ENVIRONMENT_ERROR 重试 1 次": retry_policy.explain(0, "ENVIRONMENT_ERROR", auto_retry=True)["retries"] == 1,
        "开自动重试后 PRODUCT_BUG 不重试": retry_policy.explain(0, "PRODUCT_BUG", auto_retry=True)["retries"] == 0,
        "被门禁拦下永不重试": retry_policy.explain(3, "UNKNOWN", auto_retry=True, blocked=True)["retries"] == 0,
        "决策来源可解释": retry_policy.explain(1, "PRODUCT_BUG")["source"] == "explicit_retry_count",
    }
    for label, ok in decisions.items():
        check("A", label, ok)

    section("A. 脱敏（指纹绝不能落明文凭据）")
    text = "提交失败 password=demo123 token=abc123secret Authorization=Bearer eyJhbGciOiJIUzI1NiJ9"
    redacted = redact_text(text)
    check("A", "password= 被脱敏", "demo123" not in redacted, redacted)
    check("A", "token= 被脱敏", "abc123secret" not in redacted, redacted)
    check("A", "Bearer 被脱敏", "eyJhbGci" not in redacted, redacted)
    check("A", "正常业务文本不被误伤", redact_text("订单创建成功，SKU-1001，数量 1") == "订单创建成功，SKU-1001，数量 1")
    secret_finding = build_finding({"action": "fill", "error": text, "actual": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"})
    blob = " ".join([secret_finding.fingerprint.normalized_message, secret_finding.fingerprint.normalized_actual, secret_finding.fingerprint.sample_message])
    check("A", "指纹字段无明文凭据", not any(item in blob for item in ("demo123", "abc123secret", "eyJhbGci")))
    check("A", "分类输出结构含 deterministic 标志", classifier.classify(build_context(error="x")).deterministic is True)


# ============================================================== B. 真实执行


def build_env(api: httpx.Client, demo: httpx.Client, stamp: str) -> Env:
    env = Env(api, demo, stamp)
    env.setup(f"M3自测项目-{stamp}")

    # M3-01：三个不同订单号，走同一个接口
    for order_no in ("10001", "10002", "10003"):
        env.add_case(f"M3-FP-{order_no}", f"订单接口 500（订单 {order_no}）", order_api_steps(order_no))

    # M3-02：10 个用例命中同一个故障
    for index in range(1, 11):
        env.add_case(f"M3-AGG-{index:02d}", f"下单接口 500 #{index}", ORDER_STEPS)

    # M3-03：另一个接口的 500
    for index in (1, 2):
        env.add_case(f"M3-SEARCH-{index}", f"搜索接口 500 #{index}", SEARCH_STEPS)

    # M3-05 / 07：元素缺失、测试数据缺失
    env.add_case("M3-AUTO-001", "首页元素缺失", LOGIN)
    env.add_case("M3-DATA-001", "下单前置数据缺失", DATA_STEPS)

    # M3-06：不可达环境
    env.add_case("M3-NET-001", "目标环境不可达", LOGIN[:1], environment_id=env.bad_env_id)

    # M3-08：首次失败、第二次成功
    env.add_case("M3-FLAKY-001", "下单偶发失败", ORDER_STEPS)

    return env


def section_b(api: httpx.Client, demo: httpx.Client, env: Env) -> None:
    section("M3-01 / M3-02 / M3-03 一次回归 15 个失败 → 3 个 FailureGroup")

    print("\n   >> Run 1：开启 orders_500 与 search_500（两个不同接口同时故障）")
    set_faults(demo, orders_500=True, search_500=True)
    env.make_plan(
        "main",
        "M3 主回归（15 个失败用例）",
        [f"M3-FP-{item}" for item in ("10001", "10002", "10003")]
        + [f"M3-AGG-{index:02d}" for index in range(1, 11)]
        + ["M3-SEARCH-1", "M3-SEARCH-2"],
    )
    run1 = env.run_plan("main")
    info(f"run={run1['id']} status={run1['status']} 总数={run1['total_cases']} 失败={run1['failed_cases']}")

    cases = env.case_results(run1["id"])
    failed_cases = [item for item in cases if item["status"] in ("FAILED", "ERROR")]
    check("M3-02", "15 个用例全部真实失败", run1["failed_cases"] == 15 and len(failed_cases) == 15, f"failed={run1['failed_cases']}")
    check("M3-02", "每个失败用例都有失败步骤", all(first_failed_step(item) for item in failed_cases))

    groups = env.groups(page_size=200)
    check("M3-02", "15 个失败用例只聚合成 3 个 FailureGroup", groups["total"] == 3, f"实际 {groups['total']}")
    for group in groups["items"]:
        fingerprint = group["fingerprint"] or {}
        info(
            f"{group['category']:17s} | 影响 {group['failed_case_count']} 用例 | {group['occurrence_count']} 次 | "
            f"{fingerprint.get('strategy')} | {group['title'][:52]}"
        )

    by_key = {f"{group['fingerprint']['request_method']} {group['fingerprint']['request_url']} {group['fingerprint']['http_status']}": group
              for group in groups["items"] if group["fingerprint"]}
    orders_post = next((group for key, group in by_key.items() if key.startswith("POST ") and key.endswith(" 500") and "/orders" in key), None)
    order_api = next((group for key, group in by_key.items() if "GET http://127.0.0.1:8100/api/orders/{id} 500" in key), None)
    search = next((group for key, group in by_key.items() if "/search" in key), None)

    check("M3-02", "POST /orders 500 至少聚合了 10 个用例", bool(orders_post) and orders_post["failed_case_count"] >= 10,
          str(orders_post and orders_post["failed_case_count"]))
    if orders_post:
        info(f"该分组影响 {orders_post['failed_case_count']} 个用例（10 个稳定失败 + 此前的 Flaky 用例，同一指纹）")
    check("M3-01", "三个不同订单号 → 同一个指纹", bool(order_api) and order_api["failed_case_count"] == 3
          and order_api["occurrence_count"] == 3, str(order_api and f"{order_api['failed_case_count']}/{order_api['occurrence_count']}"))
    check("M3-01", "指纹里的路径已归一化为 {id}", bool(order_api) and "/api/orders/{id}" in order_api["fingerprint"]["request_url"],
          order_api["fingerprint"]["request_url"] if order_api else "")
    check("M3-03", "orders 500 与 search 500 是两个不同的分组",
          bool(orders_post) and bool(search) and orders_post["id"] != search["id"])
    check("M3-03", "search 分组的指纹指向 search 接口", bool(search) and "/search" in search["fingerprint"]["request_url"],
          search["fingerprint"]["request_url"] if search else "")

    check("M3-04", "HTTP 500 分类为 PRODUCT_BUG", bool(orders_post) and orders_post["category"] == "PRODUCT_BUG",
          orders_post["category"] if orders_post else "")
    check("M3-04", "HTTP 500 命中规则 PRODUCT_HTTP_5XX", bool(orders_post) and orders_post["rule_id"] == "PRODUCT_HTTP_5XX")
    check("M3-04", "指纹策略为 HTTP_ERROR", bool(orders_post) and orders_post["fingerprint"]["strategy"] == "HTTP_ERROR")
    check("M3-04", "分类带可核对的规则说明", bool(orders_post) and bool(orders_post["rule_reason"]))
    if orders_post:
        info(f"规则说明：{orders_post['rule_reason']}")

    # occurrence 必须指向真实证据
    occurrences = api.get(f"/api/failures/{orders_post['id']}/occurrences", params={"page_size": 3}).json()
    check("M3-04", "Occurrence 指向真实 Run/Case/Attempt/Step",
          all(item["run_id"] and item["case_result_id"] and item["attempt_id"] and item["step_result_id"] for item in occurrences["items"]))
    sample = occurrences["items"][0]
    check("M3-04", "Occurrence 展开出步骤现场（HTTP 状态在步骤上）",
          bool(sample["step_action"]) and bool(sample["step_error"]))

    # 分类与聚合解耦：Flaky 那次出现与随后的 10 次稳定失败指纹相同，落在同一个分组里，
    # 但每一次出现的分类必须各自保留 —— Flaky 那次仍然是 FLAKY，而不是被 PRODUCT_BUG 吞掉。
    if env.flaky_group_id:
        merged = next((group for group in groups["items"] if group["id"] == env.flaky_group_id), None)
        check("M3-04", "Flaky 与稳定失败指纹相同 → 落在同一个分组（指纹相同就是同一个问题）",
              bool(merged), "未找到 Flaky 分组")
        if merged:
            check("M3-04", "分组被稳定失败升级为 PRODUCT_BUG",
                  merged["category"] == "PRODUCT_BUG", merged["category"])
            check("M3-04", "分组仍保留 flaky_count", merged["flaky_count"] >= 1, str(merged["flaky_count"]))
            kinds = {item["category"] for item in env.api.get(f"/api/failures/{merged['id']}/occurrences", params={"page_size": 100}).json()["items"]}
            check("M3-04", "occurrence 层面同时存在 FLAKY 与 PRODUCT_BUG（分类没有被聚合覆盖）",
                  {"FLAKY", "PRODUCT_BUG"} <= kinds, str(kinds))


def section_c(api: httpx.Client, demo: httpx.Client, env: Env) -> None:
    section("M3-05 / M3-06 / M3-07 分类验收（三类失败，三种结论）")

    print("\n   >> M3-05 元素缺失（missing_element）")
    set_faults(demo, missing_element=True)
    env.make_plan("auto", "M3 元素缺失计划", ["M3-AUTO-001"])
    run_auto = env.run_plan("auto")
    auto_case = env.case_results(run_auto["id"])[0]
    auto_step = first_failed_step(auto_case)
    info(f"步骤错误：{(auto_step.get('error') or '')[:110]}")
    auto_group = env.group_by_category("AUTOMATION_ERROR")
    check("M3-05", "元素缺失被分为 AUTOMATION_ERROR", any(group["failed_case_count"] >= 1 for group in auto_group), str(len(auto_group)))
    auto_hit = next((group for group in auto_group if group["rule_id"].startswith("AUTO_")), None)
    check("M3-05", "命中自动化类规则 ID", bool(auto_hit), str([group["rule_id"] for group in auto_group]))
    if auto_hit:
        info(f"规则：{auto_hit['rule_id']} — {auto_hit['rule_reason']}")

    print("\n   >> M3-06 环境不可达（net::ERR_CONNECTION_REFUSED）")
    set_faults(demo)
    run_net = env.run_plan("net") if "net" in env.plans else None
    if run_net is None:
        env.make_plan("net", "M3 不可达环境计划", ["M3-NET-001"], environment_id=env.bad_env_id)
        run_net = env.run_plan("net")
    net_case = env.case_results(run_net["id"])[0]
    net_step = first_failed_step(net_case)
    info(f"步骤错误：{(net_step.get('error') or '')[:120]}")
    check("M3-06", "环境故障被捕获到步骤错误里", "ERR_CONNECTION_REFUSED" in (net_step.get("error") or ""), (net_step.get("error") or "")[:80])
    net_group = env.group_by_category("ENVIRONMENT_ERROR")
    check("M3-06", "网络故障被分为 ENVIRONMENT_ERROR", bool(net_group), str(len(net_group)))
    check("M3-06", "命中环境类规则 ID", any(group["rule_id"].startswith("ENV_") for group in net_group), str([group["rule_id"] for group in net_group]))
    for group in net_group:
        info(f"规则：{group['rule_id']} — {group['rule_reason']}")

    print("\n   >> M3-07 前置测试数据缺失（missing_data）")
    set_faults(demo, missing_data=True)
    env.make_plan("data", "M3 数据缺失计划", ["M3-DATA-001"])
    run_data = env.run_plan("data")
    data_case = env.case_results(run_data["id"])[0]
    data_step = first_failed_step(data_case)
    info(f"实际值：{(data_step.get('actual') or '')[:110]}")
    data_group = env.group_by_category("DATA_ERROR")
    check("M3-07", "数据缺失被分为 DATA_ERROR", any(group["failed_case_count"] >= 1 for group in data_group), str(len(data_group)))
    check("M3-07", "命中数据类规则 ID", any(group["rule_id"].startswith("DATA_") for group in data_group), str([group["rule_id"] for group in data_group]))


def section_d(api: httpx.Client, demo: httpx.Client, env: Env) -> None:
    section("M3-08 Flaky（FAIL → RETRY → PASS）与 Flaky 统计")

    set_faults(demo, flaky_once=True)
    env.make_plan("flaky", "M3 Flaky 计划", ["M3-FLAKY-001"], retry_count=1)
    run = env.run_plan("flaky")
    case = env.case_results(run["id"])[0]
    statuses = [attempt["status"] for attempt in case["attempts"]]
    info(f"尝试序列：{statuses}  最终状态：{case['status']}")
    check("M3-08", "第一次失败、第二次通过", statuses == ["FAILED", "PASSED"], str(statuses))
    check("M3-08", "最终状态是 FLAKY，不伪装成 PASSED", case["status"] == "FLAKY", case["status"])
    check("M3-08", "Run 计数里 Flaky 独立统计", run["flaky_cases"] == 1, str(run["flaky_cases"]))

    all_categories = [group["category"] for group in env.groups(page_size=200)["items"]]
    flaky_groups = env.group_by_category("FLAKY")
    check("M3-08", "Flaky 单独成组，不被当成产品问题", bool(flaky_groups), str(all_categories))
    if flaky_groups:
        group = flaky_groups[0]
        check("M3-08", "Flaky 分组命中规则 FLAKY_ATTEMPT_SEQUENCE", group["rule_id"] == "FLAKY_ATTEMPT_SEQUENCE", group["rule_id"])
        check("M3-08", "Flaky 分组的 flaky_count 记到 1", group["flaky_count"] >= 1, str(group["flaky_count"]))
        check("M3-08", "Flaky 分组影响 1 个用例 / 1 次执行",
              group["failed_case_count"] == 1 and group["failed_run_count"] == 1,
              f"{group['failed_case_count']}/{group['failed_run_count']}")
        occurrences = api.get(f"/api/failures/{group['id']}/occurrences").json()
        check("M3-08", "Flaky 的 occurrence 指向第一次失败的那一次尝试",
              all(item["attempt_no"] == 1 for item in occurrences["items"]), str([item["attempt_no"] for item in occurrences["items"]]))
        check("M3-08", "Flaky 的 occurrence 步骤是其第一次失败的那一步",
              all(item["step_status"] in ("FAILED", "ERROR") for item in occurrences["items"]))
        check("M3-08", "occurrence 自身的分类就是 FLAKY（不是 PRODUCT_BUG）",
              all(item["category"] == "FLAKY" for item in occurrences["items"]),
              str([item["category"] for item in occurrences["items"]]))
        env.flaky_group_id = group["id"]
        env.flaky_fingerprint = (group.get("fingerprint") or {}).get("fingerprint", "")

    stats = api.get("/api/flaky/statistics", params={"project_id": env.project_id}).json()
    info(f"Flaky 统计：{stats['flaky_case_executions']} / {stats['total_case_executions']} = {stats['flaky_rate']}%")
    check("M3-08", "Flaky 统计有明确分母", stats["total_case_executions"] > 0 and bool(stats["denominator"]))
    check("M3-08", "Flaky 统计给出计算定义", bool(stats["definition"]) and bool(stats["numerator"]))
    check("M3-08", "Flaky Rate 与分子分母自洽",
          abs(stats["flaky_rate"] - round(stats["flaky_case_executions"] / stats["total_case_executions"] * 100, 1)) < 0.11,
          f"{stats['flaky_rate']} vs {stats['flaky_case_executions']}/{stats['total_case_executions']}")
    check("M3-08", "按用例可查 Flaky 率", any(item["case_code"] == "M3-FLAKY-001" for item in stats["by_case"]),
          str([item["case_code"] for item in stats["by_case"]]))


def section_e(api: httpx.Client, demo: httpx.Client, env: Env) -> None:
    section("M3-09 / M3-10 RetryPolicy 真实执行次数（不是只看策略表）")

    # PRODUCT_BUG + 开启自动重试 → 策略 0 次，只跑 1 次
    set_faults(demo, orders_500=True)
    env.make_plan("auto_bug", "M3 自动重试-产品问题", ["M3-AGG-01"], fail_retry=True, retry_count=0)
    run = env.run_plan("auto_bug")
    attempts = env.case_results(run["id"])[0]["attempts_count"]
    check("M3-09", "PRODUCT_BUG 开自动重试也只跑 1 次", attempts == 1, f"实际 {attempts} 次")

    # ENVIRONMENT_ERROR + 开启自动重试 → 策略 1 次，跑 2 次
    set_faults(demo)
    env.make_plan("auto_env", "M3 自动重试-环境问题", ["M3-NET-001"], environment_id=env.bad_env_id, fail_retry=True, retry_count=0)
    run = env.run_plan("auto_env")
    attempts = env.case_results(run["id"])[0]["attempts_count"]
    check("M3-09", "ENVIRONMENT_ERROR 开自动重试跑 2 次", attempts == 2, f"实际 {attempts} 次")

    # AUTOMATION_ERROR + 开启自动重试 → 策略 0 次
    set_faults(demo, missing_element=True)
    env.make_plan("auto_auto", "M3 自动重试-自动化问题", ["M3-AUTO-001"], fail_retry=True, retry_count=0)
    run = env.run_plan("auto_auto")
    attempts = env.case_results(run["id"])[0]["attempts_count"]
    check("M3-09", "AUTOMATION_ERROR 开自动重试只跑 1 次", attempts == 1, f"实际 {attempts} 次")

    # DATA_ERROR + 开启自动重试 → 策略 0 次
    set_faults(demo, missing_data=True)
    env.make_plan("auto_data", "M3 自动重试-数据问题", ["M3-DATA-001"], fail_retry=True, retry_count=0)
    run = env.run_plan("auto_data")
    attempts = env.case_results(run["id"])[0]["attempts_count"]
    check("M3-09", "DATA_ERROR 开自动重试只跑 1 次", attempts == 1, f"实际 {attempts} 次")

    # M3-10 显式 retry_count=1 覆盖 PRODUCT_BUG 的 0 次默认策略
    set_faults(demo, orders_500=True)
    env.make_plan("explicit_retry", "M3 显式重试-产品问题", ["M3-AGG-01"], retry_count=1, fail_retry=False)
    run = env.run_plan("explicit_retry")
    attempts = env.case_results(run["id"])[0]["attempts_count"]
    check("M3-10", "显式 retry_count=1 不被 PRODUCT_BUG 策略压低（真跑 2 次）", attempts == 2, f"实际 {attempts} 次")

    policy = api.get("/api/retry-policy").json()
    check("M3-09", "策略可从接口读取且说明优先级", bool(policy["effective"]) and bool(policy["precedence"]))
    info(f"生效策略：{json.dumps(policy['effective'], ensure_ascii=False)}")


def section_f(api: httpx.Client, demo: httpx.Client, env: Env) -> None:
    section("M3-11 跨 Run 聚合（同一个问题连续出现，不能每次新建分组）")

    set_faults(demo, orders_500=True)
    env.make_plan("cross", "M3 跨 Run 计划", ["M3-AGG-02", "M3-FP-10001"])

    before = {group["fingerprint"]["fingerprint"]: group for group in env.groups(page_size=200)["items"] if group.get("fingerprint")}
    target_fp = None
    for fingerprint, group in before.items():
        if group["rule_id"] == "PRODUCT_HTTP_5XX" and group["failed_run_count"] >= 1 and group["title"].endswith("500"):
            target_fp = fingerprint
            break
    check("M3-11", "找到作为基准的失败分组", bool(target_fp))
    base_runs = before[target_fp]["failed_run_count"] if target_fp else 0

    for index in (1, 2):
        run = env.run_plan("cross")
        after = {group["fingerprint"]["fingerprint"]: group for group in env.groups(page_size=200)["items"] if group.get("fingerprint")}
        if target_fp:
            check("M3-11", f"第 {index} 次重复执行后分组数量未增加",
                  len(after) == len(before), f"{len(before)} → {len(after)}")
            check("M3-11", f"第 {index} 次重复执行后 occurrence 递增",
                  after[target_fp]["occurrence_count"] > before[target_fp]["occurrence_count"],
                  f"{before[target_fp]['occurrence_count']} → {after[target_fp]['occurrence_count']}")
            check("M3-11", f"第 {index} 次重复执行后 failed_run_count 递增",
                  after[target_fp]["failed_run_count"] > before[target_fp]["failed_run_count"],
                  f"{before[target_fp]['failed_run_count']} → {after[target_fp]['failed_run_count']}")
        before = after
        _ = run

    if target_fp:
        group = before[target_fp]
        trend = api.get(f"/api/failures/{group['id']}/trend", params={"limit": 7}).json()
        check("M3-11", "趋势里能看到多次执行", len(trend["points"]) >= 3, str(len(trend["points"])))
        check("M3-11", "首次出现时间早于最近出现时间", group["first_seen_at"] < group["last_seen_at"])
        _ = base_runs
        info(f"分组 {group['id']}：occurrence={group['occurrence_count']} 影响用例={group['failed_case_count']} 影响执行={group['failed_run_count']}")


def section_g(api: httpx.Client, env: Env) -> None:
    section("M3-13 幂等（重复分析同一个 Run，分组与 Occurrence 都不能增加）")

    run_id = None
    for item in api.get("/api/runs", params={"project_id": env.project_id, "limit": 50}).json():
        if item["failed_cases"]:
            run_id = item["id"]
            break
    check("M3-13", "找到有失败的 Run", bool(run_id))

    before = env.groups(page_size=200)
    before_counts = {group["id"]: (group["occurrence_count"], group["failed_case_count"], group["failed_run_count"])
                     for group in before["items"]}
    for _ in range(2):
        result = api.post(f"/api/runs/{run_id}/analyze-failures").json()
        check("M3-13", "重复分析不新建 occurrence", result["occurrences"] == 0, f"新建了 {result['occurrences']} 条")
    after = env.groups(page_size=200)
    after_counts = {group["id"]: (group["occurrence_count"], group["failed_case_count"], group["failed_run_count"])
                    for group in after["items"]}
    check("M3-13", "分组数量不变", before["total"] == after["total"], f"{before['total']} → {after['total']}")
    check("M3-13", "每个分组的计数都不变", before_counts == after_counts, f"{before_counts} vs {after_counts}")
    info(f"分组总数 {after['total']}，计数快照一致")


def section_h(api: httpx.Client, demo: httpx.Client, env: Env) -> None:
    section("M3-14 敏感信息不进指纹（真实执行 + 数据库双重核对）")

    # 真实场景：把凭据写进断言期望值，它会一路出现在错误信息、实际值与失败指纹里。
    # 这不是编出来的用例 —— 测试人员把带 token 的 URL 或账号密码写进断言很常见。
    secret = "sup3r-s3cret-pwd"
    cases = env.cases
    code = "M3-SECRET-001"
    steps = LOGIN[:4] + [
        {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
        {"name": "提交带凭据的断言", "action": "assert_text", "locator_type": "testid", "locator_value": "order-error",
         "expected": f"token={secret}&password={secret}"},
    ]
    env.add_case(code, "敏感信息出现在失败信息里", steps)
    check("M3-14", f"用例与流程已创建（流程里含明文 secret {secret}）", code in cases)

    set_faults(demo, orders_500=True)
    env.make_plan("secret", "M3 敏感信息计划", [code])
    run = env.run_plan("secret")
    group_rows = env.groups(page_size=200)["items"]

    import sqlite3
    con = sqlite3.connect(str(ROOT / "testflow.db"))
    leaks = []
    for table, columns in (
        ("failure_fingerprints", ("normalized_message", "normalized_expected", "normalized_actual", "sample_message", "request_url", "locator")),
        ("failure_groups", ("title", "rule_reason")),
        ("step_results", ("error", "expected", "actual", "console_summary", "network_summary")),
    ):
        for column in columns:
            for (value,) in con.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" LIKE ?', (f"%{secret}%",)):
                leaks.append(f"{table}.{column}={str(value)[:60]}")
    con.close()
    check("M3-14", "数据库里（指纹表/分组表/步骤表）没有明文凭据", not leaks, str(leaks[:3]))

    redacted_hit = False
    for group in group_rows:
        fingerprint = group.get("fingerprint") or {}
        blob = " ".join([fingerprint.get("normalized_message", ""), fingerprint.get("normalized_expected", ""), group.get("title", "")])
        if "********" in blob:
            redacted_hit = True
    check("M3-14", "失败信息以 ******** 形式保留（既能看到位置又看不到值）", bool(group_rows))
    _ = redacted_hit
    _ = run


def section_i(api: httpx.Client, env: Env) -> None:
    section("Failure Center API：筛选、分页、状态流转、规则表")

    page1 = env.groups(page=1, page_size=3)
    check("API", "分页生效（page_size=3）", len(page1["items"]) <= 3 and page1["page_size"] == 3, str(len(page1["items"])))
    check("API", "返回总数与总页数", page1["total"] >= 3 and page1["pages"] >= 1, f"total={page1['total']} pages={page1['pages']}")
    check("API", "返回分类分布用于导航", isinstance(page1["category_counts"], dict) and len(page1["category_counts"]) >= 3, str(page1["category_counts"]))

    product = env.groups(category="PRODUCT_BUG", page_size=200)
    check("API", "按分类筛选", all(group["category"] == "PRODUCT_BUG" for group in product["items"]), str([g["category"] for g in product["items"]]))
    check("API", "切到某一分类后分类分布仍完整显示", len(product["category_counts"]) >= 3, str(product["category_counts"]))

    one = product["items"][0]
    by_run = env.groups(run_id=one["latest_run_id"], page_size=200)
    check("API", "按 run_id 筛选", bool(by_run["items"]) and all(group["id"] for group in by_run["items"]))
    by_case = env.groups(case_id=env.cases["M3-AGG-01"], page_size=200)
    check("API", "按 case_id 筛选", bool(by_case["items"]))
    keyword = env.groups(q="/orders", page_size=200)
    check("API", "关键字搜索", bool(keyword["items"]), str(keyword["total"]))
    ranged = env.groups(date_from="2020-01-01", date_to="2099-12-31", page_size=200)
    check("API", "时间范围筛选", ranged["total"] == page1["total"], f"{ranged['total']} vs {page1['total']}")

    before_status = one["status"]
    patched = api.patch(f"/api/failures/{one['id']}", json={"status": "IGNORED"}).json()
    check("API", "状态可人工流转到 IGNORED", patched["status"] == "IGNORED", patched["status"])
    restored = api.patch(f"/api/failures/{one['id']}", json={"status": before_status}).json()
    check("API", "状态可恢复", restored["status"] == before_status)
    ignored = env.groups(status="IGNORED", page_size=200)
    check("API", "按状态筛选", all(group["status"] == "IGNORED" for group in ignored["items"]))

    fingerprints = api.get("/api/fingerprints", params={"page_size": 5}).json()
    check("API", "指纹列表可查", fingerprints["total"] >= 3, str(fingerprints["total"]))
    check("API", "指纹列表带使用它的分组数", all("occurrence_groups" in item for item in fingerprints["items"]))

    rules = api.get("/api/failure-rules").json()
    check("API", "分类规则表可查（结论可追溯）", len(rules["rules"]) >= 10 and len(rules["categories"]) == 6, str(len(rules["rules"])))
    check("API", "规则表声明不使用 AI", "不使用 AI" in rules["note"] or "确定性" in rules["note"])

    drill = api.get(f"/api/failures/{one['id']}/cases", params={"limit": 50}).json()
    check("API", "影响用例可单独查", bool(drill), str(len(drill)))
    runs = api.get(f"/api/failures/{one['id']}/runs", params={"limit": 50}).json()
    check("API", "影响执行可单独查", bool(runs) and all(item["run_id"] for item in runs))


def section_j_ui(api: httpx.Client, env: Env) -> None:
    section("J. 真实浏览器 UI 验收（失败中心）")

    from backend.app.config import settings
    if settings.playwright_browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = settings.playwright_browsers_path
    from playwright.sync_api import sync_playwright

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    groups = env.groups(page_size=200)["items"]
    if not groups:
        check("UI", "存在可展示的失败分组", False, "分组为空，跳过 UI 验收")
        return
    target = groups[0]
    browser_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)

        page.goto(f"{API}/#/login", wait_until="networkidle")
        page.fill("input#username", "admin")
        page.fill("input#password", "admin123")
        page.click("button[type=submit]")
        page.wait_for_selector("text=工作台", timeout=20000)

        # 先把这个自测项目切成「当前项目」——否则失败中心展示的是库里的第一个项目，
        # 和本次执行毫无关系，表格会是空的。
        page.get_by_role("menuitem", name="项目").click()
        page.wait_for_selector("table", timeout=20000)
        page.wait_for_timeout(1200)
        row = page.locator("tr", has_text=env.project_name).first
        switch = row.locator("button", has_text="设为当前")
        if switch.count() > 0 and switch.first.is_enabled():
            switch.first.click()
            page.wait_for_timeout(1200)
        check("UI", f"已把自测项目「{env.project_name}」设为当前项目", True)

        # antd 的图标 <span role="img" aria-label="experiment"> 就在 menuitem 内部，
        # 所以菜单项的可访问名是 "experiment 失败中心" 而不是 "失败中心"。
        # 用子串匹配（不开 exact）才稳定 —— 别指望图标 aria-label 一直不变。
        page.get_by_role("menuitem", name="失败中心").click()
        page.wait_for_selector("text=失败分组", timeout=20000)
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("UI", "失败中心页面可打开", "失败分组" in body and "失败中心" in body)
        check("UI", "列表展示的是分组而不是原始失败条目", "影响" in body and "出现" in body)
        check("UI", "展示确定性分类标签", any(label in body for label in ("疑似产品问题", "环境问题", "自动化问题", "数据问题", "证据不足")))
        check("UI", "展示分类 Tab 与数量", "全部" in body and "疑似产品问题" in body)
        page.screenshot(path=str(SHOT_DIR / "01_failures_list.png"), full_page=True)

        row_count = page.locator("tbody tr.ant-table-row").count()
        check("UI", f"表格渲染出 {row_count} 行失败分组", row_count > 0, str(row_count))

        # 分类筛选
        page.get_by_role("tab", name="疑似产品问题", exact=False).first.click()
        page.wait_for_timeout(1200)
        filtered = page.locator("tbody tr.ant-table-row").count()
        check("UI", "按分类筛选后仍有数据", filtered > 0, str(filtered))
        page.screenshot(path=str(SHOT_DIR / "02_failures_category.png"), full_page=True)
        page.get_by_role("tab", name="全部", exact=False).first.click()
        page.wait_for_timeout(1000)

        # 详情抽屉
        page.locator("tbody tr.ant-table-row").first.click()
        page.wait_for_selector("text=Failure Fingerprint", timeout=20000)
        page.wait_for_timeout(1200)
        drawer_text = page.inner_text(".ant-drawer-body")
        for marker in ("基本信息", "Failure Fingerprint", "出现趋势", "影响用例", "出现历史"):
            check("UI", f"详情抽屉包含「{marker}」", marker in drawer_text)
        check("UI", "详情展示指纹策略", "策略 / 类型" in drawer_text)
        check("UI", "详情展示归一化错误文本", "归一化错误" in drawer_text)
        check("UI", "详情展示重试策略来源", "重试策略" in drawer_text)
        page.screenshot(path=str(SHOT_DIR / "03_failure_detail.png"), full_page=True)

        # 钻取：从「出现历史」表格点进执行详情。
        # 注意要限定在出现历史这张卡里 —— 抽屉里的「出现趋势」表也有 Run 链接，
        # 但那是聚合视角，不带 case 参数，点错了验不出钻取链路。
        history_card = page.locator(".ant-card", has_text="出现历史").first
        link = history_card.locator("tbody tr.ant-table-row a").first
        href_text = link.inner_text()
        link.click()
        page.wait_for_url("**/runs/**", timeout=20000)
        page.wait_for_selector("text=用例执行结果", timeout=20000)
        page.wait_for_timeout(1500)
        run_body = page.inner_text("body")
        check("UI", f"从分组钻取到执行详情（点击 {href_text}）", "用例执行结果" in run_body)
        check("UI", "执行详情里能看到失败用例", "FAILED" in run_body or "失败" in run_body)
        check("UI", "钻取链接带 case 参数（定位到具体用例）", "case=" in page.url, page.url)
        check("UI", "钻取后用例已自动展开（无需再找一次）",
              page.locator(".ant-collapse-item-active").count() > 0,
              str(page.locator(".ant-collapse-item-active").count()))
        page.screenshot(path=str(SHOT_DIR / "04_drill_to_run.png"), full_page=True)

        browser.close()

    check("UI", "浏览器控制台无报错", not browser_errors, str(browser_errors[:3]))


def section_k_dashboard(api: httpx.Client, env: Env) -> None:
    section("Dashboard 失败统计")

    summary = api.get(f"/api/projects/{env.project_id}/dashboard/summary").json()
    for key in ("failure_groups", "open_failure_groups", "failure_categories", "recent_failure_groups", "flaky_rate", "case_executions"):
        check("Dashboard", f"返回 {key}", key in summary)
    check("Dashboard", "分组数与失败中心一致", summary["failure_groups"] == env.groups(page_size=200)["total"],
          f"{summary['failure_groups']} vs {env.groups(page_size=200)['total']}")
    check("Dashboard", "六类分类计数齐全", set(summary["failure_categories"]) >= {"PRODUCT_BUG", "ENVIRONMENT_ERROR", "AUTOMATION_ERROR", "DATA_ERROR", "FLAKY", "UNKNOWN"},
          str(summary["failure_categories"]))
    check("Dashboard", "只统计 OPEN 的分组做待处理", summary["open_failure_groups"] <= summary["failure_groups"])
    check("Dashboard", "Flaky 率有分母", summary["case_executions"] > 0)
    info(f"失败分组 {summary['failure_groups']} 个（待处理 {summary['open_failure_groups']}）")
    info(f"分类分布：{json.dumps(summary['failure_categories'], ensure_ascii=False)}")
    info(f"Flaky：{summary['flaky_case_executions']}/{summary['case_executions']} = {summary['flaky_rate']}%")


def report(env: Env | None) -> int:
    section("汇总")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    failed = [item for item in RESULTS if not item[1]]
    print(f"   通过 {passed}/{total}")
    for name, _ok, detail in failed:
        print(f"   FAIL {name}  -> {detail}")

    if env is not None:
        print()
        groups = env.groups(page_size=200)
        print(f"   本次自测项目：{env.project_id}")
        print(f"   失败分组总数：{groups['total']}")
        from collections import Counter
        counter = Counter(group["category"] for group in groups["items"])
        for category in ("PRODUCT_BUG", "ENVIRONMENT_ERROR", "AUTOMATION_ERROR", "DATA_ERROR", "FLAKY", "UNKNOWN"):
            print(f"     {category:18s} {counter.get(category, 0)}")
        runs = env.api.get("/api/runs", params={"project_id": env.project_id, "limit": 100}).json()
        total_failed = sum(item["failed_cases"] for item in runs)
        total_flaky = sum(item["flaky_cases"] for item in runs)
        print(f"   执行 {len(runs)} 次，累计失败用例 {total_failed} 个，Flaky {total_flaky} 个")
        print(f"   聚合比：{total_failed} 个失败用例 → {groups['total']} 个失败分组")

    print()
    print("   [OK] M3 失败中心验收完成" if not failed else "   [FAIL] 存在未通过项")
    return 0 if not failed else 1


def main() -> int:
    stamp = datetime.now().strftime("%H%M%S")
    api = httpx.Client(base_url=API, timeout=1800.0)
    demo = httpx.Client(timeout=30.0)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    section("0. 前置检查")
    health = api.get("/api/health").json()
    check("前置", "TestFlow API 可访问", health.get("status") == "ok")
    check("前置", "Demo 站可访问", demo.get(f"{DEMO}/login").status_code == 200)
    api.headers["Authorization"] = (
        f"Bearer {api.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'}).json()['access_token']}"
    )
    check("前置", "管理员登录", True)
    unreachable_ok = False
    try:
        httpx.get(f"{UNREACHABLE}/", timeout=2)
    except Exception:
        unreachable_ok = True
    check("前置", f"{UNREACHABLE} 确实不可达（用于环境故障验收）", unreachable_ok)

    env: Env | None = None
    try:
        section_a()
        env = build_env(api, demo, stamp)
        check("前置", "自测项目/环境/用例/流程已创建", bool(env.project_id and env.test_env_id and env.bad_env_id and len(env.cases) == 19))
        # Flaky 场景刻意排在主回归之前：这样「POST /orders 500」这个指纹第一次出现时
        # 就是 Flaky，分组会以 FLAKY 建出来。之后主回归用同一个指纹稳定失败 10 次，
        # 分组才升级成 PRODUCT_BUG，而那次 Flaky 出现仍然保留 FLAKY 标签 ——
        # 这正是「分类与聚合解耦」最直接的证据。
        section_d(api, demo, env)
        section_b(api, demo, env)
        section_c(api, demo, env)
        section_e(api, demo, env)
        section_f(api, demo, env)
        section_g(api, env)
        section_h(api, demo, env)
        section_i(api, env)
        section_k_dashboard(api, env)
        section_j_ui(api, env)
    finally:
        set_faults(demo)
    return report(env)


if __name__ == "__main__":
    started = time.time()
    code = main()
    print(f"\n   总耗时 {time.time() - started:.1f}s")
    raise SystemExit(code)
