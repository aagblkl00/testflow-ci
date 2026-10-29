"""OpenAI-compatible Provider。

只依赖 `httpx`（项目已有），不引入任何厂商 SDK。任何兼容
`POST /chat/completions` 的服务都能接：OpenAI 官方、Azure OpenAI 代理、
自建网关、以及各家兼容层。

几个实际会踩到的点，都在这儿处理了：

- **`response_format` 不是所有服务都支持**。带上 `json_object` 能显著提高
  JSON 合法率，但部分网关会直接 400。所以遇到「和 response_format 相关的 400」
  会自动摘掉它重试一次 —— 宁可少一层保障，也不能因为服务商不支持就整个功能不可用。
- **模型爱包 markdown 代码块**。即使要求了纯 JSON，` ```json ` 依然常见，
  所以解析前先剥壳。
- **错误信息要脱敏**。某些网关会把请求头回显到错误里，那把 API Key 也带出来了。
  所有错误文本过一遍 `_redact`。
- **重试只针对「值得重试」的错**。超时、连接失败、5xx、429 重试；
  400/401/403 不重试 —— 那是参数或凭据问题，重试一百次也一样。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ...config import settings
from ..prompts import SYSTEM_PROMPT, build_user_prompt
from ..provider import AIProvider, AIProviderError
from ..schemas import FailureAnalysisContext, ProviderResponse

DEFAULT_BASE_URL = "https://api.openai.com/v1"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def strip_code_fence(text: str) -> str:
    """剥掉 ```json ... ``` 外壳。模型十次里有三次会包上。"""
    cleaned = str(text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def try_parse_json(text: str) -> dict[str, Any] | None:
    """尽量把文本解析成 JSON 对象。解析不出来返回 None，由上层决定是否修复重试。"""
    cleaned = strip_code_fence(text)
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # 再退一步：截取第一个 { 到最后一个 } 之间的内容
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


class OpenAICompatibleProvider(AIProvider):
    name = "openai_compatible"
    supports_vision = True

    @property
    def base_url(self) -> str:
        return (settings.ai_base_url or DEFAULT_BASE_URL).rstrip("/")

    def _payload(
        self, context: FailureAnalysisContext, *, with_response_format: bool, repair_hint: str = ""
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_content(context)},
        ]
        if repair_hint:
            messages.append({"role": "user", "content": repair_hint})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # 失败分析要的是稳定复现的判断，不是创意写作
            "temperature": 0.1,
            "max_tokens": settings.ai_max_tokens,
        }
        if with_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _user_content(self, context: FailureAnalysisContext) -> Any:
        """有截图且自己也支持视觉时，用多模态消息；否则就是纯文本。"""
        text = build_user_prompt(context)
        if not (self.supports_vision and context.images):
            return text
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in context.images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.media_type};base64,{image.data_base64}"},
                }
            )
        parts.append({"type": "text", "text": "（截图见上，仅作辅助）"})
        return parts

    def analyze_failure(self, context: FailureAnalysisContext, *, repair_hint: str = "") -> ProviderResponse:
        if not self.available():
            raise AIProviderError("AI 未配置（缺少 AI_API_KEY 或 AI_MODEL）", retryable=False)

        started = time.perf_counter()
        last_error = ""
        attempts = 0
        with_response_format = True
        max_attempts = 1 + max(0, int(settings.ai_max_retries))

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.ai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self._payload(context, with_response_format=with_response_format, repair_hint=repair_hint),
                    timeout=settings.ai_timeout,
                )
            except httpx.HTTPError as exc:
                last_error = f"请求失败：{type(exc).__name__}"
                if attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                break

            if response.status_code >= 400:
                body = self._redact(response.text[:500])
                last_error = f"HTTP {response.status_code}：{body}"
                # 服务商不支持 response_format 时摘掉它重试，这不算「调用失败」
                if response.status_code == 400 and with_response_format:
                    lowered = response.text.lower()
                    if "response_format" in lowered or "json_object" in lowered:
                        with_response_format = False
                        continue
                if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                break

            raw_text = self._extract_text(response)
            return ProviderResponse(
                ok=True,
                raw_text=raw_text,
                parsed=try_parse_json(raw_text),
                model=self.model,
                provider=self.name,
                latency_ms=int((time.perf_counter() - started) * 1000),
                attempts=attempt,
            )

        return ProviderResponse(
            ok=False,
            raw_text="",
            parsed=None,
            model=self.model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts,
            error=self._redact(last_error) or "未知错误",
        )

    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text or ""
        try:
            choices = data.get("choices") or []
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # 部分网关返回分段 content
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return str(content or "")
        except (AttributeError, IndexError, TypeError):
            return json.dumps(data, ensure_ascii=False)[:2000]


__all__ = ["DEFAULT_BASE_URL", "OpenAICompatibleProvider", "RETRYABLE_STATUS", "strip_code_fence", "try_parse_json"]
