"""CI 接口的出入参。

出参全部用显式 dict 由 `ci_service.ci_status_payload` 构造（JSON schema 稳定、
字段只增不改），请求体用下面这几个模型校验。

支持 `project` 与 `project_id`、`plan` 与 `plan_id`、`environment` 与 `environment_id`
两组写法：方案 §9 用的是 `*_id`，§11 的 GitLab 载荷用的是短名。两种都收，
免得调用方为了一个字段名调试半天。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CiRunRequest(BaseModel):
    project: str | None = None
    project_id: str | None = None
    plan: str | None = None
    plan_id: str | None = None
    environment: str | None = None
    environment_id: str | None = None
    # ci / cli / gitlab —— 只用来决定 Run 的 trigger_source，不参与鉴权
    source: str = "ci"
    pipeline_id: str = ""
    pipeline_url: str = ""
    commit_sha: str = ""
    branch: str = ""
    triggered_by: str = ""
    # 可选：只覆盖本次执行的 base_url。仍要过安全门禁。
    base_url: str = ""

    def project_ref(self) -> str:
        return str(self.project or self.project_id or "").strip()

    def plan_ref(self) -> str:
        return str(self.plan or self.plan_id or "").strip()

    def environment_ref(self) -> str:
        return str(self.environment or self.environment_id or "").strip()


class CiRunResponse(BaseModel):
    """创建执行的响应。复用 CI 视图，额外告诉调用方本次是新建还是幂等命中。"""

    run_id: str
    status: str
    created: bool = Field(description="true=新建；false=幂等命中已有 Run")
    idempotent_hit: bool = False
    exit_code: int | None = None
    ci_status_url: str = ""


__all__ = ["CiRunRequest", "CiRunResponse"]
