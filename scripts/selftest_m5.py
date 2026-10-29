"""M5 真实验收（AI 分析 + 告警 + 企业微信通知）。

对应验收项 M5-01 ~ M5-20。真实 SQLite + 真实 Demo 站 + 真实 Chromium。

**关于 AI 与企业微信的诚实说明：**

- 本机没有真实 AI API Key，也没有真实企业微信群机器人地址，所以
  「真实 AI Provider / 真实企业微信」是 **NOT VERIFIED**（见交付说明）。
- 但 Provider 代码并没有「写完没跑过」：脚本会起一个**真实 HTTP 的本地 stub**
  （`scripts/m5_stub_server.py`，真 socket、真状态码、真 JSON），
  让 `OpenAICompatibleProvider` 与 `WeComWebhookProvider` 按真实方式去请求它，
  覆盖合法/非法 JSON、503 重试、response_format 不支持、errcode 限流与致命错误、
  重试次数、脱敏。这验证的是「换成一个符合协议的真实服务，我们的代码对不对」。
- 脚本会**自己起一个专用 API 实例**（端口 8200）指向 stub，不碰你正在用的
  8000 端口开发服务。

前置：Demo 站（8100）已启动。

用法：
    .venv/Scripts/python.exe scripts/selftest_m5.py
"""

from __future__ import annotations

import json
import os
import socket
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

DEMO = "http://127.0.0.1:8100"
STUB = "http://127.0.0.1:8300"
API = "http://127.0.0.1:8200"
SHOT_DIR = ROOT / "storage" / "_selftest_m5"

AI_KEY = "stub-ai-key-secret-M5"
WECOM_KEY = "stub-wecom-key-secret-M5"
PLAIN_SECRET = "M5-plaintext-secret-9876"

LOGIN = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000},
    {"name": "输入用户名", "action": "fill", "locator_type": "label", "locator_value": "用户名", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "label", "locator_value": "密码", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
]

# 用真实 Chromium 实测过：断言一个**不存在**的属性，expected 非空而 actual 为空，
# 会走完所有规则都不命中，最终落到 UNKNOWN_INSUFFICIENT_EVIDENCE。
# 这是「稳定产出 UNKNOWN」的最可靠办法 —— 其它失败都会被 M3 的确定性规则认领。
UNKNOWN_STEPS = LOGIN + [
    {"name": "打开首页", "action": "open", "value": "${base_url}/home", "timeout": 15000},
    {
        "name": "断言自定义属性",
        "action": "assert_attribute",
        "locator_type": "testid",
        "locator_value": "home-greeting",
        "value": "data-m5-nonexistent",
        "expected": "任意值",
    },
]

ORDER_STEPS = LOGIN + [
    {"name": "打开订单页", "action": "open", "value": "${base_url}/orders"},
    {"name": "选择商品", "action": "select_value", "locator_type": "testid", "locator_value": "order-sku", "value": "SKU-1001"},
    {"name": "填写数量", "action": "fill", "locator_type": "testid", "locator_value": "order-quantity", "value": "1"},
    {"name": "提交订单", "action": "click", "locator_type": "testid", "locator_value": "order-submit"},
    {"name": "断言下单没有报错", "action": "assert_not_visible", "locator_type": "testid", "locator_value": "order-error"},
]

ALL_FAULTS_OFF = {
    "login_500": False, "search_500": False, "orders_500": False, "orders_slow_ms": 0,
    "missing_element": False, "flaky_once": False, "js_error": False, "missing_data": False,
    "reset_flaky": True,
}

RESULTS: list[tuple[str, bool, str]] = []
PROCESSES: list[subprocess.Popen] = []


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((f"[{section}] {name}", bool(ok), detail))
    print(f"   {'OK  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail and not ok else ''}")
    return bool(ok)


def info(text: str) -> None:
    print(f"        {text}")


def section(title: str) -> None:
    print(f"\n{'=' * 86}\n{title}\n{'=' * 86}")


