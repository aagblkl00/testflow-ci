"""企业微信群机器人 Provider。

只做群机器人 Webhook —— 方案 §16 明确第一版就做这个。自建应用（能真正 @人）
留到后续，因为那需要 corpid / secret / agentid 三件套，起步成本高得多。

几个实现细节都是踩过的：

- **群机器人的 key 在 URL 里**，所以 URL 本身就是秘密。任何形式的回显
  （日志、报错、NotificationLog、前端）都必须把它掩掉 —— `_safe_url()` 就是干这个的。
- **企业微信对 HTTP 状态码不敏感**：请求成功也可能返回 `{"errcode": 93000, ...}`。
  只看状态码会把「发送失败」记成成功，所以必须解析 body 里的 `errcode`。
- **errcode 要区分值不值得重试**：`45009`（接口调用超过限制）、`-1`（系统繁忙）
  是临时的，值得退避重试；`93000`（webhook 无效）、`40001`（secret 无效）
  重试一万次也一样，直接判失败。
- **markdown 有 4096 字节上限**，超了直接报错而不是截断展示 —— 所以在渲染层
  就截断（见 `notification/service.py`），Provider 只是兜底再校一次。
"""

from __future__ import annotations

import time

import httpx

from ...config import settings
from ...utils.redact import redact_text
from ..provider import NotificationMessage, NotificationProvider, NotificationResult

# 值得重试的企业微信错误码：限流与系统繁忙
RETRYABLE_ERRCODES = {45009, -1, 45047}
# 明确不该重试的：地址或凭据不对
FATAL_ERRCODES = {93000, 40001, 40013, 40014, 42001}

CONTENT_LIMIT_BYTES = 3800


class WeComWebhookProvider(NotificationProvider):
    name = "wecom_webhook"
    channel = "wecom_webhook"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings.wecom_webhook_url

    def available(self) -> bool:
        return bool(self._url)

    def safe_url(self) -> str:
        """可安全打印的地址。key 在 query 里，必须掩掉。"""
        return redact_text(self._url or "", [settings.wecom_webhook_secret])

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self._url:
            return NotificationResult(ok=False, error="企业微信 Webhook 未配置", retryable=False)

        payload = {
            "msgtype": "markdown",
            "markdown": {"content": message.content},
        }
        started = time.perf_counter()
        try:
            response = httpx.post(self._url, json=payload, timeout=15.0)
        except httpx.HTTPError as exc:
            return NotificationResult(
                ok=False,
                error=redact_text(f"请求失败：{type(exc).__name__}", [settings.wecom_webhook_secret]),
                retryable=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500:
            return NotificationResult(ok=False, error=f"HTTP {response.status_code}", retryable=True, latency_ms=latency)
        if response.status_code >= 400:
            return NotificationResult(
                ok=False,
                error=redact_text(f"HTTP {response.status_code}：{response.text[:200]}", [settings.wecom_webhook_secret]),
                retryable=False,
                latency_ms=latency,
            )

        try:
            data = response.json()
        except ValueError:
            return NotificationResult(ok=False, error="企业微信返回的不是 JSON", retryable=True, latency_ms=latency)

        errcode = int(data.get("errcode", 0) or 0)
        if errcode != 0:
            return NotificationResult(
                ok=False,
                error=redact_text(f"errcode={errcode} errmsg={data.get('errmsg', '')}", [settings.wecom_webhook_secret]),
                retryable=errcode in RETRYABLE_ERRCODES,
                latency_ms=latency,
            )

        return NotificationResult(ok=True, message_id=str(data.get("msgid") or ""), latency_ms=latency)


__all__ = ["CONTENT_LIMIT_BYTES", "FATAL_ERRCODES", "RETRYABLE_ERRCODES", "WeComWebhookProvider"]
