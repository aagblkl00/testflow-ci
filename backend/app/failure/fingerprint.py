"""Failure Fingerprint：把一次失败转成稳定、可比较的指纹。

必须是**确定性算法**，不涉及任何 AI —— 这是失败聚合能不能成立的前提。
同一段代码、同一份输入，永远算出同一个指纹。

策略选择顺序（先选证据最强的）：

    JS_ERROR   → 页面脚本异常，通常就是根因
    HTTP_ERROR → 有明确的 4xx/5xx 响应，可定位到具体接口
    TIMEOUT    → 再细分 element / navigation / network / assertion
    ELEMENT    → 定位器相关问题
    ASSERTION  → 值断言期望与实际不一致
    UNKNOWN    → 兜底，仍然出指纹（不能让失败漏掉）

输入优先级来自方案 §5。字段缺失不报错，缺什么就少用一个维度。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .normalize import (
    normalize_error,
    normalize_locator,
    normalize_text,
    normalize_url,
    page_key,
)
from ..utils.redact import redact_text

STRATEGY_JS_ERROR = "JS_ERROR"
STRATEGY_HTTP_ERROR = "HTTP_ERROR"
STRATEGY_TIMEOUT = "TIMEOUT"
STRATEGY_ELEMENT = "ELEMENT"
STRATEGY_ASSERTION = "ASSERTION"
STRATEGY_UNKNOWN = "UNKNOWN"

FAILURE_TYPE_BY_STRATEGY = {
    STRATEGY_JS_ERROR: "js_error",
    STRATEGY_HTTP_ERROR: "http_error",
    STRATEGY_TIMEOUT: "timeout",
    STRATEGY_ELEMENT: "element_failure",
    STRATEGY_ASSERTION: "assertion_failure",
    STRATEGY_UNKNOWN: "unknown",
}

# 超时细分：方案 §7 Strategy E 要求不能把所有超时归成一个 Timeout
TIMEOUT_KIND_ELEMENT = "element_timeout"
TIMEOUT_KIND_NAVIGATION = "navigation_timeout"
TIMEOUT_KIND_NETWORK = "network_timeout"
TIMEOUT_KIND_ASSERTION = "assertion_timeout"

_TIMEOUT_HINT = re.compile(r"(timeout|timed out|超时)", re.IGNORECASE)
_NAV_HINT = re.compile(r"(page\.goto|wait_for_url|waiting for navigation|navigation|navigating)", re.IGNORECASE)
_NET_HINT = re.compile(r"(net::err|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_TIMED_OUT|socket hang up)", re.IGNORECASE)
_LOCATOR_HINT = re.compile(
    r"(定位器|locator|waiting for get_by|元素不存在|元素在 \d+ms 内未|缺少有效定位器|未配置定位器|no element|not found)",
    re.IGNORECASE,
)
_ELEMENT_VISIBLE_HINT = re.compile(r"(元素仍|仍然可见|元素当前不可用|未变为可见)", re.IGNORECASE)


@dataclass(slots=True)
class FailureContext:
    """指纹计算的输入。字段全部为已归一化前的原始值。"""

    action: str = ""
    error: str = ""
    expected: str = ""
    actual: str = ""
    page_url: str = ""
    locator: Any = None
    request_method: str = ""
    request_url: str = ""
    http_status: int | None = None
    console_summary: str = ""
    network_summary: str = ""
    js_error: str = ""
    element_id: str = ""

    # 计算后的派生字段
    exception_type: str = ""
    message: str = ""
    strategy: str = ""
    failure_type: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class FingerprintResult:
    fingerprint: str
    strategy: str
    failure_type: str
    exception_type: str
    normalized_message: str
    normalized_expected: str
    normalized_actual: str
    page_url: str
    request_method: str
    request_url: str
    http_status: int | None
    locator: str
    step_action: str
    sample_message: str


def build_context(
    *,
    action: str = "",
    error: str = "",
    expected: str = "",
    actual: str = "",
    page_url: str = "",
    locator: Any = None,
    request_method: str = "",
    request_url: str = "",
    http_status: int | None = None,
    console_summary: str = "",
    network_summary: str = "",
    js_error: str = "",
    element_id: str = "",
) -> FailureContext:
    context = FailureContext(
        action=str(action or ""),
        error=str(error or ""),
        expected=str(expected or ""),
        actual=str(actual or ""),
        page_url=str(page_url or ""),
        locator=locator,
        request_method=str(request_method or ""),
        request_url=str(request_url or ""),
        http_status=int(http_status) if http_status else None,
        console_summary=str(console_summary or ""),
        network_summary=str(network_summary or ""),
        js_error=str(js_error or ""),
        element_id=str(element_id or ""),
    )
    context.exception_type, context.message = normalize_error(context.error)
    return context


def _pick_strategy(context: FailureContext) -> tuple[str, str]:
    """返回 (strategy, failure_type)。"""
    combined = f"{context.message} {context.error} {context.actual}"

    if context.js_error.strip():
        return STRATEGY_JS_ERROR, FAILURE_TYPE_BY_STRATEGY[STRATEGY_JS_ERROR]

    if context.http_status and context.http_status >= 400:
        return STRATEGY_HTTP_ERROR, FAILURE_TYPE_BY_STRATEGY[STRATEGY_HTTP_ERROR]

    if _TIMEOUT_HINT.search(combined):
        kind = _classify_timeout(context, combined)
        return STRATEGY_TIMEOUT, kind

    if _LOCATOR_HINT.search(combined):
        return STRATEGY_ELEMENT, FAILURE_TYPE_BY_STRATEGY[STRATEGY_ELEMENT]

    if context.action.startswith("assert_") and (context.expected.strip() or context.actual.strip()):
        return STRATEGY_ASSERTION, FAILURE_TYPE_BY_STRATEGY[STRATEGY_ASSERTION]

    # 断言类动作即使没有 expected/actual（例如单值断言），也归到 ASSERTION
    if context.action.startswith("assert_"):
        return STRATEGY_ASSERTION, FAILURE_TYPE_BY_STRATEGY[STRATEGY_ASSERTION]

    return STRATEGY_UNKNOWN, FAILURE_TYPE_BY_STRATEGY[STRATEGY_UNKNOWN]


def _classify_timeout(context: FailureContext, combined: str) -> str:
    if _NET_HINT.search(combined):
        return TIMEOUT_KIND_NETWORK
    if context.action.startswith("assert_"):
        return TIMEOUT_KIND_ASSERTION
    if context.action in {"open", "wait_url"} or _NAV_HINT.search(combined):
        return TIMEOUT_KIND_NAVIGATION
    if _ELEMENT_VISIBLE_HINT.search(combined) or _LOCATOR_HINT.search(combined):
        return TIMEOUT_KIND_ELEMENT
    return TIMEOUT_KIND_ELEMENT


def _strategy_payload(context: FailureContext) -> list[str]:
    """按策略组装参与哈希的字段。不同的失败场景用不同的维度，避免互相干扰。"""
    strategy = context.strategy
    if strategy == STRATEGY_HTTP_ERROR:
        return [
            context.request_method.upper(),
            normalize_url(context.request_url or context.page_url),
            str(context.http_status or ""),
            normalize_text(context.message)[:200],
        ]
    if strategy == STRATEGY_JS_ERROR:
        return [
            normalize_text(context.js_error)[:300],
            page_key(context.page_url),
        ]
    if strategy == STRATEGY_TIMEOUT:
        return [
            context.failure_type,
            context.action,
            normalize_locator(context.locator),
            page_key(context.page_url),
            normalize_text(context.message)[:200],
        ]
    if strategy == STRATEGY_ELEMENT:
        return [
            context.action,
            normalize_locator(context.locator) or normalize_text(context.element_id),
            page_key(context.page_url),
            normalize_text(context.message)[:200],
        ]
    if strategy == STRATEGY_ASSERTION:
        return [
            context.action,
            normalize_text(context.expected)[:200],
            normalize_text(context.actual)[:200],
            page_key(context.page_url),
        ]
    return [
        context.exception_type,
        normalize_text(context.message)[:300],
        page_key(context.page_url),
    ]


def _hash(payload: list[str]) -> str:
    canonical = "\x1f".join(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def compute(context: FailureContext) -> FingerprintResult:
    """计算指纹。同一输入永远得到同一结果。"""
    context.strategy, context.failure_type = _pick_strategy(context)
    digest = _hash([context.strategy, *_strategy_payload(context)])

    sample_source = context.error or context.message or context.actual
    return FingerprintResult(
        fingerprint=digest,
        strategy=context.strategy,
        failure_type=context.failure_type,
        exception_type=context.exception_type,
        normalized_message=normalize_text(context.message)[:2000],
        normalized_expected=normalize_text(context.expected)[:1000],
        normalized_actual=normalize_text(context.actual)[:1000],
        page_url=page_key(context.page_url)[:512],
        request_method=context.request_method.upper()[:16],
        request_url=normalize_url(context.request_url)[:1024],
        http_status=context.http_status,
        locator=normalize_locator(context.locator)[:512],
        step_action=context.action[:64],
        sample_message=redact_text(str(sample_source or ""))[:500],
    )


def build_title(result: FingerprintResult) -> str:
    """给人看的标题。同类问题应当给出同一句标题。"""
    action = result.step_action
    if result.strategy == STRATEGY_HTTP_ERROR:
        target = result.request_url or result.page_url or "(未知地址)"
        return f"{result.request_method or 'HTTP'} {target} 返回 {result.http_status}"
    if result.strategy == STRATEGY_JS_ERROR:
        return f"页面脚本异常：{result.normalized_message[:60] or result.page_url}"
    if result.strategy == STRATEGY_TIMEOUT:
        label = {
            TIMEOUT_KIND_NETWORK: "网络超时",
            TIMEOUT_KIND_NAVIGATION: "页面导航超时",
            TIMEOUT_KIND_ASSERTION: "断言等待超时",
            TIMEOUT_KIND_ELEMENT: "元素等待超时",
        }.get(result.failure_type, "超时")
        return f"{label}（{action or 'step'}）：{result.normalized_message[:60]}"
    if result.strategy == STRATEGY_ELEMENT:
        return f"元素定位失败（{action or 'step'}）：{result.locator or result.normalized_message[:60]}"
    if result.strategy == STRATEGY_ASSERTION:
        return f"断言失败（{action or 'step'}）：期望「{result.normalized_expected[:40]}」实际「{result.normalized_actual[:40]}」"
    return f"未分类失败：{result.normalized_message[:80] or result.exception_type or '未知'}"


__all__ = [
    "FailureContext",
    "FingerprintResult",
    "build_context",
    "compute",
    "build_title",
    "STRATEGY_JS_ERROR",
    "STRATEGY_HTTP_ERROR",
    "STRATEGY_TIMEOUT",
    "STRATEGY_ELEMENT",
    "STRATEGY_ASSERTION",
    "STRATEGY_UNKNOWN",
    "TIMEOUT_KIND_ELEMENT",
    "TIMEOUT_KIND_NAVIGATION",
    "TIMEOUT_KIND_NETWORK",
    "TIMEOUT_KIND_ASSERTION",
]
