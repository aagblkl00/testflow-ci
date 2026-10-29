"""假通知 Provider —— **只给自动化测试用**。

方案 §29 要求测试不能依赖真实企业微信。这个替身可以精确制造：
成功、失败（可重试）、失败（不可重试）、超时。

它还记录下每一次收到的消息内容 —— 这点很重要：**脱敏断言必须检查
「真正被送出去的那份内容」**，而不是检查渲染函数的返回值。
只测渲染函数的话，「Provider 里又拼了一版带明文的文案」这种问题就漏了。
"""

from __future__ import annotations

import os
import time

from ..provider import NotificationMessage, NotificationProvider, NotificationResult


class FakeWeComProvider(NotificationProvider):
    name = "fake"
    channel = "wecom_webhook"

    def __init__(self, mode: str | None = None) -> None:
        # ok / fail_then_ok / fail / fatal / timeout
        self.mode = (mode or os.getenv("FAKE_WECOM_MODE", "ok")).strip().lower()
        self.sent: list[NotificationMessage] = []
        self.attempts = 0

    def available(self) -> bool:
        return True

    def send(self, message: NotificationMessage) -> NotificationResult:
        self.attempts += 1
        started = time.perf_counter()
        if self.mode == "timeout":
            return NotificationResult(ok=False, error="伪造的超时", retryable=True, latency_ms=1)
        if self.mode == "fatal":
            return NotificationResult(ok=False, error="errcode=93000 webhook 无效", retryable=False, latency_ms=1)
        if self.mode == "fail":
            return NotificationResult(ok=False, error="伪造的发送失败", retryable=True, latency_ms=1)
        if self.mode == "fail_then_ok" and self.attempts == 1:
            return NotificationResult(ok=False, error="伪造的首次失败", retryable=True, latency_ms=1)

        self.sent.append(message)
        return NotificationResult(
            ok=True, message_id=f"fake-msg-{len(self.sent)}", latency_ms=int((time.perf_counter() - started) * 1000)
        )


__all__ = ["FakeWeComProvider"]
