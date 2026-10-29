"""通知层。

职责：**把通知送出去，并如实记录送成了没有。**

    notification/provider.py     渠道抽象（告警引擎不认识企业微信）
    notification/providers/*     具体渠道：wecom_webhook（真实）、fake（测试用）
    notification/service.py      渲染消息 → 去重 → 重试 → 写 notification_logs

边界很硬：这一层**不改任何测试结果、也不改告警状态**。
发送失败只落一行 `status=FAILED`，然后就没有然后了。
"""

from .provider import NotificationMessage, NotificationProvider, NotificationResult

# import 即注册内置渠道（wecom_webhook / fake）
from . import providers as _providers  # noqa: E402,F401

__all__ = ["NotificationMessage", "NotificationProvider", "NotificationResult", "_providers"]
