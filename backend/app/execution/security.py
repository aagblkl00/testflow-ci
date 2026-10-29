"""环境与请求安全门禁。

移植自 Lodestar（北极星）`backend/mission_engine.py` 中的安全校验部分，
去掉 mission/缺陷草稿等无关逻辑，仅保留执行链路必需的四道门：

1. 环境身份门禁  —— PROD 默认禁止执行
2. 域名白名单    —— 未登记域名一律拦截
3. 真实支付门禁  —— alipay / weixin pay / paypal / stripe / unionpay
4. 环境只读模式  —— read_only 环境下禁止 write / destructive 步骤
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

PRODUCTION_TOKENS = {"prod", "production", "online", "live", "正式", "生产", "prd", "release"}
REAL_PAYMENT_HOSTS = (
    "alipay.com",
    "pay.weixin.qq.com",
    "paypal.com",
    "stripe.com",
    "unionpay.com",
)
SAFE_PAYMENT_MARKERS = ("sandbox", "mock", "test", "staging", "qa", "dev")
NON_NETWORK_SCHEMES = {"about", "blob", "data", "chrome", "edge"}

HIGH_RISK_ACTIONS = {"write", "destructive"}


class ActionBlocked(RuntimeError):
    """安全策略在动作到达浏览器之前拦下了它。"""


def _identity_tokens(value: str) -> set[str]:
    normalized = str(value or "").lower()
    for separator in ("-", "_", "/", ":", " ", ".", "@"):
        normalized = normalized.replace(separator, " ")
    return {item for item in normalized.split() if item}


def is_real_payment_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    if not host or any(marker in host for marker in SAFE_PAYMENT_MARKERS):
        return False
    return any(host == item or host.endswith(f".{item}") for item in REAL_PAYMENT_HOSTS)


def host_is_allowed(host: str, allowed_hosts: list[Any]) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    for raw in allowed_hosts or []:
        allowed = str(raw or "").strip().lower().rstrip(".")
        if not allowed:
            continue
        if allowed.startswith("*.") and normalized.endswith(allowed[1:]) and normalized != allowed[2:]:
            return True
        if normalized == allowed:
            return True
    return False


def validate_request_target(value: str, environment: dict[str, Any]) -> dict[str, str] | None:
    """在 Playwright 真正发出请求之前做一次校验。返回 None 表示放行。"""
    parsed = urlparse(str(value or "").strip())
    scheme = parsed.scheme.lower()
    if scheme in NON_NETWORK_SCHEMES:
        return None
    if scheme not in {"http", "https", "ws", "wss"}:
        return {"code": "REQUEST_SCHEME_FORBIDDEN", "severity": "block", "message": f"已阻止非 HTTP(S) 请求：{scheme or 'unknown'}"}
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return {"code": "REQUEST_HOST_MISSING", "severity": "block", "message": "请求目标缺少有效域名"}
    if str(environment.get("environment_type") or "").upper() == "PROD":
        return {"code": "PRODUCTION_REQUEST_FORBIDDEN", "severity": "block", "message": f"生产环境默认禁止执行，已阻止：{host}"}
    if PRODUCTION_TOKENS.intersection(_identity_tokens(f"{host} {parsed.path}")):
        return {"code": "PRODUCTION_REQUEST_FORBIDDEN", "severity": "block", "message": f"已阻止疑似生产地址：{host}"}
    if is_real_payment_url(value):
        return {"code": "REAL_PAYMENT_REQUEST_FORBIDDEN", "severity": "block", "message": f"已阻止真实支付网关：{host}"}
    allowed_hosts = list(environment.get("allowed_hosts") or [])
    if not allowed_hosts or not host_is_allowed(host, allowed_hosts):
        return {"code": "REQUEST_HOST_NOT_ALLOWED", "severity": "block", "message": f"已阻止白名单外请求：{host}"}
    return None


def validate_environment(environment: dict[str, Any]) -> list[dict[str, str]]:
    """执行前对整套环境配置做一次体检。含 block 级别发现时禁止开跑。"""
    findings: list[dict[str, str]] = []
    base_url = str(environment.get("base_url") or "").strip()
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    environment_type = str(environment.get("environment_type") or "TEST").upper()

    if parsed.scheme not in {"http", "https"} or not host:
        findings.append({"code": "INVALID_BASE_URL", "severity": "block", "message": "环境必须配置完整的 http/https 地址"})
    if environment_type == "PROD":
        findings.append({"code": "PRODUCTION_FORBIDDEN", "severity": "block", "message": "生产环境（PROD）默认禁止执行；如确需执行请显式开启生产保护开关"})
    if PRODUCTION_TOKENS.intersection(_identity_tokens(f"{host} {parsed.path} {environment.get('name') or ''}")):
        findings.append({"code": "PRODUCTION_LOOKALIKE", "severity": "block", "message": "环境名称或地址带生产标识，已阻止执行"})
    allowed_hosts = list(environment.get("allowed_hosts") or [])
    if not allowed_hosts or not host_is_allowed(host, allowed_hosts):
        findings.append({"code": "HOST_NOT_REGISTERED", "severity": "block", "message": f"环境域名未登记到执行白名单：{host or '(空)'}"})
    if is_real_payment_url(base_url):
        findings.append({"code": "REAL_PAYMENT_FORBIDDEN", "severity": "block", "message": "检测到真实支付网关，所有环境均禁止执行"})
    if not findings:
        findings.append({"code": "ENVIRONMENT_READY", "severity": "info", "message": "环境身份、域名白名单与支付门禁检查通过"})
    return findings


def assert_environment_ready(environment: dict[str, Any]) -> None:
    blocked = [item for item in validate_environment(environment) if item.get("severity") == "block"]
    if blocked:
        raise ActionBlocked("；".join(item["message"] for item in blocked))


def assert_step_risk_allowed(step_risk: str, environment: dict[str, Any]) -> None:
    if str(environment.get("read_only") or "").lower() in {"1", "true", "yes"} or environment.get("read_only") is True:
        if str(step_risk or "read").lower() in HIGH_RISK_ACTIONS:
            raise ActionBlocked("当前环境为只读模式，已阻止高风险步骤")
