"""告警严重级别规则：确定性，不用 AI。

为什么严重级别不用 AI 判：级别决定「谁的手机半夜会响」。这种判断必须可解释、
可复现、可争论 —— 而「模型觉得这个挺严重」既不能复现也没法讲理。
规则写在这里，命中哪一条、为什么，都存进告警记录里给人事后核对。

规则按「影响面」而不是按「分类好听不好听」来定级：

    产品问题（影响大）        → CRITICAL
    产品问题（影响小）        → ERROR
    环境问题                  → ERROR（阻塞整批回归，但不一定是产品缺陷）
    证据不足                  → WARNING（需要人来看，但还不知道是不是问题）
    自动化 / 数据问题         → WARNING（多半是测试侧要修的）
    Flaky                     → INFO（本次没失败，只是不稳定信号）
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import FailureGroup

# 影响面阈值：产品问题影响到这个规模就升级为 CRITICAL
CRITICAL_CASE_THRESHOLD = 10
CRITICAL_RUN_THRESHOLD = 3

_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}


@dataclass(slots=True)
class SeverityDecision:
    severity: str
    rule_id: str
    reason: str


def decide_severity(group: FailureGroup) -> SeverityDecision:
    """按确定性规则给出严重级别。"""
    category = str(group.category or "UNKNOWN").upper()
    cases = int(group.failed_case_count or 0)
    runs = int(group.failed_run_count or 0)

    if category == "PRODUCT_BUG":
        if cases >= CRITICAL_CASE_THRESHOLD or runs >= CRITICAL_RUN_THRESHOLD:
            return SeverityDecision(
                "CRITICAL",
                "ALERT_PRODUCT_WIDE",
                f"产品问题且影响面大（{cases} 个用例 / {runs} 次执行，阈值 {CRITICAL_CASE_THRESHOLD} / {CRITICAL_RUN_THRESHOLD}）",
            )
        return SeverityDecision("ERROR", "ALERT_PRODUCT", "疑似产品问题")

    if category == "ENVIRONMENT_ERROR":
        return SeverityDecision("ERROR", "ALERT_ENVIRONMENT", "环境问题会阻塞整批回归")

    if category == "UNKNOWN":
        return SeverityDecision("WARNING", "ALERT_UNKNOWN", "证据不足，需要人工或 AI 进一步判断")

    if category in {"AUTOMATION_ERROR", "DATA_ERROR"}:
        return SeverityDecision("WARNING", "ALERT_TEST_SIDE", f"{category} 通常需要测试侧修复")

    if category == "FLAKY":
        return SeverityDecision("INFO", "ALERT_FLAKY", "Flaky 本次未失败，仅作为不稳定信号")

    return SeverityDecision("WARNING", "ALERT_UNCATEGORIZED", f"未覆盖的分类 {category}")


def escalate(current: str, incoming: str) -> str:
    """取两者中更高的级别。

    只升不降：一个问题从 CRITICAL 掉回 WARNING 只可能是因为这一轮影响的用例少了，
    但**它还没被修**。级别往下掉会让人以为好转了，这是最危险的假信号。
    真要降级，应当先解决告警、下次重新开一条。
    """
    current_level = _SEVERITY_ORDER.get(str(current or "").upper(), -1)
    incoming_level = _SEVERITY_ORDER.get(str(incoming or "").upper(), -1)
    return incoming if incoming_level > current_level else (current or incoming)


__all__ = [
    "CRITICAL_CASE_THRESHOLD",
    "CRITICAL_RUN_THRESHOLD",
    "SeverityDecision",
    "decide_severity",
    "escalate",
]
