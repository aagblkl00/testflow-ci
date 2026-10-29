"""HTTP 客户端。

只做三件事：拼请求、判错误、把错误翻译成 CI 能理解的退出码。

Token 只从环境变量 `TESTFLOW_CI_TOKEN` 读，**刻意不提供 `--token` 参数**：
命令行参数会出现在 `ps`、shell history 和 CI 的 job log 里，
一个只该存在 5 分钟的密钥不该有这么多副本。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .output import ExitCode

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
TOKEN_ENV = "TESTFLOW_CI_TOKEN"
URL_ENV = "TESTFLOW_URL"


class CliError(RuntimeError):
    """CLI 层面的失败。exit_code 决定进程退出码，message 给人看。"""

    def __init__(self, message: str, *, exit_code: int = ExitCode.SYSTEM_ERROR) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def resolve_base_url(explicit: str = "") -> str:
    return (explicit or os.getenv(URL_ENV, "") or DEFAULT_BASE_URL).strip().rstrip("/")


def resolve_token(explicit: str = "") -> str:
    token = (explicit or os.getenv(TOKEN_ENV, "")).strip()
    if not token:
        raise CliError(
            f"缺少 CI Token：请设置环境变量 {TOKEN_ENV}（例如 export {TOKEN_ENV}=xxx）",
            exit_code=ExitCode.SYSTEM_ERROR,
        )
    return token


class CiClient:
    def __init__(self, base_url: str = "", token: str = "", *, timeout: float = 30.0) -> None:
        self.base_url = resolve_base_url(base_url)
        self.token = resolve_token(token)
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ------------------------------------------------------------- 生命周期

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CiClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- 请求

    def _headers(self) -> dict[str, str]:
        return {"X-TestFlow-Token": self.token}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise CliError(f"无法连接 TestFlowCI（{self.base_url}）：{type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise CliError(self._explain(response), exit_code=ExitCode.SYSTEM_ERROR)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise CliError(f"服务端返回的不是 JSON（HTTP {response.status_code}）") from exc
        return payload if isinstance(payload, dict) else {"items": payload}

    @staticmethod
    def _explain(response: httpx.Response) -> str:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or "")
        except ValueError:
            detail = response.text[:200]
        if response.status_code in {401, 403}:
            return f"CI Token 无效或缺失（HTTP {response.status_code}）：{detail}"
        if response.status_code == 503:
            return f"服务端未启用 CI 接口（HTTP 503）：{detail}"
        return f"请求失败（HTTP {response.status_code}）：{detail or '服务端未返回原因'}"

    # ---------------------------------------------------------------- 接口

    def create_run(
        self,
        *,
        plan: str,
        environment: str,
        project: str = "",
        base_url: str = "",
        source: str = "cli",
        pipeline_id: str = "",
        pipeline_url: str = "",
        commit_sha: str = "",
        branch: str = "",
        triggered_by: str = "",
    ) -> dict[str, Any]:
        payload = {
            "plan": plan,
            "environment": environment,
            "project": project,
            "base_url": base_url,
            "source": source,
            "pipeline_id": pipeline_id,
            "pipeline_url": pipeline_url,
            "commit_sha": commit_sha,
            "branch": branch,
            "triggered_by": triggered_by,
        }
        return self._request("POST", "/api/ci/runs", json={key: value for key, value in payload.items() if value})

    def ci_status(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/runs/{run_id}/ci-status")

    def meta(self) -> dict[str, Any]:
        """可解析的项目/计划/环境清单。报错时用它提示候选名。"""
        return self._request("GET", "/api/ci/meta")

    def candidates(self, kind: str, project: str = "", limit: int = 20) -> list[str]:
        """按类型取候选名，供错误信息使用。取不到就返回空，绝不因此再抛错。

        上限 20 个：CI 日志里刷出一百个计划名只会把真正的报错淹掉。
        """
        try:
            data = self.meta()
        except CliError:
            return []
        names: list[str] = []
        if kind == "project":
            names = [str(item.get("project") or "") for item in data.get("projects") or []]
        else:
            for item in data.get("projects") or []:
                if project and item.get("project") != project:
                    continue
                names.extend(str(entry.get("name") or "") for entry in item.get(kind) or [])
        unique = [name for name in dict.fromkeys(names) if name]
        return unique[:limit]


__all__ = ["CiClient", "CliError", "DEFAULT_BASE_URL", "TOKEN_ENV", "URL_ENV", "resolve_base_url", "resolve_token"]
