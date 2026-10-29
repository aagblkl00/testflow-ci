"""M6-A：Artifact 内容的短时签名 URL。

背景：浏览器里的 `<img>` / `<video>` / `<a href>` 不会自动携带 Axios 的
`Authorization: Bearer` Header。为了关闭无鉴权的 `/storage` 静态目录，又不能让
前端重写成 Blob 下载，这里用项目已有的 `TESTFLOW_SECRET` 生成短时 HMAC 签名。

签名绑定 `artifact_id`、`disposition`、`expiration`。服务端校验通过后仍然会：
1. 按 artifact_id 查库；
2. 对 Artifact.path 做 storage 目录约束；
所以签名过期或被篡改会立即失效，也不会把 JWT 放进 URL。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from ..config import settings


def _sign(payload: str) -> str:
    return hmac.new(settings.secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def build_artifact_access(artifact_id: str, disposition: str, *, now: int | None = None) -> tuple[int, str]:
    """返回 `(expiration, signature)`。"""
    exp = int(now if now is not None else time.time()) + int(settings.artifact_url_ttl_seconds)
    payload = f"{artifact_id}.{disposition}.{exp}"
    return exp, _sign(payload)


def build_artifact_url(artifact_id: str, disposition: str, *, now: int | None = None) -> str:
    """生成可被 `<img>` / `<video>` / `<a href>` 直接请求的 content URL。"""
    exp, sig = build_artifact_access(artifact_id, disposition, now=now)
    return f"/api/artifacts/{artifact_id}/content?exp={exp}&disposition={disposition}&sig={sig}"


def verify_artifact_access(artifact_id: str, disposition: str, exp: str | None, sig: str | None) -> bool:
    if not exp or not sig:
        return False
    try:
        expiration = int(exp)
    except (TypeError, ValueError):
        return False
    if expiration < int(time.time()):
        return False
    payload = f"{artifact_id}.{disposition}.{expiration}"
    return hmac.compare_digest(_sign(payload), sig)


__all__ = ["build_artifact_access", "build_artifact_url", "verify_artifact_access"]
