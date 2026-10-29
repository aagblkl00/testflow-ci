"""假 Provider —— **只给自动化测试用**。

为什么需要它（方案 §28）：M5 的绝大多数断言（幂等、review、Alert、通知、
脱敏）跟「模型聪不聪明」没有任何关系，但它们又必须真的跑一遍完整流程。
如果每跑一次测试都要连一次真实 AI，测试会变成又慢又不可靠的定时炸弹。

所以这里提供一个行为完全可控的替身，可以精确制造：
- 合法 JSON
- 非法 JSON（验证「一次修复重试后仍失败」这条路径）
- 直接报错 / 超时
- 空响应

它**只在测试与自测脚本里被显式选用**（`AI_PROVIDER=fake`），
生产默认是 `openai_compatible`。真实 Provider 的 smoke test 单独做，
不挂在 pytest 的必经路径上。
"""

from __future__ import annotations

import json
import os
import time

from ..provider import AIProvider, AIProviderError
from ..schemas import FailureAnalysisContext, ProviderResponse

VALID_PAYLOAD = {
    "summary": "订单创建接口返回 500，下单流程中断",
    "root_cause": "订单服务在提交订单时返回服务端错误，前端仅展示错误横幅，无法继续下单",
    "evidence": [
        "失败步骤为「提交订单」后的断言，元素 order-error 可见",
        "步骤现场捕获到 POST /orders 返回 500",
        "页面地址与前置登录步骤均正常，说明失败发生在提交环节",
    ],
    "related_commit": "abc1234",
    "suggested_owner": "建议先关注订单服务模块的近期变更（依据：失败集中在订单提交接口）",
    "confidence": 0.82,
    "uncertainty": "缺少服务端日志，无法确认是空指针还是依赖超时",
    "recommendation": "检查订单服务在本次提交前后的错误日志，并复现同样的下单请求",
    "ai_suggested_category": "PRODUCT_BUG",
}


class FakeAIProvider(AIProvider):
    """可编排的假 Provider。

    行为由构造参数或环境变量 `FAKE_AI_MODE` 决定：
      valid / invalid_json / error / timeout / empty / percent_confidence
    """

    name = "fake"
    supports_vision = False

    def __init__(self, mode: str | None = None) -> None:
        self.mode = (mode or os.getenv("FAKE_AI_MODE", "valid")).strip().lower()
        self.calls: list[str] = []

    @property
    def model(self) -> str:
        return "fake-model-1"

    def available(self) -> bool:
        # 假 Provider 永远可用：它的存在就是为了让测试不依赖外部配置
        return True

    def analyze_failure(self, context: FailureAnalysisContext, *, repair_hint: str = "") -> ProviderResponse:
        self.calls.append(context.failure_group_id)
        started = time.perf_counter()
        if self.mode == "error":
            raise AIProviderError("伪造的 Provider 故障", retryable=False)
        if self.mode == "timeout":
            raise AIProviderError("伪造的 Provider 超时", retryable=True)
        # repair_hint 刻意忽略：这样 invalid_json 模式下「修复重试」也必然失败，
        # 正好覆盖方案 §7 要求的「重试后仍失败 → status=FAILED」这条路径。
        if repair_hint and self.mode == "fixed_by_repair":
            raw = json.dumps(VALID_PAYLOAD, ensure_ascii=False)
            return ProviderResponse(
                ok=True, raw_text=raw, parsed=json.loads(raw), model=self.model,
                provider=self.name, latency_ms=0, attempts=2,
            )

        if self.mode == "empty":
            raw = ""
        elif self.mode == "invalid_json":
            raw = "抱歉，我无法按要求输出 JSON。我认为这可能是订单服务的问题。"
        elif self.mode == "percent_confidence":
            # 验证 confidence 归一化：模型回 82（百分数）应被收成 0.82
            raw = json.dumps({**VALID_PAYLOAD, "confidence": 82}, ensure_ascii=False)
        else:
            raw = json.dumps(VALID_PAYLOAD, ensure_ascii=False)

        return ProviderResponse(
            ok=True,
            raw_text=raw,
            parsed=None if self.mode in {"invalid_json", "empty"} else json.loads(raw) if raw else None,
            model=self.model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=1,
        )


__all__ = ["FakeAIProvider", "VALID_PAYLOAD"]