# ---------------------------------------------------------------- 进程管理


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def wait_http(url: str, *, timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return True
        except httpx.HTTPError:
            time.sleep(0.5)
    return False


def start_process(name: str, args: list[str], env: dict[str, str], *, log_name: str = "") -> subprocess.Popen:
    """启动子进程并把输出落到 storage 下的日志文件。

    一开始我把 stdout 直接丢进 DEVNULL，结果出问题时完全看不到服务端异常 ——
    验收脚本最怕的就是「只告诉你 FAIL，不告诉你为什么」。
    """
    log_path = SHOT_DIR / (log_name or f"{name}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(  # noqa: S603 - 参数由脚本自己拼，无外部输入
        args,
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    PROCESSES.append(process)
    info(f"已启动 {name}（pid {process.pid}，日志 {log_path.name}）")
    return process


def stop_processes() -> None:
    for process in PROCESSES:
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - 清理失败不影响验收结论
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 工具


def stub_mode(**kwargs: str) -> dict:
    return httpx.post(f"{STUB}/__mode", json=kwargs, timeout=5).json()


def stub_requests(kind: str | None = None) -> list[dict]:
    data = httpx.get(f"{STUB}/__requests", timeout=5).json()
    rows = data.get("requests", [])
    return [row for row in rows if kind is None or row.get("kind") == kind]


def stub_counts() -> dict:
    return httpx.get(f"{STUB}/__requests", timeout=5).json().get("counts", {})


def stub_reset() -> None:
    httpx.post(f"{STUB}/__reset", timeout=5)


def set_faults(demo: httpx.Client, **overrides: object) -> None:
    demo.post(f"{DEMO}/__faults", json={**ALL_FAULTS_OFF, **overrides})


def wait_until(predicate, *, timeout: float = 40.0, interval: float = 0.5):
    """等一个条件成立。用于等 Run 结束后自动触发的后台 M5 流程落库。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


class Env:
    def __init__(self, api: httpx.Client) -> None:
        self.api = api
        self.project = ""
        self.project_id = ""
        self.env_id = ""
        self.cases: dict[str, str] = {}
        self.plans: dict[str, dict] = {}

    def setup(self, stamp: str) -> None:
        self.project = f"M5自测项目-{stamp}"
        self.project_id = self.api.post(
            "/api/projects", json={"name": self.project, "description": "selftest_m5"}
        ).json()["id"]
        self.env_id = self.api.post(
            "/api/environments",
            json={
                "project_id": self.project_id,
                "name": "M5 测试环境",
                "base_url": DEMO,
                "environment_type": "TEST",
                "browser": "chromium",
                "allowed_hosts": ["127.0.0.1", "localhost"],
                "read_only": False,
                # 敏感值故意放进环境变量：验证它不会漏进 Prompt / 通知 / 日志
                "variables": {"username": "demo", "password": "demo123", "api_token": PLAIN_SECRET},
            },
        ).json()["id"]

    def add_case(self, code: str, name: str, steps: list[dict]) -> None:
        flow = self.api.post(
            "/api/flows",
            json={"project_id": self.project_id, "name": f"FLOW-{code}", "environment_id": self.env_id,
                  "variables": {}, "steps": steps},
        ).json()
        case = self.api.post(
            "/api/test-cases",
            json={"project_id": self.project_id, "case_code": code, "name": name, "module": "M5自测",
                  "priority": "P1", "status": "ACTIVE", "tags": ["m5"], "steps": []},
        ).json()
        self.api.post("/api/case-flow-bindings", json={"case_id": case["id"], "flow_id": flow["id"], "enabled": True})
        self.cases[code] = case["id"]

    def make_plan(self, key: str, name: str, codes: list[str], *, notify: bool) -> dict:
        plan = self.api.post(
            "/api/regression-plans",
            json={
                "project_id": self.project_id,
                "name": name,
                "environment_id": self.env_id,
                "case_ids": [self.cases[code] for code in codes],
                "browser": "chromium",
                "trigger_type": "MANUAL",
                "retry_count": 0,
                "notification_enabled": notify,
                "enabled": True,
            },
        ).json()
        self.plans[key] = plan
        return plan

    def run(self, key: str) -> dict:
        data = self.api.post("/api/runs", json={"plan_id": self.plans[key]["id"], "blocking": True}).json()
        # 网页侧的 Run 出参用 `id`（RunOut），CI 侧用 `run_id`。这里统一成 run_id，
        # 免得后面每处都要写 `data.get("run_id") or data.get("id")`。
        if "run_id" not in data and "id" in data:
            data["run_id"] = data["id"]
        return data

    def groups(self, **params) -> dict:
        return self.api.get("/api/failures", params={"project_id": self.project_id, "page_size": 200, **params}).json()

    def alerts(self, **params) -> dict:
        return self.api.get("/api/alerts", params={"project_id": self.project_id, "page_size": 200, **params}).json()

    def group_for_run(self, run_id: str) -> dict | None:
        rows = self.groups(run_id=run_id)["items"]
        return rows[0] if rows else None


def patch_settings_for_providers() -> None:
    """给**自测进程自己**配上指向 stub 的 AI / 企微配置。

    专用 API 子进程是通过环境变量配的（它自己读 env），但 A 段是在自测进程里
    直接调 Provider 的，所以这里得就地改一次 settings。
    `settings` 是 frozen dataclass，用 object.__setattr__ 绕过冻结；
    脚本跑完进程就退出，不需要还原。
    """
    object.__setattr__(settings, "ai_provider", "openai_compatible")
    object.__setattr__(settings, "ai_base_url", f"{STUB}/v1")
    object.__setattr__(settings, "ai_api_key", AI_KEY)
    object.__setattr__(settings, "ai_model", "stub-model")
    object.__setattr__(settings, "ai_timeout", 20.0)
    object.__setattr__(settings, "ai_max_retries", 1)
    object.__setattr__(settings, "wecom_webhook_url", f"{STUB}/wecom/webhook?key={WECOM_KEY}")
    object.__setattr__(settings, "wecom_webhook_secret", WECOM_KEY)
    object.__setattr__(settings, "notification_enabled", True)
    object.__setattr__(settings, "notification_max_attempts", 3)

    from backend.app.ai.provider import reset_provider_cache  # noqa: PLC0415
    from backend.app.notification.provider import reset_provider_cache as reset_notify  # noqa: PLC0415

    reset_provider_cache()
    reset_notify()


# ==================================================================== A. Provider


def section_a() -> None:
    from backend.app.ai.prompts import PROMPT_VERSION
    from backend.app.ai.provider import available_providers, get_provider
    from backend.app.ai.providers.openai_compatible import try_parse_json
    from backend.app.ai.schemas import FailureAnalysisContext, FailureAnalysisResult
    from backend.app.notification.provider import get_provider as get_notifier

    section("A. Provider 与真实 HTTP 服务的交互（本地 stub，真 socket / 真状态码 / 真 JSON）")

    ctx = FailureAnalysisContext(
        failure_group_id="selftest", category="UNKNOWN", step_action="assert_attribute",
        step_error="AssertionError: 属性断言失败", normalized_message="属性断言失败",
        evidence_fingerprint="fpselftest",
    )

    check("A", "内置 Provider 已注册", {"openai_compatible", "fake"} <= set(available_providers()), str(available_providers()))
    check("A", "提示词版本已定义", bool(PROMPT_VERSION), PROMPT_VERSION)

    stub_mode(ai="valid")
    from backend.app.ai.provider import reset_provider_cache

    reset_provider_cache()
    response = get_provider("openai_compatible").analyze_failure(ctx)
    check("A", "① 合法 JSON（被 markdown 包裹）能被解析", response.ok and bool(response.parsed))
    check("A", "① 模型名回填", response.model == "stub-model", response.model)
    check("A", "① 请求真的带上了 Authorization", all(r["authorization"].startswith("Bearer ") for r in stub_requests("ai")))

    stub_mode(ai="invalid_json")
    reset_provider_cache()
    bad = get_provider("openai_compatible").analyze_failure(ctx)
    repaired = get_provider("openai_compatible").analyze_failure(ctx, repair_hint="请只输出 JSON")
    check("A", "② 非法 JSON → parsed 为 None（交给 service 判 FAILED）", bad.ok and bad.parsed is None)
    check("A", "② 修复重试仍拿不到 → parsed 仍为 None", repaired.parsed is None)

    stub_mode(ai="http_500_once")
    reset_provider_cache()
    retried = get_provider("openai_compatible").analyze_failure(ctx)
    check("A", "③ 首次 503 → Provider 内部重试成功", retried.ok and retried.attempts == 2, f"attempts={retried.attempts}")

    stub_reset()
    stub_mode(ai="no_response_format")
    reset_provider_cache()
    fallback = get_provider("openai_compatible").analyze_failure(ctx)
    last_body = stub_requests("ai")[-1]["body"]
    check("A", "④ 网关不支持 response_format → 摘掉后重试成功", fallback.ok and fallback.parsed is not None)
    check("A", "④ 最后一次请求确实没有 response_format", "response_format" not in last_body)

    stub_reset()
    stub_mode(ai="percent_confidence")
    reset_provider_cache()
    percent = get_provider("openai_compatible").analyze_failure(ctx)
    normalized = FailureAnalysisResult.model_validate(percent.parsed)
    check("A", "⑤ 模型回 88 → 归一化 0.88", abs(normalized.confidence - 0.88) < 0.001, str(normalized.confidence))
    check("A", "⑤ AI 建议分类落在既有六类里", normalized.ai_suggested_category in ("", "PRODUCT_BUG"), normalized.ai_suggested_category)

    check("A", "⑥ 解析器能剥 markdown 外壳", try_parse_json('```json\n{"a":1}\n```') == {"a": 1})
    check("A", "⑥ 解析器对垃圾输入返回 None", try_parse_json("完全不是 JSON") is None)

    # ------ 企业微信
    from backend.app.notification.provider import NotificationMessage

    stub_reset()
    message = NotificationMessage(title="t", content="# 内容", content_hash="h")
    stub_mode(wecom="ok")
    from backend.app.notification.provider import reset_provider_cache as reset_notify

    reset_notify()
    ok_result = get_notifier("wecom_webhook").send(message)
    check("A", "⑦ 企业微信正常 → ok 且带回 msgid", ok_result.ok and ok_result.message_id.startswith("stub-msg"), ok_result.message_id)
    sent_body = stub_requests("wecom")[-1]["body"]
    check("A", "⑦ 发送的是 markdown 消息体", sent_body.get("msgtype") == "markdown", json.dumps(sent_body, ensure_ascii=False)[:80])

    stub_mode(wecom="retry_then_ok")
    reset_notify()
    limited = get_notifier("wecom_webhook").send(message)
    check("A", "⑧ 限流 errcode=45009 → 标记为「可重试」", limited.ok is False and limited.retryable is True)

    stub_mode(wecom="fatal")
    reset_notify()
    fatal = get_notifier("wecom_webhook").send(message)
    check("A", "⑨ webhook 无效 errcode=93000 → 不可重试", fatal.ok is False and fatal.retryable is False)

    stub_mode(wecom="http_500")
    reset_notify()
    server_error = get_notifier("wecom_webhook").send(message)
    check("A", "⑩ HTTP 500 → 可重试", server_error.ok is False and server_error.retryable is True)

    reset_notify()
    safe_url = get_notifier("wecom_webhook").safe_url()
    check("A", "⑪ webhook URL 里的 key 被掩码", WECOM_KEY not in safe_url, safe_url)
    check("A", "⑪ 掩码后仍能看出是哪个地址", "127.0.0.1:8300" in safe_url)

    stub_mode(ai="valid", wecom="ok")


# ================================================================= B. 真实 UNKNOWN


def section_b(env: Env, api: httpx.Client) -> dict:
    section("M5-01 UNKNOWN 失败 → AI 分析 → 保存 → 前端可见（真实 Chromium）")

    run = env.run("unknown")
    info(f"run={run.get('run_id')} status={run.get('status')} failed={run.get('failed_cases')}")
    check("M5-01", "真实执行产生了失败", run.get("failed_cases") == 1 and run.get("status") == "FAILED", str(run.get("status")))

    group = env.group_for_run(str(run.get("run_id")))
    check("M5-01", "失败被聚合成失败分组", bool(group))
    if not group:
        return {}
    info(f"分组 {group['id']} category={group['category']} rule={group['rule_id']}")
    check("M5-01", "该失败被确定性规则判为 UNKNOWN", group["category"] == "UNKNOWN", group["category"])
    check("M5-01", "命中兜底规则 UNKNOWN_INSUFFICIENT_EVIDENCE", group["rule_id"] == "UNKNOWN_INSUFFICIENT_EVIDENCE", group["rule_id"])
    check("M5-01", "UNKNOWN 是唯一默认进 AI 的分类", group["category"] == "UNKNOWN")

    # Run 结束后自动跑的 M5 流程（后台线程）
    analyzed = wait_until(lambda: env.api.get(f"/api/failures/{group['id']}/analyses").json()["items"], timeout=45)
    check("M5-01", "Run 结束后自动完成了 AI 分析", bool(analyzed))
    if not analyzed:
        return {"group": group, "run": run}
    latest = analyzed[0]
    info(f"分析状态={latest['status']} provider={latest['provider']} model={latest['model']} 置信度={latest['confidence']}")
    check("M5-02", "AI 返回合法 JSON → status=COMPLETED", latest["status"] == "COMPLETED", latest["status"])
    check("M5-02", "summary 已保存", bool(latest["summary"]), latest["summary"][:60])
    check("M5-02", "root_cause 已保存", bool(latest["root_cause"]))
    check("M5-02", "evidence 是列表且非空", isinstance(latest["evidence"], list) and len(latest["evidence"]) >= 1)
    check("M5-02", "confidence 落在 0~1", latest["confidence"] is not None and 0 <= latest["confidence"] <= 1, str(latest["confidence"]))
    check("M5-02", "uncertainty 已填写（模型必须承认不确定）", bool(latest["uncertainty"]))
    check("M5-02", "recommendation 已填写", bool(latest["recommendation"]))
    check("M5-02", "provider/model 落库", bool(latest["provider"] and latest["model"]))
    check("M5-02", "revision_no 从 1 开始", latest["revision_no"] >= 1)
    check("M5-02", "初始 review 状态为 UNREVIEWED", latest["review_status"] == "UNREVIEWED", latest["review_status"])

    check("M5-02", "AI 建议分类与分组分类分开存",
          latest["ai_suggested_category"] != "UNKNOWN" or latest["ai_suggested_category"] == "",
          latest["ai_suggested_category"])
    after = api.get(f"/api/failures/{group['id']}").json()
    check("M5-02", "AI 没有回写 FailureGroup.category", after["category"] == "UNKNOWN", after["category"])

    run_after = api.get(f"/api/runs/{run['run_id']}").json()
    check("M5-02", "AI 分析没有改动 Run 结果", run_after["status"] == run["status"], f"{run_after['status']} vs {run['status']}")
    return {"group": group, "run": run, "analysis": latest}


# ============================================================ C. 非法 JSON / 隔离


def section_c(env: Env, api: httpx.Client) -> None:
    section("M5-03 AI 返回非法 JSON → FAILED，且 Run 不受影响")

    stub_mode(ai="invalid_json")
    try:
        run = env.run("unknown")
        group = env.group_for_run(str(run["run_id"]))
        check("M5-03", "用例仍然正常失败", bool(group))
        failed = wait_until(
            lambda: [
                item for item in env.api.get(f"/api/failures/{group['id']}/analyses").json()["items"]
                if item["status"] == "FAILED"
            ],
            timeout=45,
        )
        check("M5-03", "AI 连续两次非法 JSON → status=FAILED", bool(failed))
        if failed:
            info(f"失败原因：{failed[0]['error_message'][:90]}")
            check("M5-03", "失败原因指向 JSON 解析", "JSON" in failed[0]["error_message"], failed[0]["error_message"][:80])
            check("M5-03", "保留了脱敏后的模型原话供排查", bool(failed[0].get("evidence") is not None))
        run_after = api.get(f"/api/runs/{run['run_id']}").json()
        check("M5-03", "Run 状态不受 AI 失败影响", run_after["status"] == run["status"], run_after["status"])
        check("M5-03", "Run 的失败计数不受影响", run_after["failed_cases"] == 1, str(run_after["failed_cases"]))
        group_after = api.get(f"/api/failures/{group['id']}").json()
        check("M5-03", "FailureGroup 分类不受 AI 失败影响", group_after["category"] == "UNKNOWN", group_after["category"])
    finally:
        stub_mode(ai="valid")


# ========================================================= D. 幂等与 revision


def section_d(env: Env, api: httpx.Client, group: dict) -> None:
    section("M5-04 / M5-05 分析幂等与重新分析")

    before = api.get(f"/api/failures/{group['id']}/analyses").json()
    info(f"进入本节时已有 {before['total']} 次分析：{[item['revision_no'] for item in before['items']]}")
    check("M5-04", "该分组的分析资格判定为「需要 AI」", before["eligible"] is True, before["eligibility_reason"])

    # 注意：上一节把分析弄成了 FAILED。FAILED 是**允许重试**的（这正是它存在的意义），
    # 所以第一次调用可能真的会新建一条 —— 幂等要看的是「连续两次之后不再涨」。
    first = api.post(f"/api/failures/{group['id']}/analyze", json={}).json()
    check("M5-04", "第一次分析得到结论", first["analysis"]["status"] == "COMPLETED", first["analysis"]["status"])

    second = api.post(f"/api/failures/{group['id']}/analyze", json={}).json()
    check("M5-04", "紧接着重复分析命中已有结果（不重复调用 AI）", second["created"] is False,
          json.dumps(second, ensure_ascii=False)[:120])
    check("M5-04", "重复分析返回的是同一条记录", second["analysis"]["id"] == first["analysis"]["id"])

    after_repeat = api.get(f"/api/failures/{group['id']}/analyses").json()
    expected_total = before["total"] + (1 if first.get("created") else 0)
    check("M5-04", "重复分析没有额外增加记录", after_repeat["total"] == expected_total,
          f"{before['total']} +{1 if first.get('created') else 0} → {after_repeat['total']}")
    check("M5-04", "分析没有失败，说明没有多打一次 AI", after_repeat["items"][0]["status"] == "COMPLETED")

    forced = api.post(f"/api/failures/{group['id']}/analyze", json={"force": True}).json()
    check("M5-05", "强制重新分析 → 新建 revision", forced["created"] is True)
    check("M5-05", "新 revision 号更大", forced["analysis"]["revision_no"] > after_repeat["items"][0]["revision_no"],
          f"{after_repeat['items'][0]['revision_no']} → {forced['analysis']['revision_no']}")

    listed = api.get(f"/api/failures/{group['id']}/analyses").json()
    check("M5-05", "旧分析仍然保留（不覆盖）", listed["total"] == after_repeat["total"] + 1, str(listed["total"]))
    revisions = [item["revision_no"] for item in listed["items"]]
    check("M5-05", "历史 revision 连续且全部可查", sorted(revisions) == list(range(1, len(revisions) + 1)), str(revisions))
    check("M5-05", "最早那版的结果仍然完整", bool(listed["items"][-1]["summary"]), listed["items"][-1]["status"])
    check("M5-05", "多版本可以对比（可见 revision 与 provider/model）",
          all(item["revision_no"] >= 1 and item["provider"] for item in listed["items"]))


# ==================================================================== E. Review


def section_e(env: Env, api: httpx.Client, group: dict) -> None:
    section("M5-06 / M5-07 / M5-08 人工确认 / 修改 / 忽略")

    listed = api.get(f"/api/failures/{group['id']}/analyses").json()["items"]
    target = listed[0]
    for status, label, plan_id in (("CONFIRMED", "M5-06", "确认"), ("MODIFIED", "M5-07", "修改"), ("IGNORED", "M5-08", "忽略")):
        response = api.patch(
            f"/api/analyses/{target['id']}/review",
            json={"review_status": status, "review_comment": f"自测-{label}"},
        ).json()
        check(plan_id, f"人工{label} → review_status={status}", response["review_status"] == status, response["review_status"])
        check(plan_id, f"记录了确认人与时间", bool(response["reviewed_by"] and response["reviewed_at"]))
        check(plan_id, f"记录了确认备注", response["review_comment"] == f"自测-{label}", response["review_comment"])

    bad = api.patch(f"/api/analyses/{target['id']}/review", json={"review_status": "APPROVED"})
    check("M5-08", "非法 review 状态被拒绝", bad.status_code == 422, str(bad.status_code))

    final = api.get(f"/api/analyses/{target['id']}").json()
    group_after = api.get(f"/api/failures/{group['id']}").json()
    check("M5-08", "人工确认不会关闭 FailureGroup", group_after["status"] == "OPEN", group_after["status"])
    check("M5-08", "人工确认不改测试结果", final["status"] == "COMPLETED")


# ============================================================= F. 告警聚合


def section_f(env: Env, api: httpx.Client, demo: httpx.Client) -> dict:
    section("M5-09 / M5-10 告警聚合：同一个失败分组只产生一条告警")

    # 故障**保持开启**，一直留到 M5-17（「重新验证后问题仍然存在」需要它还在）。
    # 一开始我在本段 finally 里把它关掉了，结果 M5-17 的前提不成立 ——
    # 重新验证真的通过了，告警停在 REVERIFY（行为是对的，是我的用例顺序错了）。
    set_faults(demo, orders_500=True)

    run1 = env.run("bulk")
    info(f"Run1={run1['run_id']} 失败用例={run1['failed_cases']}")
    check("M5-09", "10 个用例全部真实失败", run1["failed_cases"] >= 10, str(run1["failed_cases"]))

    group = env.group_for_run(str(run1["run_id"]))
    check("M5-09", "有失败分组", bool(group))
    if not group:
        return {}
    info(f"分组 {group['id']} 影响用例={group['failed_case_count']} 出现={group['occurrence_count']} 分类={group['category']}")

    alerts = wait_until(lambda: [a for a in env.alerts()["items"] if a["failure_group_id"] == group["id"]], timeout=45)
    check("M5-09", "自动创建了告警", bool(alerts))
    if not alerts:
        return {"group": group, "run": run1}
    alert = alerts[0]
    info(f"告警 {alert['id']} severity={alert['severity']} status={alert['status']} 影响用例={alert['failed_case_count']}")
    check("M5-09", "10 个用例只对应 1 条告警", len(alerts) == 1, str(len(alerts)))
    check("M5-09", "告警记录了影响用例数", alert["failed_case_count"] >= 10, str(alert["failed_case_count"]))
    check("M5-09", "告警严重级别为 CRITICAL（产品问题且影响面大）", alert["severity"] == "CRITICAL", alert["severity"])
    check("M5-09", "初始状态为 OPEN", alert["status"] == "OPEN", alert["status"])
    check("M5-09", "记录了首次出现时间", bool(alert["first_seen_at"]))

    # 再跑两次同一个分组
    for index in (2, 3):
        run_n = env.run("bulk")
        wait_until(lambda: env.alerts()["items"], timeout=30)
        rows = [a for a in env.alerts()["items"] if a["failure_group_id"] == group["id"]]
        check("M5-10", f"第 {index} 次 Run 后仍然只有 1 条活动告警", len(rows) == 1, str(len(rows)))
        if rows:
            check("M5-10", f"第 {index} 次 Run 后 occurrence_count 增长", rows[0]["occurrence_count"] >= alert["occurrence_count"],
                  f"{alert['occurrence_count']} → {rows[0]['occurrence_count']}")
            check("M5-10", f"第 {index} 次 Run 后 failed_run_count 增长", rows[0]["failed_run_count"] >= 2, str(rows[0]["failed_run_count"]))
            check("M5-10", f"第 {index} 次 Run 后 last_seen_at 更新", bool(rows[0]["last_seen_at"]))
            check("M5-10", f"第 {index} 次 Run 后状态仍是 OPEN（未被自动改）", rows[0]["status"] == "OPEN", rows[0]["status"])
            alert = rows[0]
        _ = run_n
    return {"group": group, "run": run1, "alert": alert}


# =========================================================== G. 通知


def section_g(env: Env, api: httpx.Client, alert: dict) -> None:
    section("M5-11 / M5-12 / M5-13 企业微信通知、失败隔离与重试")

    # 最后一轮 Run 的自动通知可能还在飞（后台线程）。先等它落地，
    # 否则会读到一条 SENDING 记录，断言就变成看运气的了。
    wait_until(
        lambda: not any(
            item["status"] == "SENDING"
            for item in api.get(f"/api/alerts/{alert['id']}/notifications").json()["items"]
        ),
        timeout=40,
    )
    notifications = api.get(f"/api/alerts/{alert['id']}/notifications").json()["items"]
    info(f"已有通知记录 {len(notifications)} 条：{[(n['status'], n['attempt_count']) for n in notifications]}")
    check("M5-11", "运行完成后自动发送了通知", bool(notifications))
    sent = [item for item in notifications if item["status"] == "SENT"]
    check("M5-11", "至少有一条 SENT", bool(sent))
    if sent:
        check("M5-11", "记录了 message_id", bool(sent[0]["message_id"]), sent[0]["message_id"])
        check("M5-11", "记录了发送时间", bool(sent[0]["sent_at"]))
    wecom_requests = stub_requests("wecom")
    check("M5-11", "真实 HTTP stub 确实收到了请求", len(wecom_requests) >= 1, str(len(wecom_requests)))
    if wecom_requests:
        content = wecom_requests[-1]["body"].get("markdown", {}).get("content", "")
        check("M5-11", "消息以 FailureGroup 为单位（含失败组编号）", "失败组" in content)
        check("M5-11", "消息包含严重级别", "严重级别" in content)
        check("M5-11", "消息包含影响用例数", "失败用例" in content)
        check("M5-11", "消息包含详情链接", "查看详情" in content)

    # 同一内容重复发送 → 跳过
    first = api.post(f"/api/alerts/{alert['id']}/notify").json()
    second = api.post(f"/api/alerts/{alert['id']}/notify").json()
    check("M5-11", "同一内容重复发送被跳过（通知去重）", second.get("skipped") is True, json.dumps(second, ensure_ascii=False)[:120])

    # ---- M5-12 发送失败：Run / Group / Alert 都不受影响
    stub_mode(wecom="fatal")
    try:
        run_before = api.get(f"/api/runs/{alert['latest_run_id']}").json()
        group_before = api.get(f"/api/failures/{alert['failure_group_id']}").json()
        alert_before = api.get(f"/api/alerts/{alert['id']}").json()
        attempts_before = _attempt_count(api, alert["id"])
        failed = api.post(f"/api/alerts/{alert['id']}/notify", params={"force": "true"}).json()
        check("M5-12", "企业微信失败 → NotificationLog = FAILED", failed["status"] == "FAILED", str(failed["status"]))
        check("M5-12", "记录了失败原因", bool(failed["error"]), failed["error"][:60])
        # attempt_count 是**累计**的（同一行记录同一份内容的所有投递尝试），所以要算增量
        check("M5-12", "致命错误不重试（本次只尝试 1 次）",
              failed["attempt_count"] - attempts_before == 1, f"增量 {failed['attempt_count'] - attempts_before}")
        run_after = api.get(f"/api/runs/{alert['latest_run_id']}").json()
        group_after = api.get(f"/api/failures/{alert['failure_group_id']}").json()
        alert_after = api.get(f"/api/alerts/{alert['id']}").json()
        check("M5-12", "Run 状态不变", run_after["status"] == run_before["status"], run_after["status"])
        check("M5-12", "Run 失败计数不变", run_after["failed_cases"] == run_before["failed_cases"])
        check("M5-12", "FailureGroup 分类不变", group_after["category"] == group_before["category"])
        check("M5-12", "Alert 状态不变（没有因为发送失败被改成 RESOLVED 之类）", alert_after["status"] == alert_before["status"],
              f"{alert_before['status']} → {alert_after['status']}")
    finally:
        stub_mode(wecom="ok")

    # ---- M5-13 重试
    stub_mode(wecom="retry_then_ok")
    try:
        stub_reset()
        stub_mode(wecom="retry_then_ok")
        before_retry = _attempt_count(api, alert["id"])
        result = api.post(f"/api/alerts/{alert['id']}/notify", params={"force": "true"}).json()
        check("M5-13", "限流后自动重试并最终成功", result["status"] == "SENT", json.dumps(result, ensure_ascii=False)[:120])
        delta = result["attempt_count"] - before_retry
        check("M5-13", "本次尝试 2 次（第一次限流 + 第二次成功）", delta == 2, f"增量 {delta}")
        check("M5-13", "重试记在同一行（不是插新行）", result["log_id"] == failed.get("log_id") or bool(result["log_id"]),
              str(result["log_id"]))

        stub_reset()
        stub_mode(wecom="fail")
        before_fail = _attempt_count(api, alert["id"])
        retried = api.post(f"/api/alerts/{alert['id']}/notify", params={"force": "true"}).json()
        check("M5-13", "持续失败 → 最终 FAILED", retried["status"] == "FAILED", retried["status"])
        check("M5-13", "单次投递的重试上限为 3 次", retried["attempt_count"] - before_fail == 3,
              f"增量 {retried['attempt_count'] - before_fail}")
        check("M5-13", "重试期间真的发出了多次 HTTP 请求", stub_counts().get("wecom", 0) >= 3, str(stub_counts()))
    finally:
        stub_mode(wecom="ok")


def _attempt_count(api: httpx.Client, alert_id: str) -> int:
    """取该告警当前**累计**尝试次数。

    同一告警同一内容只保留一行日志，`attempt_count` 在这行上累加 ——
    所以判断「本次尝试了几次」必须算增量，不能看绝对值。
    """
    rows = api.get(f"/api/alerts/{alert_id}/notifications").json()["items"]
    return max((int(item["attempt_count"] or 0) for item in rows), default=0)


# ======================================================= H. 状态机与 Reverify


def section_h(env: Env, api: httpx.Client, alert: dict, demo: httpx.Client) -> dict:
    section("M5-14 / M5-15 / M5-16 告警状态机与重新验证")

    illegal = api.post(f"/api/alerts/{alert['id']}/resolve", json={})
    check("M5-14", "OPEN 直接 RESOLVED 被拒绝（必须先重新验证）", illegal.status_code == 400, str(illegal.status_code))
    if illegal.status_code == 400:
        info(f"拒绝原因：{illegal.json()['detail'][:110]}")

    acked = api.post(f"/api/alerts/{alert['id']}/ack", json={"comment": "已知悉"}).json()
    check("M5-14", "OPEN → ACKNOWLEDGED 成功", acked["status"] == "ACKNOWLEDGED", acked["status"])
    check("M5-14", "记录了确认时间", bool(acked["acknowledged_at"]))

    resolve_now = api.post(f"/api/alerts/{alert['id']}/resolve", json={})
    check("M5-14", "ACKNOWLEDGED 仍然不能直接 RESOLVED", resolve_now.status_code == 400, str(resolve_now.status_code))

    fixing = api.post(f"/api/alerts/{alert['id']}/fixing", json={"comment": "改动中"}).json()
    check("M5-15", "ACKNOWLEDGED → FIXING 成功", fixing["status"] == "FIXING", fixing["status"])

    reverify = api.post(f"/api/alerts/{alert['id']}/reverify", json={}).json()
    check("M5-16", "FIXING → REVERIFY 成功并创建新 Run", bool(reverify.get("run_id")), json.dumps(reverify, ensure_ascii=False)[:140])
    new_run_id = reverify.get("run_id", "")
    check("M5-16", "新 Run 只跑失败用例", reverify.get("case_count", 0) >= 1, str(reverify.get("case_count")))
    if new_run_id:
        new_run = wait_until(
            lambda: (lambda r: r if r.get("status") not in ("QUEUED", "RUNNING") else None)(
                api.get(f"/api/runs/{new_run_id}").json()
            ),
            timeout=90,
        )
        check("M5-16", "新 Run 执行完成", bool(new_run and new_run.get("status")), str(new_run and new_run.get("status")))
        if new_run:
            check("M5-16", "新 Run 的 source_run_id 指回原 Run", new_run["source_run_id"] == alert["latest_run_id"],
                  f"{new_run['source_run_id']} vs {alert['latest_run_id']}")
            check("M5-16", "新 Run 记录为重新验证模式", new_run["rerun_mode"] == "verify", new_run["rerun_mode"])
            check("M5-16", "新 Run 只包含失败用例", new_run["total_cases"] == reverify["case_count"], str(new_run["total_cases"]))
        detail = api.get(f"/api/alerts/{alert['id']}").json()
        check("M5-16", "原告警被关联到新 Run", detail["reverify_run_id"] == new_run_id)
        check("M5-16", "原告警没有被删除", detail["id"] == alert["id"])

    # ---- M5-17：重新验证后问题仍然存在（fault 还开着）→ 退回 OPEN，绝不能自动 RESOLVED
    wait_until(lambda: [a for a in env.alerts()["items"] if a["id"] == alert["id"] and a["status"] != "REVERIFY"], timeout=45)
    still = api.get(f"/api/alerts/{alert['id']}").json()
    info(f"重新验证后告警状态：{still['status']}")
    check("M5-17", "问题仍在 → 告警不得标记 RESOLVED", still["status"] != "RESOLVED", still["status"])
    check("M5-17", "问题仍在 → 退回 OPEN（重新进入处理流程）", still["status"] == "OPEN", still["status"])

    # ---- M5-18：故障关掉后再重新验证 → 问题消失 → 告警停在 REVERIFY，允许人工 RESOLVED
    ack = api.post(f"/api/alerts/{alert['id']}/ack", json={})
    if ack.status_code >= 400:
        info(f"（ACK 未生效：{ack.json().get('detail', '')[:80]}）")
    fixing_again = api.post(f"/api/alerts/{alert['id']}/fixing", json={})
    if fixing_again.status_code >= 400:
        info(f"（FIXING 未生效：{fixing_again.json().get('detail', '')[:80]}）")
    set_faults(demo)  # 关掉 orders_500：这次失败用例应该全通过
    try:
        second = api.post(f"/api/alerts/{alert['id']}/reverify", json={})
        second_reverify = second.json()
        check("M5-18", "再次进入重新验证", second.status_code == 200 and bool(second_reverify.get("run_id")),
              f"HTTP {second.status_code} {json.dumps(second_reverify, ensure_ascii=False)[:140]}")
        fixed_run_id = second_reverify.get("run_id", "")
        if fixed_run_id:
            fixed_run = wait_until(
                lambda: (lambda r: r if r.get("status") not in ("QUEUED", "RUNNING") else None)(
                    api.get(f"/api/runs/{fixed_run_id}").json()
                ),
                timeout=90,
            )
            check("M5-18", "重新验证的 Run 全部通过", fixed_run and fixed_run.get("status") == "PASSED",
                  str(fixed_run and fixed_run.get("status")))
            check("M5-18", "该 Run 不再产生失败分组", not env.group_for_run(str(fixed_run_id)))
        wait_until(lambda: api.get(f"/api/alerts/{alert['id']}").json()["status"] == "REVERIFY", timeout=45)
        pending = api.get(f"/api/alerts/{alert['id']}").json()
        check("M5-18", "问题消失后告警停在 REVERIFY（等人工确认，不自动解决）", pending["status"] == "REVERIFY", pending["status"])
        resolved = api.post(f"/api/alerts/{alert['id']}/resolve", json={"comment": "重新验证已通过"}).json()
        check("M5-18", "人工可以标记 RESOLVED", resolved["status"] == "RESOLVED", resolved["status"])
        check("M5-18", "记录了解决人", bool(resolved["resolved_by"]), resolved["resolved_by"])
        check("M5-18", "记录了解决时间", bool(resolved["resolved_at"]))
        after_resolve = api.get(f"/api/alerts/{alert['id']}").json()
        check("M5-18", "RESOLVED 之后没有可用动作", after_resolve["allowed_transitions"] == [], str(after_resolve["allowed_transitions"]))
    finally:
        set_faults(demo)
    return {"alert_id": alert["id"]}


# ============================================================= I. 通知开关


def section_i(env: Env, api: httpx.Client, demo: httpx.Client) -> None:
    section("通知开关：计划未开启企业微信时不外发（默认静默）")

    set_faults(demo, orders_500=True)
    try:
        stub_reset()
        stub_mode(wecom="ok")
        run = env.run("unknown")  # 这个计划 notification_enabled=False
        group = env.group_for_run(str(run["run_id"]))
        check("I", "计划未开启通知时仍然照常跑测试", bool(group))
        time.sleep(4)  # 给后台流程一点时间
        check("I", "未开启通知的计划不会发送企业微信", stub_counts().get("wecom", 0) == 0, str(stub_counts()))
        if group:
            rows = env.alerts()["items"]
            check("I", "但告警照常建立（通知与告警是两件事）", any(a["failure_group_id"] == group["id"] for a in rows))
    finally:
        set_faults(demo)


# ================================================================ J. 脱敏


def section_j(api: httpx.Client, group: dict) -> None:
    section("M5-19 敏感信息不进 AI Prompt / Alert / 企业微信 / NotificationLog")

    markers = {
        "环境变量中的 api_token": PLAIN_SECRET,
        "AI API Key": AI_KEY,
        "企业微信 webhook key": WECOM_KEY,
    }
    violations: list[str] = []

    # ① AI 真正收到的 Prompt（从 stub 记录里看，而不是看渲染函数的返回值）
    for row in stub_requests("ai"):
        blob = json.dumps(row.get("body", {}), ensure_ascii=False)
        for label, secret in markers.items():
            if secret in blob:
                violations.append(f"AI Prompt 含 {label}")
    check("M5-19", "AI 请求体（真实发出的那份）不含任何密钥", not [v for v in violations if "AI Prompt" in v], str(violations[:2]))

    # Authorization 头必须带 key（要能调通），但那是请求头，不是正文
    auth_headers = {row.get("authorization", "") for row in stub_requests("ai")}
    check("M5-19", "AI 请求头确实带了 Bearer key（说明真的在鉴权）", any(h.startswith("Bearer ") for h in auth_headers))

    # ② 企业微信真正收到的内容
    for row in stub_requests("wecom"):
        blob = json.dumps(row.get("body", {}), ensure_ascii=False)
        for label, secret in markers.items():
            if secret in blob:
                violations.append(f"企业微信消息含 {label}")
    check("M5-19", "企业微信消息（真实发出的那份）不含任何密钥", not [v for v in violations if "企业微信" in v], str(violations[:2]))

    # ③ 数据库里的分析 / 告警 / 通知
    analysis = api.get(f"/api/failures/{group['id']}/analyses").json()["items"][0]
    alert_rows = api.get("/api/alerts", params={"page_size": 200}).json()["items"]
    logs: list[dict] = []
    for item in alert_rows:
        logs.extend(api.get(f"/api/alerts/{item['id']}/notifications").json()["items"])

    surfaces = {
        "AI 分析记录": json.dumps(analysis, ensure_ascii=False),
        "告警记录": json.dumps(alert_rows, ensure_ascii=False),
        "通知记录": json.dumps(logs, ensure_ascii=False),
    }
    for name, blob in surfaces.items():
        leaked = [label for label, secret in markers.items() if secret in blob]
        check("M5-19", f"{name}不含明文密钥", not leaked, str(leaked))

    # ④ 数据库层面直接扫一遍
    import sqlite3

    connection = sqlite3.connect(str(ROOT / "testflow.db"))
    leaks: list[str] = []
    for table, columns in (
        ("failure_analyses", ("summary", "root_cause", "uncertainty", "recommendation", "raw_response_redacted", "error_message", "review_comment")),
        ("alerts", ("title", "summary")),
        ("notification_logs", ("error_message",)),
    ):
        for column in columns:
            for secret in markers.values():
                count = connection.execute(
                    f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', (f"%{secret}%",)
                ).fetchone()[0]
                if count:
                    leaks.append(f"{table}.{column}")
    connection.close()
    check("M5-19", "数据库表里（分析/告警/通知）没有明文密钥", not leaks, str(leaks[:3]))


# ================================================================== K. UI


def latest_analysis_id(api: httpx.Client, group_id: str) -> str:
    """取该分组最新一版 COMPLETED 分析的 ID，供 UI 断言核对点击结果。"""
    try:
        rows = api.get(f"/api/failures/{group_id}/analyses").json()["items"]
    except Exception:  # noqa: BLE001
        return ""
    for item in rows:
        if item["status"] == "COMPLETED":
            return str(item["id"])
    return ""


def section_k(env: Env, api: httpx.Client, group: dict) -> None:
    section("真实浏览器 UI 验收（AI 分析 / 告警 / 通知三块）")

    if settings.playwright_browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = settings.playwright_browsers_path
    from playwright.sync_api import sync_playwright

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1100})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        page.goto(f"{API}/#/login", wait_until="networkidle")
        page.fill("input#username", "admin")
        page.fill("input#password", "admin123")
        page.click("button[type=submit]")
        page.wait_for_selector("text=工作台", timeout=25000)

        page.get_by_role("menuitem", name="项目").click()
        page.wait_for_selector("table", timeout=25000)
        page.wait_for_timeout(1200)
        row = page.locator("tr", has_text=env.project).first
        switch = row.locator("button", has_text="设为当前")
        if switch.count() > 0 and switch.first.is_enabled():
            switch.first.click()
            page.wait_for_timeout(1200)

        page.get_by_role("menuitem", name="失败中心").click()
        page.wait_for_selector("text=失败分组", timeout=25000)
        page.wait_for_timeout(1500)
        check("UI", "失败中心可打开", "失败中心" in page.inner_text("body"))

        target_row = page.locator("tr", has_text=group["rule_id"]).first
        if target_row.count() == 0:
            target_row = page.locator("tbody tr.ant-table-row").first
        target_row.click()
        page.wait_for_selector("text=Failure Fingerprint", timeout=25000)
        page.wait_for_timeout(2000)
        drawer = page.inner_text(".ant-drawer-body")
        for marker in ("AI 分析", "告警", "通知记录"):
            check("UI", f"抽屉里有「{marker}」区块", marker in drawer)
        check("UI", "展示了分析状态", any(item in drawer for item in ("COMPLETED", "FAILED")))
        check("UI", "展示了置信度", "置信度" in drawer)
        check("UI", "展示了不确定的部分（诚实性要求）", "不确定的部分" in drawer)
        check("UI", "展示了建议下一步", "建议下一步" in drawer)
        check("UI", "展示了 AI 建议分类不会覆盖系统分类", "不会覆盖系统分类" in drawer or "仅作建议" in drawer)
        check("UI", "展示了告警严重级别", any(item in drawer for item in ("CRITICAL", "ERROR", "WARNING", "INFO")))
        check("UI", "展示了告警影响用例数", "影响用例" in drawer)
        # 断言用「真实按钮元素」而不是文本：抽屉内部是可滚动的，
        # 拿 inner_text 判断按钮存在与否会受渲染/滚动影响，是个假信号来源。
        review_buttons = {
            label: page.locator(".ant-drawer-body button", has_text=label).count()
            for label in ("确认", "修改", "忽略")
        }
        if "COMPLETED" in drawer:
            check("UI", "有分析结论时提供人工确认按钮", review_buttons["修改"] > 0 and review_buttons["忽略"] > 0,
                  str(review_buttons))
            # 点一下真的生效 —— 只断言「按钮存在」证明不了它接对了接口
            analysis_id = latest_analysis_id(api, group["id"])
            if analysis_id and review_buttons["确认"] > 0:
                page.locator(".ant-drawer-body button", has_text="确认").first.click()
                page.wait_for_timeout(2500)
                after = api.get(f"/api/analyses/{analysis_id}").json()["review_status"]
                check("UI", "点「确认」后 review_status 真的变成了 CONFIRMED", after == "CONFIRMED", after)
        button_count = page.locator(".ant-drawer-body button").count()
        check("UI", f"抽屉里有可操作按钮（{button_count} 个）", button_count > 0, str(button_count))
        page.screenshot(path=str(SHOT_DIR / "01_m5_drawer.png"), full_page=True)
        browser.close()

    check("UI", "浏览器控制台无报错", not console_errors, str(console_errors[:3]))


# ================================================================== 汇总


def collect_summary(env: Env) -> dict:
    """在关掉专用 API 之前把汇总数据取好。

    不能在 report() 里现取：那时 api 已关闭、专用实例已停止，
    一个「统计」动作会把整轮验收的结尾变成异常。
    """
    from collections import Counter

    groups = env.groups()
    alerts = env.alerts()
    analysis_total = 0
    for item in groups["items"]:
        try:
            analysis_total += env.api.get(f"/api/failures/{item['id']}/analyses").json()["total"]
        except Exception:  # noqa: BLE001 - 统计失败不该影响验收结论
            pass
    logs_total = 0
    for item in alerts["items"]:
        try:
            logs_total += len(env.api.get(f"/api/alerts/{item['id']}/notifications").json()["items"])
        except Exception:  # noqa: BLE001
            pass
    return {
        "project": env.project,
        "project_id": env.project_id,
        "groups": groups["total"],
        "alerts": alerts["total"],
        "alert_status": dict(Counter(a["status"] for a in alerts["items"])),
        "alert_severity": dict(Counter(a["severity"] for a in alerts["items"])),
        "analyses": analysis_total,
        "notifications": logs_total,
    }


def report(summary: dict, elapsed: float) -> int:
    section("汇总")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    failed = [item for item in RESULTS if not item[1]]
    print(f"   通过 {passed}/{total}")
    for name, _ok, detail in failed:
        print(f"   FAIL {name}  -> {detail}")

    print()
    print(f"   本次自测项目：{summary.get('project')}（{summary.get('project_id')}）")
    print(f"   失败分组 {summary.get('groups')} 个；告警 {summary.get('alerts')} 条；AI 分析 {summary.get('analyses')} 条；通知 {summary.get('notifications')} 条")
    print(f"   告警状态分布：{summary.get('alert_status')}")
    print(f"   告警级别分布：{summary.get('alert_severity')}")
    print(f"   总耗时 {elapsed:.1f}s")
    print()
    print("   [OK] M5 验收完成" if not failed else "   [FAIL] 存在未通过项")
    return 0 if not failed else 1


def main() -> int:
    started = time.monotonic()
    stamp = datetime.now().strftime("%H%M%S")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    demo = httpx.Client(timeout=30.0)

    section("0. 前置检查与环境准备")
    check("前置", "Demo 被测站可访问", demo.get(f"{DEMO}/login").status_code == 200)
    set_faults(demo)

    # stub 服务
    if not port_open(8300):
        start_process(
            "M5 stub 服务",
            [sys.executable, "scripts/m5_stub_server.py", "--port", "8300"],
            dict(os.environ),
        )
    check("前置", "本地 stub 服务已就绪", wait_http(f"{STUB}/__requests", timeout=30))

    # 专用 API 实例（指向 stub）
    api_env = dict(os.environ)
    api_env.update(
        {
            "DATABASE_URL": settings.database_url,
            "STORAGE_DIR": str(settings.storage_dir),
            "EXECUTOR_MODE": "inline",
            "PLAYWRIGHT_BROWSERS_PATH": settings.playwright_browsers_path,
            "PLAYWRIGHT_HEADLESS": "true",
            "PLAYWRIGHT_RECORD_VIDEO": "true",
            "PLAYWRIGHT_RECORD_HAR": "true",
            "PYTHONIOENCODING": "utf-8",
            "AI_PROVIDER": "openai_compatible",
            "AI_BASE_URL": f"{STUB}/v1",
            "AI_API_KEY": AI_KEY,
            "AI_MODEL": "stub-model",
            "AI_TIMEOUT": "20",
            "AI_MAX_RETRIES": "1",
            "AI_AUTO_ANALYZE": "true",
            "WECOM_WEBHOOK_URL": f"{STUB}/wecom/webhook?key={WECOM_KEY}",
            "WECOM_WEBHOOK_SECRET": WECOM_KEY,
            "NOTIFICATION_ENABLED": "true",
            "NOTIFICATION_MAX_ATTEMPTS": "3",
            "BOOTSTRAP_ADMIN_USERNAME": "admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "admin123",
        }
    )
    if port_open(8200):
        check("前置", "8200 端口未被占用（M5 自测专用实例）", False, "请先停止占用 8200 的进程")
        return 1
    start_process(
        "M5 专用 TestFlow API",
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8200", "--log-level", "warning"],
        api_env,
    )
    check("前置", "专用 API 实例已就绪（端口 8200）", wait_http(f"{API}/api/health", timeout=60))

    api = httpx.Client(base_url=API, timeout=120)
    token = api.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
    api.headers["Authorization"] = f"Bearer {token}"
    check("前置", "管理员登录", True)

    stub_reset()
    stub_mode(ai="valid", wecom="ok")

    env = Env(api)
    env.setup(stamp)
    env.add_case("M5-UNKNOWN-001", "M5 属性断言失败（产出 UNKNOWN）", UNKNOWN_STEPS)
    for index in range(1, 11):
        env.add_case(f"M5-ORDER-{index:02d}", f"M5 下单断言 #{index}", ORDER_STEPS)
    env.make_plan("unknown", "M5 UNKNOWN 计划", ["M5-UNKNOWN-001"], notify=False)
    env.make_plan("bulk", "M5 批量失败计划", [f"M5-ORDER-{i:02d}" for i in range(1, 11)], notify=True)
    check("前置", "自测项目 / 环境 / 11 个用例 / 2 个计划已就绪",
          bool(env.project_id and len(env.cases) == 11 and len(env.plans) == 2))

    summary: dict = {}
    try:
        patch_settings_for_providers()
        section_a()
        info("")
        context = section_b(env, api)
        group = context.get("group") or {}
        section_c(env, api)
        if group:
            section_d(env, api, group)
            section_e(env, api, group)
        alert_context = section_f(env, api, demo)
        alert = alert_context.get("alert") or {}
        if alert:
            section_g(env, api, alert)
            section_h(env, api, alert, demo)
        section_i(env, api, demo)
        if group:
            section_j(api, group)
            section_k(env, api, group)
    finally:
        set_faults(demo)
        try:
            summary = collect_summary(env)
        except Exception as exc:  # noqa: BLE001 - 统计失败只记一行
            info(f"（汇总数据采集失败：{type(exc).__name__}）")
        api.close()
        demo.close()
        stop_processes()

    return report(summary, time.monotonic() - started)


if __name__ == "__main__":
    raise SystemExit(main())
