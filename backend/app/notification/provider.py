"""通知 Provider 抽象。

**告警引擎不允许直接依赖企业微信 SDK**（方案 §15）。它只知道
「有一条告警要送出去」，至于送到企业微信、邮件还是飞书，是 Provider 的事。

契约：`send(message) -> NotificationResult`。
Provider 不写数据库、不碰 Alert 状态 —— 投递记录由 `service.py` 统一负责，
这样「送失败」和「记没记下来」才不会两处各写一半。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import settings


@dataclass(slots=True)
class NotificationMessage:
    """一条待发送的消息。已经渲染好、已经脱敏、已经截断。"""

    title: str
    content: str
    # 结构化附件字段，方便非文本渠道（邮件/飞书卡片）复用
    fields: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""


@dataclass(slots=True)
class NotificationResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    # 是否值得重试：4xx 参数错误重试一百次也一样，网络抖动才值得重试
    retryable: bool = False
    latency_ms: int = 0


class NotificationProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class NotificationProvider(ABC):
    name: str = "base"
    channel: str = "generic"

    def available(self) -> bool:
        return True

    @abstractmethod
    def send(self, message: NotificationMessage) -> NotificationResult:
        raise NotImplementedError


_REGISTRY: dict[str, Callable[[], NotificationProvider]] = {}
_CACHE: dict[str, NotificationProvider] = {}


def register_provider(name: str, factory: Callable[[], NotificationProvider]) -> None:
    _REGISTRY[name.lower()] = factory


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str | None = None) -> NotificationProvider:
    key = (name or default_provider_name()).strip().lower()
    if key in _CACHE:
        return _CACHE[key]
    if not _REGISTRY:
        # 兜底自动注册，理由同 AI Provider：不该因为调用方少 import 一个模块
        # 就得到一个「可用：无」的报错。
        import importlib  # noqa: PLC0415

        importlib.import_module(".providers", __package__)
    factory = _REGISTRY.get(key)
    if factory is None:
        raise NotificationProviderError(
            f"未知的通知渠道：{key}（可用：{', '.join(available_providers()) or '无'}）", retryable=False
        )
    provider = factory()
    _CACHE[key] = provider
    return provider


def reset_provider_cache() -> None:
    """测试用：改了配置后让缓存失效。"""
    _CACHE.clear()


def default_provider_name() -> str:
    return "wecom_webhook" if settings.wecom_webhook_url else "fake"


__all__ = [
    "NotificationMessage",
    "NotificationProvider",
    "NotificationProviderError",
    "NotificationResult",
    "available_providers",
    "default_provider_name",
    "get_provider",
    "register_provider",
    "reset_provider_cache",
]
