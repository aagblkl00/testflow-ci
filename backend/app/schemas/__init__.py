"""Pydantic 出入参。

约定：出参统一带 `model_config = ConfigDict(from_attributes=True)`，
前端拿到的 JSON 与数据库字段同名，减少映射层出错。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ORM = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ORM

    id: str
    username: str
    display_name: str = ""
    role: str
    enabled: bool = True
    created_at: datetime | None = None


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "TESTER"


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    repository_url: str = ""


class ProjectOut(BaseModel):
    model_config = ORM

    id: str
    name: str
    description: str = ""
    repository_url: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EnvironmentIn(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=64)
    base_url: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)
    browser: str = "chromium"
    environment_type: str = "TEST"
    allowed_hosts: list[str] = Field(default_factory=list)
    read_only: bool = False


class EnvironmentOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    name: str
    base_url: str = ""
    browser: str = "chromium"
    environment_type: str = "TEST"
    allowed_hosts: list[str] = Field(default_factory=list)
    read_only: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EnvironmentOutWithMaskedVariables(EnvironmentOut):
    """出参里变量已按敏感度脱敏，密码/Token 不会出现在接口响应中。"""

    variables: dict[str, Any] = Field(default_factory=dict)


class TestStepIn(BaseModel):
    sort_order: int = 1
    action: str = ""
    element_id: str = ""
    value: str = ""
    expected: str = ""
    timeout: int = 15000
    retry: int = 0
    screenshot: bool = False


class TestStepOut(BaseModel):
    model_config = ORM

    id: str
    sort_order: int
    action: str = ""
    element_id: str = ""
    value: str = ""
    expected: str = ""
    timeout: int = 15000
    retry: int = 0
    screenshot: bool = False


class TestCaseIn(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=255)
    case_code: str = ""
    module: str = ""
    priority: str = "P2"
    status: str = "ACTIVE"
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    preconditions: str = ""
    expected: str = ""
    created_by: str = ""
    steps: list[TestStepIn] = Field(default_factory=list)


class TestCaseOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    case_code: str = ""
    name: str
    module: str = ""
    priority: str = "P2"
    status: str = "ACTIVE"
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    preconditions: str = ""
    expected: str = ""
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    steps: list[TestStepOut] = Field(default_factory=list)
    flow_id: str = ""
    flow_name: str = ""


class FlowIn(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=128)
    engine: str = "playwright"
    environment_id: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)


class FlowOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    name: str
    version: int = 1
    engine: str = "playwright"
    environment_id: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)
    definition: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BindingIn(BaseModel):
    case_id: str
    flow_id: str
    enabled: bool = True


class BindingOut(BaseModel):
    model_config = ORM

    id: str
    case_id: str
    flow_id: str
    enabled: bool


class PlanIn(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    environment_id: str | None = None
    # M2：suite_id 优先；suite_id 为空时回落到 case_ids（M1 兼容路径）
    suite_id: str | None = None
    case_ids: list[str] = Field(default_factory=list)
    browser: str = "chromium"
    trigger_type: str = "MANUAL"
    schedule: str = ""
    retry_count: int = 0
    fail_retry: bool = False
    notification_enabled: bool = False
    enabled: bool = True


class PlanOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    name: str
    description: str = ""
    environment_id: str | None = None
    suite_id: str | None = None
    case_ids: list[str] = Field(default_factory=list)
    browser: str = "chromium"
    trigger_type: str = "MANUAL"
    schedule: str = ""
    retry_count: int = 0
    fail_retry: bool = False
    notification_enabled: bool = False
    enabled: bool = True
    created_at: datetime | None = None
    suite_name: str = ""
    resolved_case_count: int = 0


class SuiteCaseIn(BaseModel):
    case_id: str
    sort_order: int | None = None
    enabled: bool = True


class SuiteOrderItem(BaseModel):
    case_id: str
    sort_order: int
    enabled: bool = True


class SuiteOrderIn(BaseModel):
    items: list[SuiteOrderItem]


class TestSuiteIn(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    enabled: bool = True
    case_ids: list[str] = Field(default_factory=list)


class SuiteMemberOut(BaseModel):
    model_config = ORM

    id: str
    case_id: str
    sort_order: int
    enabled: bool
    case_code: str = ""
    case_name: str = ""
    module: str = ""
    priority: str = "P2"
    case_status: str = ""
    flow_id: str = ""
    flow_name: str = ""


class TestSuiteOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    name: str
    description: str = ""
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    members: list[SuiteMemberOut] = Field(default_factory=list)
    case_count: int = 0
    enabled_case_count: int = 0


class RunCreate(BaseModel):
    plan_id: str | None = None
    project_id: str | None = None
    environment_id: str | None = None
    trigger_source: str = "MANUAL"
    commit_sha: str = ""
    branch: str = ""
    pipeline_id: str = ""
    pipeline_url: str = ""
    blocking: bool = False
    case_ids: list[str] | None = None


class CancelRequest(BaseModel):
    reason: str = ""


class RunOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    plan_id: str | None = None
    environment_id: str = ""
    commit_sha: str = ""
    branch: str = ""
    trigger_source: str = "MANUAL"
    pipeline_id: str = ""
    pipeline_url: str = ""
    # M4：本次执行的 base_url 覆盖（CLI --base-url），空表示用环境自身配置
    base_url_override: str = ""
    status: str
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    skipped_cases: int = 0
    error_cases: int = 0
    flaky_cases: int = 0
    cancelled_cases: int = 0
    source_run_id: str | None = None
    rerun_mode: str = ""
    cancel_requested: bool = False
    started_at: datetime | None = None
    cancelled_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    error_message: str = ""
    created_at: datetime | None = None
    plan_name: str = ""
    environment_name: str = ""


class StepResultOut(BaseModel):
    model_config = ORM

    id: str
    attempt_id: str | None = None
    step_no: int
    step_name: str = ""
    action: str = ""
    status: str
    expected: str = ""
    actual: str = ""
    duration_ms: int = 0
    error: str = ""
    attempt: int = 1
    screenshot_path: str = ""
    page_url: str = ""
    page_title: str = ""
    dom_path: str = ""
    log_path: str = ""
    # M3 归因信号：执行详情可以直接看到「为什么被分类成这一类」
    console_summary: str = ""
    network_summary: str = ""
    request_method: str = ""
    request_url: str = ""
    http_status: int | None = None
    js_error: str = ""


class AttemptOut(BaseModel):
    model_config = ORM

    id: str
    case_result_id: str
    attempt_no: int
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    error_message: str = ""
    trace_path: str = ""
    video_path: str = ""
    har_path: str = ""
    step_results: list[StepResultOut] = Field(default_factory=list)


class CaseResultOut(BaseModel):
    model_config = ORM

    id: str
    run_id: str
    case_id: str | None = None
    case_code: str = ""
    case_name: str = ""
    flow_id: str = ""
    status: str
    attempts_count: int = 1
    flaky: bool = False
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    error_message: str = ""
    # attempts 是 M2 的主结构；step_results 是所有 attempt 的步骤汇总（扁平），
    # 保留它是为了让 M1 的页面与验收脚本继续可用。
    attempts: list[AttemptOut] = Field(default_factory=list)
    step_results: list[StepResultOut] = Field(default_factory=list)


class ArtifactOut(BaseModel):
    model_config = ORM

    id: str
    run_id: str
    case_result_id: str | None = None
    step_result_id: str | None = None
    attempt_id: str | None = None
    type: str
    path: str
    size: int = 0
    url: str = ""
    created_at: datetime | None = None


class ArtifactPatch(BaseModel):
    run_id: str
    case_result_id: str | None = None
    step_result_id: str | None = None
    type: str = "log"
    path: str
    size: int = 0


class DashboardSummary(BaseModel):
    today_runs: int = 0
    today_passed: int = 0
    today_failed: int = 0
    today_skipped: int = 0
    today_pass_rate: float = 0.0
    running_runs: int = 0
    recent_runs: list[RunOut] = Field(default_factory=list)
    recent_failures: list[CaseResultOut] = Field(default_factory=list)
    # M3：失败中心在 Dashboard 上的聚合视图
    failure_groups: int = 0
    open_failure_groups: int = 0
    failure_categories: dict[str, int] = Field(default_factory=dict)
    failures: list["FailureGroupOut"] = Field(default_factory=list)
    flaky_cases: int = 0
    flaky_rate: float = 0.0


# ------------------------------------------------------------------ M3 失败中心


class FailureFingerprintOut(BaseModel):
    model_config = ORM

    id: str
    fingerprint: str
    strategy: str
    failure_type: str
    exception_type: str = ""
    normalized_message: str = ""
    normalized_expected: str = ""
    normalized_actual: str = ""
    page_url: str = ""
    request_method: str = ""
    request_url: str = ""
    http_status: int | None = None
    locator: str = ""
    step_action: str = ""
    sample_message: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    occurrence_groups: int = 0


class FailureGroupOut(BaseModel):
    model_config = ORM

    id: str
    project_id: str
    project_name: str = ""
    fingerprint_id: str
    title: str = ""
    category: str = "UNKNOWN"
    rule_id: str = ""
    rule_reason: str = ""
    status: str = "OPEN"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrence_count: int = 0
    failed_case_count: int = 0
    failed_run_count: int = 0
    flaky_count: int = 0
    latest_run_id: str = ""
    latest_case_result_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fingerprint: FailureFingerprintOut | None = None
    # 这个分类对应的重试策略，让测试人员看到「为什么系统不重试 / 重试几次」
    retry_policy: dict[str, Any] = Field(default_factory=dict)


class FailureGroupPage(BaseModel):
    items: list[FailureGroupOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 0
    # 当前筛选条件下的分类分布，用于列表页顶部的分类 Tab
    category_counts: dict[str, int] = Field(default_factory=dict)


class FailureOccurrenceOut(BaseModel):
    model_config = ORM

    id: str
    failure_group_id: str
    fingerprint_id: str
    run_id: str
    case_result_id: str
    attempt_id: str | None = None
    step_result_id: str | None = None
    case_status: str = ""
    is_flaky: bool = False
    # 这一次出现自己的分类：同一个指纹下的 Flaky 与稳定失败必须分得开
    category: str = "UNKNOWN"
    created_at: datetime | None = None

    # 展开出来的现场信息，避免前端为了列表再打一圈接口
    case_code: str = ""
    case_name: str = ""
    run_status: str = ""
    run_started_at: datetime | None = None
    # M4：沿 Run 带出 CI 信息，失败中心不必再复制一套 commit/branch 字段
    run_branch: str = ""
    run_commit_sha: str = ""
    run_pipeline_id: str = ""
    run_pipeline_url: str = ""
    run_trigger_source: str = ""
    plan_id: str | None = None
    plan_name: str = ""
    environment_name: str = ""
    attempt_no: int = 0
    attempt_status: str = ""
    step_no: int = 0
    step_action: str = ""
    step_status: str = ""
    step_error: str = ""
    step_expected: str = ""
    step_actual: str = ""


class FailureOccurrencePage(BaseModel):
    items: list[FailureOccurrenceOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 0


class FailureAffectedCase(BaseModel):
    """某个 Failure Group 影响到的用例（按用例聚合）。"""

    case_id: str | None = None
    case_code: str = ""
    case_name: str = ""
    occurrences: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    is_flaky: bool = False


class FailureTrendPoint(BaseModel):
    run_id: str
    run_started_at: datetime | None = None
    run_status: str = ""
    occurrences: int = 0
    cases: int = 0
    flaky_occurrences: int = 0


class FailureTrendOut(BaseModel):
    group_id: str
    points: list[FailureTrendPoint] = Field(default_factory=list)
    total_occurrences: int = 0
    flaky_occurrences: int = 0


class FailureGroupPatch(BaseModel):
    status: str = Field(pattern="^(OPEN|RESOLVED|IGNORED)$")


class FlakyCaseStat(BaseModel):
    case_id: str | None = None
    case_code: str = ""
    case_name: str = ""
    executions: int = 0
    flaky_count: int = 0
    flaky_rate: float = 0.0


class FlakyRunStat(BaseModel):
    run_id: str
    started_at: datetime | None = None
    flaky_cases: int = 0
    total_cases: int = 0


class FlakyGroupStat(BaseModel):
    group_id: str
    title: str = ""
    category: str = ""
    flaky_occurrences: int = 0
    all_occurrences: int = 0


class FlakyStatistics(BaseModel):
    """Flaky 统计。

    分母必须显式给出，`definition` 说明分子怎么算 —— 方案 §33 明确要求
    「不要出现没有分母的 12%」，也要求统计定义写在 API 里而不是让人猜。
    """

    total_case_executions: int = 0
    flaky_case_executions: int = 0
    flaky_rate: float = 0.0
    denominator: str = "该项目下全部用例执行次数（CaseResult 行数）"
    numerator: str = "其中最终状态为 FLAKY 的执行次数"
    definition: str = (
        "Flaky = 同一次执行里，先失败、重试后通过。"
        "分母是全部用例执行次数，所以这个比例回答的是「每跑 100 次用例，有多少次结果不稳定」。"
    )
    distinct_flaky_cases: int = 0
    flaky_groups: list[FlakyGroupStat] = Field(default_factory=list)
    by_case: list[FlakyCaseStat] = Field(default_factory=list)
    by_run: list[FlakyRunStat] = Field(default_factory=list)


class RetryPolicyOut(BaseModel):
    defaults: dict[str, int] = Field(default_factory=dict)
    effective: dict[str, int] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    precedence: list[str] = Field(default_factory=list)


class FailureAnalyzeResult(BaseModel):
    run_id: str
    analyzed: bool = False
    reason: str = ""
    failed_cases: int = 0
    occurrences: int = 0
    groups_touched: int = 0
    categories: dict[str, int] = Field(default_factory=dict)


# ------------------------------------------------------- M5 AI 分析 / 告警


class AnalyzeRequest(BaseModel):
    """触发一次 AI 分析。"""

    force: bool = Field(default=False, description="true=忽略已有分析，强制产生新 revision")
    occurrence_id: str | None = Field(default=None, description="指定代表出现；不填取最近一次")
    provider: str | None = Field(default=None, description="临时指定 Provider，不填用配置默认值")


class AnalysisReviewRequest(BaseModel):
    review_status: str = Field(pattern="^(CONFIRMED|MODIFIED|IGNORED)$")
    review_comment: str = ""


class AlertTransitionRequest(BaseModel):
    comment: str = ""


class AlertListPage(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    severity_counts: dict[str, int] = Field(default_factory=dict)


class AiMetaOut(BaseModel):
    """AI 与通知的可用性，供前端决定按钮该不该亮。"""

    ai_enabled: bool = False
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    available_providers: list[str] = Field(default_factory=list)
    auto_analyze: bool = True
    required_rules: list[str] = Field(default_factory=list)
    notification_enabled: bool = False
    notification_provider: str = ""
    notification_max_attempts: int = 3
    note: str = ""


class RetentionRequest(BaseModel):
    """M6-B：管理员触发 Retention Preview / Apply 的请求。

    `retention_days` 不传时使用全局 `ARTIFACT_RETENTION_DAYS`；传了则只对本次操作生效，
    不改变全局配置。
    """

    retention_days: int | None = Field(default=None, gt=0)


TokenResponse.model_rebuild()
DashboardSummary.model_rebuild()
