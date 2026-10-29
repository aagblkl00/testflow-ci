"""CI 通道的 Token 认证。

与 Web 的 JWT 是两条独立通道，互不影响：
- Web 用户走 `Authorization: Bearer <jwt>`（`auth/deps.py`）
- CI 机器走 `X-TestFlow-Token: <TESTFLOW_CI_TOKEN>`

几个刻意的决定：

1. **Token 没配就等于接口关闭**。服务端没设 `TESTFLOW_CI_TOKEN` 时返回 503，
   而不是放行。否则「忘了配」会静默变成一个谁都能创建执行的开放入口。
2. **用 `compare_digest` 比较**，不用 `==`。字符串比较是短路的，
   攻击者能按字节把 Token 试出来。
3. **报错信息里不带 Token**，也不回显用户传错的值 —— 401 只说明「无效」。
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from ..config import settings

CI_TOKEN_HEADER = "X-TestFlow-Token"
GITLAB_TOKEN_HEADER = "X-Gitlab-Token"


def require_ci_token(
    x_testflow_token: str | None = Header(default=None, alias=CI_TOKEN_HEADER),
    x_gitlab_token: str | None = Header(default=None, alias=GITLAB_TOKEN_HEADER),
) -> str:
    """校验 CI Token。返回触发者标识（当前固定为 ci）。

    同时接受 `X-Gitlab-Token`：GitLab 的原生 Webhook 只会按它自己配置的
    「Secret token」发这个头。两处都是同一个密钥，不是两套凭据 ——
    这样既能让 CI Job 直接调，也能配置成 GitLab 原生 Webhook，不用维护两份秘密。
    """
    expected = settings.ci_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CI 接口未启用：服务端未配置 TESTFLOW_CI_TOKEN",
        )
    provided = x_testflow_token or x_gitlab_token
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"缺少 {CI_TOKEN_HEADER} 请求头",
        )
    if not hmac.compare_digest(str(provided), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="CI Token 无效")
    return "ci"


def ci_enabled() -> bool:
    return bool(settings.ci_token)


__all__ = ["CI_TOKEN_HEADER", "GITLAB_TOKEN_HEADER", "require_ci_token", "ci_enabled"]
