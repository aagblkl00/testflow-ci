from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "********"

SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "session",
    "credential",
    "private_key",
)

# 文本形式的敏感键：用于匹配 `password=xxx` 这类「键值对直接写在字符串里」的情况。
# 比 SENSITIVE_KEY_TOKENS 收敛 —— 那里用于字典键判定，可以宽；这里用于正则替换，
# 太宽的词（例如单独的 auth、session）会把正常业务文本也打码。
_SENSITIVE_VALUE_KEYS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api[-_]?key",
    "access[-_]?token",
    "refresh[-_]?token",
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "session[-_]?id",
    "credential",
    "private[-_]?key",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{6,}"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9._\-+/=]{6,}"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|api[-_]?key|access[-_]?token|refresh[-_]?token)\b\s*[:=]\s*([^\s,;\"']+)"), r"\1: " + MASK),
    # `password=xxx` / `"token": "xxx"` 这类「键值对直接写在文本里」的形式。
    # 前三条只覆盖 Authorization / Cookie / API Key，漏了 password 与裸 token ——
    # 而错误信息、console 输出、网络日志里最常出现的恰恰是这两种。
    # M3 指纹表会保存归一化后的错误文本，这里漏一次就等于把凭据写进指纹。
    (
        re.compile(
            r"(?i)\b(" + "|".join(_SENSITIVE_VALUE_KEYS) + r")\b\"?\s*[:=]\s*\"?([^\s,;\"'&]+)(\"?)"
        ),
        # 三个组分别是：键名、值、值后面的收尾引号。
        # 必须用 \3 而不是 \2 —— 值本身要丢掉，收尾引号要留着，
        # 否则 `{"token": "abc"}` 会被替换成 `{"token: ********abc"}`，
        # 明文凭着「掩码后面跟着半截真值」又漏了出去。
        r"\1: " + MASK + r"\3",
    ),
)


def is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").lower()
    if lowered in {"environment_type", "case_type", "content_type", "artifact_type", "trigger_type"}:
        return False
    return any(token in lowered for token in SENSITIVE_KEY_TOKENS)


def redact_mapping(value: Any) -> Any:
    """Recursively replace values whose key looks sensitive."""
    if isinstance(value, Mapping):
        return {key: (MASK if is_sensitive_key(str(key)) else redact_mapping(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item) for item in value]
    return value


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask credential-looking substrings, plus any literal secret values supplied by the caller."""
    if not text:
        return ""
    result = str(text)
    for secret in secrets:
        secret = str(secret or "").strip()
        if len(secret) < 3:
            continue
        result = result.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def collect_secrets(*mappings: Any) -> list[str]:
    """Gather every sensitive-looking leaf value so logs and prompts can mask them literally."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if is_sensitive_key(str(key)) and isinstance(item, (str, int, float)):
                    found.append(str(item))
                else:
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for mapping in mappings:
        walk(mapping)
    return found
