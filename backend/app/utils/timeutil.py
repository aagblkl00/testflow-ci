"""时间工具。

存在的唯一原因是一个很容易踩的坑：

**SQLite 的 `DateTime(timezone=True)` 在读回来之后会丢掉时区。**
写进去时是 `datetime(..., tzinfo=utc)`，`db.refresh()` 或换一个 Session 读出来
就变成 naive 了。于是「写入时刻」是 aware、「读取时刻」是 naive，
两者相减直接抛 `TypeError: can't subtract offset-naive and offset-aware datetimes`。

M2 的执行编排之所以一直没事，是因为它在同一个 Session 里设完 `started_at`
紧接着算 `finished_at`，中间没有 refresh —— 属于**侥幸**而不是设计。
M5 的 AI 分析要先 commit（让记录对用户可见）再算耗时，立刻就撞上了。

所以耗时计算统一走这里：`elapsed_ms(perf_start)` 用单调时钟，
跟数据库字段完全解耦；需要比较数据库时间时用 `ensure_utc()`。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """把可能 naive 的时间补成 UTC aware。naive 一律按 UTC 解释 ——
    库里存的就是 UTC，只是 SQLite 不帮我们保留 tzinfo。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def elapsed_ms(started: float) -> int:
    """从 `time.perf_counter()` 的起点算到现在的毫秒数。

    这是算耗时的**首选**方式：单调、不受系统时间调整影响、
    也不依赖数据库字段的时区是否完好。
    """
    return max(0, int((time.perf_counter() - started) * 1000))


def delta_ms(start: datetime | None, end: datetime | None) -> int:
    """两个数据库时间之间的毫秒差。任一侧缺失或类型不对都返回 0，不抛异常。

    只有在拿不到单调时钟（例如事后补算历史记录）时才用它。
    """
    left, right = ensure_utc(start), ensure_utc(end)
    if left is None or right is None:
        return 0
    try:
        return max(0, int((right - left).total_seconds() * 1000))
    except TypeError:  # pragma: no cover - ensure_utc 之后不该发生
        return 0


def is_within(value: datetime | None, window_seconds: float) -> bool:
    """某个数据库时间是否在「距今 window_seconds 秒内」。"""
    moment = ensure_utc(value)
    if moment is None:
        return False
    return (utcnow() - moment).total_seconds() <= window_seconds


__all__ = ["delta_ms", "elapsed_ms", "ensure_utc", "is_within", "utcnow"]
