"""AI Provider 抽象与注册表。

为什么要这层抽象（方案 §3）：失败中心**不允许**直接依赖某家模型的 SDK。
今天用 OpenAI-compatible，明天可能换成自建网关或别的厂商；
只要 Provider 接口稳定，换实现就不需要动 `service.py` 和分析流程。

契约很简单：**给一个 `FailureAnalysisContext`，还一个 `ProviderResponse`。**
Provider 不写数据库、不知道 FailureGroup 是什么、不做幂等 ——
那些都是 `service.py` 的事。这样 Provider 可以被单独测试，
`FakeAIProvider` 也才能完全替身。

`supports_vision` 决定要不要把截图塞进去：不认识图片的模型收到 base64
只会浪费 token 并可能报错，所以默认 False，由实现自己声明。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..config import settings
from ..utils.redact import redact_text
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import FailureAnalysisContext, ProviderResponse


class AIProviderError(RuntimeError):
    """Provider 层的调用失败。带上「可重试」标记，交给 service 决定怎么办。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class AIProvider(ABC):
    """所有 Provider 的基类。"""

    name: str = "base"
    supports_vision: bool = False

    @property
    def model(self) -> str:
        return settings.ai_model

    def available(self) -> bool:
        """配置齐了没有。不齐时上层跳过而不是报错。"""
        return settings.ai_enabled

    def _redact(self, text: str) -> str:
        """错误信息里可能带上 key（某些网关会回显请求头），统一掩掉。"""
        return redact_text(str(text or ""), [settings.ai_api_key, settings.ai_base_url])

    @abstractmethod
    def analyze_failure(self, context: FailureAnalysisContext, *, repair_hint: str = "") -> ProviderResponse:
        """执行一次失败分析。

        `repair_hint` 非空时表示「上一次输出没法解析，请重新输出」——
        把「修复重试」当作 Provider 契约的一部分，而不是让 service 拼消息，
        因为「怎么把话说给这个模型听」本来就是 Provider 该管的事。

        失败时返回 ok=False 的响应或抛 AIProviderError。
        """
        raise NotImplementedError

    # 子类复用的消息构造：system + user，需要视觉时由子类自行加图片
    def build_messages(self, context: FailureAnalysisContext) -> list[dict[str, object]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(context)},
        ]


_REGISTRY: dict[str, Callable[[], AIProvider]] = {}
_CACHE: dict[str, AIProvider] = {}


def register_provider(name: str, factory: Callable[[], AIProvider]) -> None:
    _REGISTRY[name.lower()] = factory


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str | None = None) -> AIProvider:
    """按名字取 Provider（默认取配置里的）。

    实例做缓存：Provider 是无状态的，每次新建只是浪费。
    """
    key = (name or settings.ai_provider or "openai_compatible").strip().lower()
    if key in _CACHE:
        return _CACHE[key]
    if not _REGISTRY:
        # 兜底自动注册：直接 import 本模块（而不是 import 整个 ai 包）的人
        # 也应该能拿到 Provider，否则会得到一个「可用：无」这种毫无线索的报错。
        import importlib  # noqa: PLC0415

        importlib.import_module(".providers", __package__)
    factory = _REGISTRY.get(key)
    if factory is None:
        raise AIProviderError(
            f"未知的 AI Provider：{key}（可用：{', '.join(available_providers()) or '无'}）",
            retryable=False,
        )
    provider = factory()
    _CACHE[key] = provider
    return provider


def reset_provider_cache() -> None:
    """测试用：改了配置之后让缓存的 Provider 失效。"""
    _CACHE.clear()


__all__ = [
    "AIProvider",
    "AIProviderError",
    "available_providers",
    "get_provider",
    "register_provider",
    "reset_provider_cache",
]
