"""GitLab 触发载荷解析。

要同时吃两种真实来源，因为这两种用法在实际 CI 里都会出现：

**A. GitLab 原生 Pipeline Webhook**（项目设置 → Webhooks → Pipeline events）

    {"object_kind": "pipeline",
     "object_attributes": {"id": 182, "ref": "main", "sha": "abc123",
                           "url": "https://gitlab.example/group/app/-/pipelines/182"},
     "project": {"id": 12, "path_with_namespace": "group/app", "web_url": "https://gitlab.example/group/app"},
     "user": {"username": "zhangsan"}}

  Webhook 载荷里**没有**「跑哪个计划、跑哪个环境」——这是 GitLab 不知道的事。
  所以这个来源需要额外提供 `plan` 与 `environment`（可以放在 pipeline 变量里，
  GitLab 会在 `object_attributes.variables` 里带出来）。

**B. CI Job 直接调用**（`.gitlab-ci.yml` 里用 curl / CLI）

    {"project": "AI Shop", "plan": "smoke", "environment": "test",
     "pipeline_id": "182", "pipeline_url": "$CI_PIPELINE_URL",
     "commit_sha": "$CI_COMMIT_SHA", "branch": "$CI_COMMIT_REF_NAME"}

  这条路最可控：GitLab 会注入全部 CI_* 变量，Job 原样转发即可。

解析器把两者归一化成同一个结构。**不做 GitLab 项目 → TestFlowCI 项目的映射表**：
那需要一张映射表和一个维护入口，而 CI Job 里本来就知道要跑哪个项目，
多这张表只是给运维加负担。Webhook 场景用 path_with_namespace / name 当项目名去匹配，
匹配不到就明确报 4xx 让调用方显式传 project。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .service import CiRequestError

# 只认这两种事件：pipeline 事件（有 object_attributes）与直接调用
KNOWN_EVENTS = ("pipeline", "job", "push", "unknown")


@dataclass(slots=True)
class GitlabTrigger:
    project_ref: str
    plan_ref: str
    environment_ref: str
    pipeline_id: str
    pipeline_url: str
    commit_sha: str
    branch: str
    user: str
    event: str

    def as_dict(self) -> dict[str, str]:
        return {
            "project": self.project_ref,
            "plan": self.plan_ref,
            "environment": self.environment_ref,
            "pipeline_id": self.pipeline_id,
            "pipeline_url": self.pipeline_url,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "user": self.user,
            "event": self.event,
        }


def _pick(*values: Any) -> str:
    """取第一个非空值并转成字符串。"""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _pipeline_variables(attributes: dict[str, Any]) -> dict[str, str]:
    """GitLab pipeline 事件里的 variables 是 [{key, value}, ...] 形状。"""
    result: dict[str, str] = {}
    raw = attributes.get("variables")
    if isinstance(raw, dict):
        return {str(key).upper(): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("key"):
                result[str(item["key"]).upper()] = str(item.get("value") or "")
    return result


def parse_gitlab_payload(payload: dict[str, Any] | None, *, event_header: str = "") -> GitlabTrigger:
    """把两种来源归一化成 GitlabTrigger。缺关键字段时抛 CiRequestError。"""
    data = payload if isinstance(payload, dict) else {}
    object_kind = _pick(data.get("object_kind"), event_header).lower()
    attributes = data.get("object_attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    project_block = data.get("project")
    project_block = project_block if isinstance(project_block, dict) else {}
    user_block = data.get("user")
    user_block = user_block if isinstance(user_block, dict) else {}
    commit_block = data.get("commit")
    commit_block = commit_block if isinstance(commit_block, dict) else {}
    variables = _pipeline_variables(attributes)

    # 项目：显式 project > pipeline 变量 TESTFLOW_PROJECT > GitLab 项目路径/名称
    project_ref = _pick(
        data.get("project_ref"),
        data.get("testflow_project"),
        variables.get("TESTFLOW_PROJECT"),
        data.get("project") if isinstance(data.get("project"), str) else "",
        project_block.get("path_with_namespace"),
        project_block.get("name"),
        data.get("CI_PROJECT_PATH"),
    )
    plan_ref = _pick(
        data.get("plan"),
        variables.get("TESTFLOW_PLAN"),
        data.get("CI_TESTFLOW_PLAN"),
    )
    environment_ref = _pick(
        data.get("environment"),
        data.get("env"),
        variables.get("TESTFLOW_ENV"),
        data.get("CI_TESTFLOW_ENV"),
    )

    pipeline_id = _pick(
        data.get("pipeline_id"),
        attributes.get("id"),
        data.get("CI_PIPELINE_ID"),
    )
    project_web_url = _pick(project_block.get("web_url"), data.get("CI_PROJECT_URL"))
    pipeline_url = _pick(
        data.get("pipeline_url"),
        attributes.get("url"),
        data.get("CI_PIPELINE_URL"),
        f"{project_web_url}/-/pipelines/{pipeline_id}" if project_web_url and pipeline_id else "",
    )
    commit_sha = _pick(
        data.get("commit_sha"),
        attributes.get("sha"),
        commit_block.get("id"),
        data.get("CI_COMMIT_SHA"),
    )
    branch = _pick(
        data.get("branch"),
        attributes.get("ref"),
        data.get("CI_COMMIT_REF_NAME"),
        data.get("CI_COMMIT_BRANCH"),
    )
    user = _pick(
        data.get("user") if isinstance(data.get("user"), str) else "",
        data.get("triggered_by"),
        data.get("GITLAB_USER_LOGIN"),
        user_block.get("username"),
        user_block.get("name"),
    )

    if not project_ref:
        raise CiRequestError(
            400,
            "无法确定 TestFlowCI 项目：请在请求里显式提供 project，"
            "或设置 pipeline 变量 TESTFLOW_PROJECT",
        )
    if not plan_ref:
        raise CiRequestError(400, "缺少 plan：请在请求里提供 plan，或设置 pipeline 变量 TESTFLOW_PLAN")
    if not environment_ref:
        raise CiRequestError(400, "缺少 environment：请在请求里提供 environment，或设置 pipeline 变量 TESTFLOW_ENV")

    return GitlabTrigger(
        project_ref=project_ref,
        plan_ref=plan_ref,
        environment_ref=environment_ref,
        pipeline_id=pipeline_id,
        pipeline_url=pipeline_url,
        commit_sha=commit_sha[:64],
        branch=branch[:128],
        user=user[:128],
        event=_pick(object_kind, "unknown")[:32],
    )


__all__ = ["KNOWN_EVENTS", "GitlabTrigger", "parse_gitlab_payload"]
