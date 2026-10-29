"""告警引擎。

职责：**什么时候应该通知。**

    FailureGroup（M3 的聚合结果）
        → rules.decide_severity    确定性定级（不用 AI）
        → service.upsert_alert     去重聚合：一个分组同时只有一条活动告警
        → service.transition       白名单状态机
        → service.reverify         复用 M2 失败用例重跑做重新验证
        → notification.service     交给通知层送出去

它不分析失败（那是 M3 与 AI 的事），也不负责怎么发消息（那是 notification 的事）。
"""

from .rules import decide_severity, escalate

__all__ = ["decide_severity", "escalate"]
