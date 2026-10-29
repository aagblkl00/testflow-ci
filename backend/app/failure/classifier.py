"""确定性失败分类。

**只有确定性规则，没有 AI。** 输出结构里刻意没有 confidence / probability ——
有置信度就会诱使人「按置信度排序」，那是在用概率掩盖证据不足。这里只有
`deterministic=True` 一种语义：命中了某条规则，且证据足以支持这个结论。

核心原则（方案 §13）：

    证据能确定性归因到什么程度，就分类到什么程度。

具体禁令：

    HTTP 500          ≠  PRODUCT_BUG
    断言失败           ≠  PRODUCT_BUG
    assert_not_visible ≠  PRODUCT_BUG
    HTTP 404          ≠  DATA_ERROR

规则顺序与方案 §14 的建议有两处**有意调整**，在此说明：

    方案建议：FLAKY → AUTOMATION → ENVIRONMENT → DATA → PRODUCT_BUG → UNKNOWN
    实际采用：FLAKY → ENVIRONMENT → DATA → 服务端5xx → JS异常 → AUTOMATION → PRODUCT_BUG → UNKNOWN

    底层原则是一条：**越接近根因的证据越优先。**

    调整一：`net::ERR_*`、`ECONNREFUSED`、网关 502-504 是基础设施层的强证据，
    排在「元素找不到」之前。否则「页面资源因 DNS 失败没加载出来、于是元素找不到」
    这种真实的环境故障会被误判成自动化脚本问题。

    调整二：服务端 5xx 与页面 JS 异常也排在 AUTOMATION 之前。页面拿了 500、
    或者自家脚本抛了异常，最常见的后果就是该渲染的元素没渲染出来 ——
    此时把锅甩给定位器是最容易犯、也最误导人的错，会把测试人员引向完全错误的方向。

    这两处调整不影响方案里的任何一条验收项（M3-04/05/06/07 的证据互斥，不会互相遮蔽）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORY_PRODUCT_BUG = "PRODUCT_BUG"
CATEGORY_ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
CATEGORY_AUTOMATION_ERROR = "AUTOMATION_ERROR"
CATEGORY_DATA_ERROR = "DATA_ERROR"
CATEGORY_FLAKY = "FLAKY"
CATEGORY_UNKNOWN = "UNKNOWN"

FAILURE_CATEGORIES = (
    CATEGORY_PRODUCT_BUG,
    CATEGORY_ENVIRONMENT_ERROR,
    CATEGORY_AUTOMATION_ERROR,
    CATEGORY_DATA_ERROR,
    CATEGORY_FLAKY,
    CATEGORY_UNKNOWN,
)


@dataclass(slots=True)
class FailureClassification:
    """分类结果。`rule_id` 是给人核对用的：结论必须能追溯到一条具体规则。"""

    category: str
    rule_id: str
    reason: str
    deterministic: bool = True
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "deterministic": self.deterministic,
            "evidence": list(self.evidence),
        }


# --------------------------------------------------------------------- 证据模式

# 基础设施 / 网络层：这些是强证据，出现即认为不是被测系统自身逻辑的问题
_ENV_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "ENV_CONNECTION_REFUSED",
        r"(ECONNREFUSED|ERR_CONNECTION_REFUSED|Connection refused|拒绝连接|连接被拒绝)",
        "目标环境拒绝连接",
    ),
    (
        "ENV_DNS_FAILURE",
        r"(ERR_NAME_NOT_RESOLVED|DNS_PROBE|EAI_AGAIN|ENOTFOUND|getaddrinfo|Name or service not known|DNS)",
        "域名解析失败",
    ),
    (
        "ENV_NETWORK_UNREACHABLE",
        r"(ENETUNREACH|EHOSTUNREACH|ERR_ADDRESS_UNREACHABLE|ERR_NETWORK_CHANGED|ERR_INTERNET_DISCONNECTED|network is unreachable)",
        "网络不可达",
    ),
    (
        "ENV_CONNECTION_RESET",
        r"(ECONNRESET|ERR_CONNECTION_RESET|ERR_CONNECTION_CLOSED|ERR_EMPTY_RESPONSE|socket hang up|连接被重置)",
        "连接被对端重置或中断",
    ),
    (
        "ENV_NETWORK_TIMEOUT",
        r"(ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT|ETIMEDOUT|read timed out|连接超时)",
        "网络连接超时",
    ),
)

# HTTP 网关层：502/503/504 是「上游/网关不可用」，与业务逻辑无关
_ENV_GATEWAY_STATUS = {502, 503, 504}

# 安全门禁：环境本身被策略拦下，属于配置问题，不是产品问题
_BLOCKED_PATTERNS = (
    "安全策略",
    "生产环境",
    "白名单",
    "只读模式",
    "支付网关",
)

# 数据层
_DATA_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "DATA_MISSING",
        r"(测试数据不存在|数据不存在|账号不存在|用户不存在|商品不存在|订单不存在|"
        r"前置数据缺失|缺少测试数据|无此数据|记录不存在|数据已删除)",
        "前置测试数据缺失或不可用",
    ),
    (
        "DATA_INVALID_CREDENTIALS",
        r"(账号或密码错误|用户名或密码错误|invalid credentials|密码不正确|登录失败)",
        "测试账号凭据无效",
    ),
)

# 自动化脚本自身的问题：这些是「怎么跑」的问题，不是「被测对象」的问题
_AUTOMATION_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "AUTO_LOCATOR_MISSING",
        r"(步骤未配置定位器|缺少有效定位器|未配置定位器|定位器为空)",
        "步骤没有配置定位器",
    ),
    (
        "AUTO_LOCATOR_SYNTAX",
        # 三种真实文案（在真实 Chromium 上实测过，不是猜的）：
        #   css   ':::'      → Unexpected token ":" while parsing css selector
        #   xpath '///bad('  → The string '///bad(' is not a valid XPath expression
        #   css   'div[['    → waiting for css=div[[ >> nth=0（同样是解析失败）
        r"(locator syntax|Unexpected token|is not a valid selector|is not a valid XPath|"
        r"Failed to execute 'evaluate'|InvalidSelectorError|定位器语法|parsing css selector)",
        "定位器语法错误",
    ),
    (
        "AUTO_ACTION_UNSUPPORTED",
        r"(未知动作|不支持的动作|unknown action|unsupported action)",
        "流程里存在执行器不支持的动作",
    ),
    (
        "AUTO_VARIABLE_MISSING",
        r"(变量未定义|未定义变量|undefined variable|Variable .* is not defined|缺少变量)",
        "流程引用了未定义的变量",
    ),
    (
        "AUTO_ASSERTION_CONFIG",
        r"(断言需要数字期望值|断言期望值.*不合法|期望值格式错误)",
        "断言期望值配置不合法",
    ),
    (
        "AUTO_ELEMENT_NOT_FOUND",
        r"(元素不存在|元素在 \d+ms 内未|waiting for (?:get_by|locator)|no element|元素不可见|未变为可见|元素仍)",
        "按定位器找不到元素（定位器可能已失效，需人工确认是否为功能异常）",
    ),
)

# 表现层：明确是服务端错误页 / 业务错误
_PRODUCT_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "PRODUCT_ERROR_PAGE",
        r"(Internal Server Error|服务器错误|系统异常|系统繁忙|服务异常|500 Internal)",
        "页面/响应呈现服务端异常",
    ),
)

_ASSERTION_ACTIONS = (
    "assert_text",
    "assert_equals",
    "assert_value",
    "assert_attribute",
    "assert_visible",
    "assert_not_visible",
    "assert_exists",
    "assert_not_exists",
    "assert_enabled",
    "assert_count",
    "assert_url",
    "assert_title",
)


def _search(patterns, *texts: str) -> tuple[str, str] | None:
    blob = " \n ".join(str(item or "") for item in texts)
    for rule_id, pattern, reason in patterns:
        if re.search(pattern, blob, re.IGNORECASE):
            return rule_id, reason
    return None


def _evidence_from(context) -> list[str]:
    """把支撑结论的现场片段摘出来，附在分类结果上。

    方案 §十三要求「不能强行判断」，所以每条结论都必须带着它依据的东西。
    """
    items: list[str] = []
    if context.http_status:
        items.append(f"HTTP {context.http_status} {context.request_method} {context.request_url}".strip())
    if context.js_error:
        items.append(f"页面脚本异常：{context.js_error[:180]}")
    if context.network_summary:
        lines = [line for line in str(context.network_summary).splitlines() if line.strip()][-3:]
        items.extend(f"网络：{line[:180]}" for line in lines)
    if context.expected or context.actual:
        items.append(f"期望「{str(context.expected)[:120]}」实际「{str(context.actual)[:120]}」")
    if context.error and not items:
        items.append(f"错误：{str(context.error)[:200]}")
    return items[:6]


def classify(
    context,
    *,
    is_flaky: bool = False,
    blocked: bool = False,
) -> FailureClassification:
    """对一次失败做确定性分类。

    Args:
        context: `failure.fingerprint.FailureContext`
        is_flaky: 该用例最终被判定为 FLAKY（FAIL → RETRY → PASS）。这是最强的
            一个信号：同一份代码、同一个用例，两次执行结果不同，说明失败本身
            不可复现。此时不允许被「第一次失败看起来像 PRODUCT_BUG」覆盖。
        blocked: 环境被安全门禁拦下。
    """
    evidence = _evidence_from(context)
    error = str(context.error or "")
    message = str(context.message or "")
    actual = str(context.actual or "")
    network = str(context.network_summary or "")
    console = str(context.console_summary or "")
    js_error = str(context.js_error or "")

    # ---- 1. FLAKY 优先（方案 §15）
    if is_flaky:
        return FailureClassification(
            category=CATEGORY_FLAKY,
            rule_id="FLAKY_ATTEMPT_SEQUENCE",
            reason="同一用例首次尝试失败、重试后通过，属于不可复现失败",
            evidence=evidence,
        )

    # ---- 2. 安全门禁：环境配置问题
    if blocked or any(marker in error for marker in _BLOCKED_PATTERNS):
        return FailureClassification(
            category=CATEGORY_ENVIRONMENT_ERROR,
            rule_id="ENV_SECURITY_GATE",
            reason="执行被环境安全策略拦下，属于环境/配置问题",
            evidence=evidence,
        )

    # ---- 3. 环境层强证据（网络异常 / 网关 5xx）
    hit = _search(_ENV_PATTERNS, error, message, network, actual)
    if hit is not None:
        rule_id, reason = hit
        return FailureClassification(category=CATEGORY_ENVIRONMENT_ERROR, rule_id=rule_id, reason=reason, evidence=evidence)

    if context.http_status in _ENV_GATEWAY_STATUS:
        return FailureClassification(
            category=CATEGORY_ENVIRONMENT_ERROR,
            rule_id="ENV_HTTP_GATEWAY_5XX",
            reason=f"网关/上游服务返回 {context.http_status}，不是业务逻辑本身的问题",
            evidence=evidence,
        )

    # ---- 4. 数据层
    hit = _search(_DATA_PATTERNS, error, message, actual)
    if hit is not None:
        rule_id, reason = hit
        return FailureClassification(category=CATEGORY_DATA_ERROR, rule_id=rule_id, reason=reason, evidence=evidence)

    # ---- 5. 业务接口 5xx：排在「元素找不到」之前
    # 页面拿了 500，最常见的后果就是该渲染的东西没渲染出来，于是下一步「元素找不到」。
    # 如果先判 AUTOMATION_ERROR，测试人员会被引去查定位器，而问题在服务端。
    # 这里已经排除了网关 5xx 与网络异常，剩下的 5xx 就是被测系统自己返回的。
    if context.http_status and 500 <= int(context.http_status) < 600:
        return FailureClassification(
            category=CATEGORY_PRODUCT_BUG,
            rule_id="PRODUCT_HTTP_5XX",
            reason=f"业务接口返回 {context.http_status}，且无环境层证据（非网关 5xx、无网络异常）",
            evidence=evidence,
        )

    # ---- 6. 页面脚本异常：同样优先于「元素找不到」
    # 前端脚本一抛异常，后面所有元素都找不到。把锅甩给定位器是最容易犯、
    # 也最误导人的错。此处已排除环境、数据、服务端三层证据。
    if js_error.strip():
        return FailureClassification(
            category=CATEGORY_PRODUCT_BUG,
            rule_id="PRODUCT_JS_ERROR",
            reason="页面自身脚本抛出未捕获异常，且无环境/数据/服务端证据",
            evidence=evidence,
        )

    # ---- 7. 自动化脚本自身
    hit = _search(_AUTOMATION_PATTERNS, error, message, console)
    if hit is not None:
        rule_id, reason = hit
        return FailureClassification(category=CATEGORY_AUTOMATION_ERROR, rule_id=rule_id, reason=reason, evidence=evidence)

    # ---- 8. 产品问题的文本证据（错误页文案）
    hit = _search(_PRODUCT_PATTERNS, error, message, actual)
    if hit is not None:
        rule_id, reason = hit
        return FailureClassification(category=CATEGORY_PRODUCT_BUG, rule_id=rule_id, reason=reason, evidence=evidence)

    if context.action in _ASSERTION_ACTIONS and str(context.expected).strip() and str(actual).strip():
        # 期望与实际都拿到了，且不相等（相等就不会失败），且没有环境/数据/自动化证据
        return FailureClassification(
            category=CATEGORY_PRODUCT_BUG,
            rule_id="PRODUCT_ASSERTION_MISMATCH",
            reason="断言拿到的实际值与明确预期不一致，且无环境/数据/自动化异常证据",
            evidence=evidence,
        )

    # ---- 9. 兜底：证据不足就承认证据不足
    return FailureClassification(
        category=CATEGORY_UNKNOWN,
        rule_id="UNKNOWN_INSUFFICIENT_EVIDENCE",
        reason="现有证据不足以确定性归因（留待 M5 AI 分析）",
        evidence=evidence,
    )


def describe_rules() -> list[dict[str, str]]:
    """把规则表暴露出来，便于 UI 展示与验收核对。"""
    rules: list[dict[str, str]] = [
        {"rule_id": "FLAKY_ATTEMPT_SEQUENCE", "category": CATEGORY_FLAKY, "reason": "首次失败、重试通过"},
        {"rule_id": "ENV_SECURITY_GATE", "category": CATEGORY_ENVIRONMENT_ERROR, "reason": "被环境安全策略拦下"},
    ]
    for rule_id, _pattern, reason in _ENV_PATTERNS:
        rules.append({"rule_id": rule_id, "category": CATEGORY_ENVIRONMENT_ERROR, "reason": reason})
    rules.append(
        {"rule_id": "ENV_HTTP_GATEWAY_5XX", "category": CATEGORY_ENVIRONMENT_ERROR, "reason": "网关/上游 502-504"}
    )
    for rule_id, _pattern, reason in _DATA_PATTERNS:
        rules.append({"rule_id": rule_id, "category": CATEGORY_DATA_ERROR, "reason": reason})
    for rule_id, _pattern, reason in _AUTOMATION_PATTERNS:
        rules.append({"rule_id": rule_id, "category": CATEGORY_AUTOMATION_ERROR, "reason": reason})
    rules.append(
        {"rule_id": "PRODUCT_JS_ERROR", "category": CATEGORY_PRODUCT_BUG, "reason": "页面自身脚本未捕获异常"}
    )
    rules.append({"rule_id": "PRODUCT_HTTP_5XX", "category": CATEGORY_PRODUCT_BUG, "reason": "业务接口 5xx"})
    for rule_id, _pattern, reason in _PRODUCT_PATTERNS:
        rules.append({"rule_id": rule_id, "category": CATEGORY_PRODUCT_BUG, "reason": reason})
    rules.append(
        {"rule_id": "PRODUCT_ASSERTION_MISMATCH", "category": CATEGORY_PRODUCT_BUG, "reason": "断言实际值与预期不一致"}
    )
    rules.append(
        {"rule_id": "UNKNOWN_INSUFFICIENT_EVIDENCE", "category": CATEGORY_UNKNOWN, "reason": "证据不足"}
    )
    return rules


__all__ = [
    "CATEGORY_PRODUCT_BUG",
    "CATEGORY_ENVIRONMENT_ERROR",
    "CATEGORY_AUTOMATION_ERROR",
    "CATEGORY_DATA_ERROR",
    "CATEGORY_FLAKY",
    "CATEGORY_UNKNOWN",
    "FAILURE_CATEGORIES",
    "FailureClassification",
    "classify",
    "describe_rules",
]
