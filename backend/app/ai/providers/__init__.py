"""Provider 实现集合。

`import` 这个包即完成注册 —— `provider.get_provider()` 靠的是这里。
"""

from __future__ import annotations

from .fake import FakeAIProvider
from .openai_compatible import OpenAICompatibleProvider
from ..provider import register_provider

register_provider("openai_compatible", OpenAICompatibleProvider)
register_provider("openai", OpenAICompatibleProvider)
register_provider("fake", FakeAIProvider)

__all__ = ["FakeAIProvider", "OpenAICompatibleProvider"]
