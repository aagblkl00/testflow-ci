"""通知 Provider 实现集合。`import` 即完成注册。"""

from __future__ import annotations

from ..provider import register_provider
from .fake import FakeWeComProvider
from .wecom_webhook import WeComWebhookProvider

register_provider("wecom_webhook", WeComWebhookProvider)
register_provider("fake", FakeWeComProvider)

__all__ = ["FakeWeComProvider", "WeComWebhookProvider"]
