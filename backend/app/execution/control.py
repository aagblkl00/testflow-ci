"""执行停止控制通道（M2）。

为什么需要两条路径：

- **inline 模式**：API 与执行器在同一个进程。用一个 `threading.Event` 可以让
  「点击停止」立刻传导到执行器，延迟在毫秒级。
- **Celery 模式**：worker 是另一个进程，进程内 Event 传不过去。唯一可靠的跨进程信号
  是数据库字段 `execution_runs.cancel_requested`，执行器按 `poll_interval` 轮询。

两者取或，因此**同一套执行语义在两种模式下都成立**：
API 层调 `request_stop()` 时既置位进程内 Event，也（由调用方）写库，
执行器只要问 `should_stop()` 就行，不需要知道自己跑在哪种模式里。

诚实说明停止粒度：
- 步骤边界：立即生效；
- `wait` 步骤：按 200ms 切片轮询，延迟 < 切片长度；
- 单个 locator 操作 / 页面导航**正在进行中**无法安全中断（Playwright 同步对象不能跨线程调用，
  强行 close 会破坏协议状态），会在该操作返回后立刻生效，上限为该步骤配置的 timeout。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("testflow.control")


class RunControl:
    """一次执行的停止控制器。"""

    def __init__(self, run_id: str, poll_interval: float = 0.4) -> None:
        self.run_id = run_id
        self._event = threading.Event()
        self._poll_interval = max(0.05, float(poll_interval))
        self._checker: Callable[[], bool] | None = None
        self._next_poll = 0.0
        self._db_flag = False
        self.requested_at: float | None = None
        self.requested_by: str = ""

    # ------------------------------------------------------------ 由 API 层调用

    def request_stop(self, actor: str = "") -> None:
        if self.requested_at is None:
            self.requested_at = time.monotonic()
            self.requested_by = actor
        self._event.set()

    # ------------------------------------------------------------ 由执行器调用

    def bind_db_checker(self, checker: Callable[[], bool] | None) -> None:
        """Celery 模式下由调用方注入「读库里的 cancel_requested」的函数。"""
        self._checker = checker

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def should_stop(self) -> bool:
        if self._event.is_set():
            return True
        if self._checker is None:
            return False
        now = time.monotonic()
        if now < self._next_poll:
            return False
        self._next_poll = now + self._poll_interval
        try:
            self._db_flag = bool(self._checker())
        except Exception as exc:  # noqa: BLE001 - 读库失败不能反过来中断执行
            logger.warning("读取停止标志失败：%s", exc)
            self._db_flag = False
        if self._db_flag:
            if self.requested_at is None:
                self.requested_at = time.monotonic()
            self._event.set()
            return True
        return False

    def latency_ms(self) -> int:
        if self.requested_at is None:
            return 0
        return int((time.monotonic() - self.requested_at) * 1000)


# ---------------------------------------------------------------- 进程内注册表

_REGISTRY: dict[str, RunControl] = {}
_LOCK = threading.Lock()


def register(run_id: str, poll_interval: float = 0.4) -> RunControl:
    control = RunControl(run_id, poll_interval=poll_interval)
    with _LOCK:
        _REGISTRY[run_id] = control
    return control


def get(run_id: str) -> RunControl | None:
    with _LOCK:
        return _REGISTRY.get(run_id)


def unregister(run_id: str) -> None:
    with _LOCK:
        _REGISTRY.pop(run_id, None)


def active_run_ids() -> list[str]:
    with _LOCK:
        return list(_REGISTRY.keys())


def request_stop(run_id: str, actor: str = "") -> bool:
    """通知执行器停止。返回 True 表示找到了同进程内的活跃执行器（即时生效）。

    返回 False 不代表停止失败 —— 说明执行在别的进程（Celery worker），
    由调用方写库 `cancel_requested=True`，执行器会在下一次轮询时看到。
    """
    control = get(run_id)
    if control is None:
        return False
    control.request_stop(actor=actor or "api")
    return True


__all__ = [
    "RunControl",
    "register",
    "get",
    "unregister",
    "active_run_ids",
    "request_stop",
]
