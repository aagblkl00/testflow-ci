"""失败字符串归一化。

存在的唯一目的：让「同一个实际问题」在字符串层面变得可比。

    /order/10001  →  /order/{id}
    /order/10002  →  /order/{id}

归一化规则集中在这里，不散落到执行器或分类器里。所有函数都是纯函数，可单测。

设计取舍：
- **只替换明显动态的部分**，不做粗暴的全量清理。例如 URL query 只丢弃动态参数名，
  保留业务参数（丢掉 page 而保留 q，因为 q 不同往往是不同问题）。
- 位数阈值：ID 识别用「连续 4 位以上数字」，这样 HTTP 状态码（3 位）不会被误伤。
- 归一化前先做脱敏，确保 token / password 不会进入指纹。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# 4 位以上连续数字：3 位的 500/404 是状态码，必须保留
LONG_NUMBER_PATTERN = re.compile(r"\b\d{4,}\b")
HEX_TOKEN_PATTERN = re.compile(r"\b[0-9a-fA-F]{16,}\b")
BASE64ISH_PATTERN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
# 2026-09-19 / 2026-09-19 10:23:41 / 2026/09/19
DATETIME_PATTERN = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b")
# 绝对路径（含 Windows 盘符）
ABS_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"'<>|]{4,}")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
WHITESPACE_PATTERN = re.compile(r"\s+")
# Playwright 报错里的动态片段
PW_TIMEOUT_PATTERN = re.compile(r"Timeout (\d+)ms exceeded")
PW_WAITING_PATTERN = re.compile(r"waiting for (.+?)(?:\n|$)")
URL_IN_TEXT_PATTERN = re.compile(r"https?://[^\s\"'<>)]+")

# query 参数里明确属于动态值的名字，指纹计算时丢弃
DYNAMIC_QUERY_KEYS = {
    "_", "_t", "t", "ts", "time", "timestamp", "requestid", "request_id", "traceid", "trace_id",
    "nonce", "sign", "signature", "page", "pageindex", "page_index", "offset", "limit", "size",
    "rand", "random", "cache", "cachebuster", "cb", "sessionid", "session_id", "sid",
}

# 归一化占位符
PH_ID = "{id}"
PH_UUID = "{uuid}"
PH_TOKEN = "{token}"
PH_TS = "{ts}"
PH_TIME = "{time}"
PH_PATH = "{path}"


def normalize_url(value: str, *, keep_query: bool = True) -> str:
    """归一化 URL：路径里的 ID 变占位符，动态 query 参数丢弃。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return normalize_text(text)

    path = parts.path or ""
    # 逐段替换：/order/10001 → /order/{id}
    segments = []
    for segment in path.split("/"):
        if not segment:
            segments.append(segment)
            continue
        if UUID_PATTERN.fullmatch(segment):
            segments.append(PH_UUID)
        elif segment.isdigit() and len(segment) >= 4:
            segments.append(PH_ID)
        elif LONG_NUMBER_PATTERN.search(segment):
            segments.append(LONG_NUMBER_PATTERN.sub(PH_ID, segment))
        else:
            segments.append(segment)
    normalized_path = "/".join(segments)

    query = ""
    if keep_query and parts.query:
        kept = [
            (key, value if not LONG_NUMBER_PATTERN.search(value) else LONG_NUMBER_PATTERN.sub(PH_ID, value))
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in DYNAMIC_QUERY_KEYS
        ]
        kept.sort()
        query = urlencode(kept)

    netloc = (parts.hostname or "").lower()
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, normalized_path, query, ""))


def normalize_text(value: Any, *, strip_urls: bool = False) -> str:
    """归一化任意文本：脱敏占位、动态值占位、空白折叠。"""
    if value is None:
        return ""
    text = str(value)

    # 先脱敏：token / password 之类绝不能进指纹
    try:
        from ..utils.redact import redact_text  # noqa: PLC0415

        text = redact_text(text)
    except Exception:  # noqa: BLE001 - 脱敏失败也要能继续，不能因此丢指纹
        pass

    text = ANSI_PATTERN.sub("", text)
    text = UUID_PATTERN.sub(PH_UUID, text)
    text = DATETIME_PATTERN.sub(PH_TS, text)
    text = TIME_PATTERN.sub(PH_TIME, text)
    if strip_urls:
        text = URL_IN_TEXT_PATTERN.sub(lambda m: normalize_url(m.group(0)), text)
    text = ABS_PATH_PATTERN.sub(PH_PATH, text)
    text = HEX_TOKEN_PATTERN.sub(PH_TOKEN, text)
    text = BASE64ISH_PATTERN.sub(PH_TOKEN, text)
    text = LONG_NUMBER_PATTERN.sub(PH_ID, text)
    text = PW_TIMEOUT_PATTERN.sub(r"Timeout {ms}ms exceeded", text)
    text = WHITESPACE_PATTERN.sub(" ", text).strip()
    # 统一引号，避免中英文引号导致同一句话算成两个指纹
    for source, target in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
        text = text.replace(source, target)
    return text


def normalize_error(value: Any) -> tuple[str, str]:
    """拆分并归一化 `异常类型: 消息` 形式的错误串。

    执行器写库时统一格式为 `AssertionError: xxx`，这里把它拆开，
    让分类器可以只按异常类型走快路径。
    """
    text = normalize_text(value)
    if not text:
        return "", ""
    head, sep, tail = text.partition(":")
    if sep and head and " " not in head and len(head) <= 60:
        return head.strip(), tail.strip()
    return "", text


def normalize_locator(locator: Any) -> str:
    """归一化定位器描述。locator 可能是 dict（{"type","value"}）或字符串。"""
    if isinstance(locator, dict):
        kind = str(locator.get("type") or locator.get("locator_type") or "").strip()
        value = str(locator.get("value") or locator.get("locator_value") or "").strip()
        if not kind and not value:
            return ""
        return normalize_text(f"{kind}={value}")
    return normalize_text(locator)


def page_key(url: str) -> str:
    """页面的稳定标识：去掉 query，只保留归一化路径。"""
    normalized = normalize_url(url, keep_query=False)
    return normalized


def extract_http_status(text: str) -> int | None:
    """从文本里提取 HTTP 状态码，用于网络类失败。"""
    for status in (500, 502, 503, 504, 404, 403, 401, 429):
        if re.search(rf"(?<!\d){status}(?!\d)", str(text or "")):
            return status
    return None


__all__ = [
    "normalize_url",
    "normalize_text",
    "normalize_error",
    "normalize_locator",
    "page_key",
    "extract_http_status",
    "DYNAMIC_QUERY_KEYS",
    "PH_ID",
    "PH_UUID",
    "PH_TOKEN",
    "PH_TS",
    "PH_PATH",
]
