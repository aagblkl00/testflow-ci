"""M1 最小领域模型：12 张表。

设计原则（对齐《V1 改造计划》）：
- 只建闭环必需的表，其余（suites / failure_groups / notifications / ci / owner_mappings）
  留到对应里程碑再建，避免"表数量达标但没验证过"。
- 所有状态用字符串常量而不是数据库 Enum，保证 SQLite 与 PostgreSQL 行为一致。
- 多值字段在 M1 用 JSON 列承载（RegressionPlan.case_ids 等），
  真正需要独立生命周期时再升级为关联表。

注意：测试用例与自动化流程之间**不使用硬关联字段**，
一律通过 case_flow_bindings 表建立关系。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import relationship

from ..db import Base
from ..utils.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


ROLES = ("ADMIN", "TESTER", "VIEWER")

ENVIRONMENT_TYPES = ("DEV", "TEST", "STAGING", "PROD")

# M2：CANCELLING 是「已请求停止、执行器尚未退出」的中间态，让页面能显示"停止中"
RUN_STATUSES = ("QUEUED", "RUNNING", "CANCELLING", "PASSED", "FAILED", "PARTIAL", "CANCELLED", "ERROR")
CASE_RESULT_STATUSES = ("PENDING", "RUNNING", "PASSED", "FAILED", "SKIPPED", "FLAKY", "ERROR", "CANCELLED")
# M2 扩展：方案 §17 原本只定义了 PASSED/FAILED/SKIPPED/ERROR。
# 「停止执行」需要给正在执行的步骤一个明确终止状态，因此补充 CANCELLED。
STEP_RESULT_STATUSES = ("PASSED", "FAILED", "SKIPPED", "ERROR", "CANCELLED")
ATTEMPT_STATUSES = ("RUNNING", "PASSED", "FAILED", "ERROR", "CANCELLED")

TRIGGER_TYPES = ("MANUAL", "SCHEDULE", "CI")
# M4：Run 的触发来源。比计划的 trigger_type 多几个值，用来区分「谁触发了这次执行」：
#   CLI      —— 开发者/CI 用 CLI 触发
#   GITLAB   —— GitLab Pipeline / Webhook 触发
#   REVERIFY —— M5 告警的重新验证（复用失败用例重跑）
# 计划（regression_plans.trigger_type）仍然只允许 TRIGGER_TYPES，二者语义不同：
# 一个是「这个计划打算怎么跑」，一个是「这一次实际是谁触发的」。
RUN_TRIGGER_SOURCES = ("MANUAL", "SCHEDULE", "CI", "CLI", "GITLAB", "REVERIFY")
# 参与 CI 幂等约束的触发来源。手动重跑会继承原 Run 的 pipeline_id，
# 但它是一次新的执行而不是同一个 Pipeline 事件，所以必须排除在外，
# 否则「重跑」会被幂等约束直接挡掉。
CI_TRIGGER_SOURCES = ("CI", "CLI", "GITLAB")
ARTIFACT_TYPES = ("screenshot", "trace", "video", "har", "console", "network", "dom", "log", "report")

# M3：确定性失败分类。AI 不在 M3 的范围内。
FAILURE_CATEGORIES = (
    "PRODUCT_BUG",
    "ENVIRONMENT_ERROR",
    "AUTOMATION_ERROR",
    "DATA_ERROR",
    "FLAKY",
    "UNKNOWN",
)
FAILURE_GROUP_STATUSES = ("OPEN", "RESOLVED", "IGNORED")

# ------------------------------------------------------------------ M5 AI / 告警 / 通知

ANALYSIS_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")
ANALYSIS_REVIEW_STATUSES = ("UNREVIEWED", "CONFIRMED", "MODIFIED", "IGNORED")

ALERT_SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")
ALERT_STATUSES = ("OPEN", "ACKNOWLEDGED", "FIXING", "REVERIFY", "RESOLVED", "IGNORED")
# 「活动告警」：同一个失败分组同时只能有一条。解决/忽略之后同一个问题再出现，
# 允许再开一条新的 —— 所以唯一约束必须是**部分**的。
ALERT_ACTIVE_STATUSES = ("OPEN", "ACKNOWLEDGED", "FIXING", "REVERIFY")
# 告警状态机。白名单式，非法跳转一律拒绝。
# 特别地：RESOLVED 只能从 REVERIFY 来 —— 不允许「OPEN 直接解决」，
# 那等于没验证就宣称修好了。IGNORED 只能从 OPEN 来。
ALERT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "OPEN": ("ACKNOWLEDGED", "IGNORED"),
    "ACKNOWLEDGED": ("FIXING",),
    "FIXING": ("REVERIFY",),
    "REVERIFY": ("RESOLVED", "OPEN"),
    "RESOLVED": (),
    "IGNORED": ("OPEN",),
}

NOTIFICATION_STATUSES = ("PENDING", "SENDING", "SENT", "FAILED", "SKIPPED")
NOTIFICATION_CHANNELS = ("wecom_webhook",)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(String(32), primary_key=True, default=new_id)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(64), default="")
    password_hash = Column(String(255), nullable=False)
    role = Column(String(16), default="TESTER", nullable=False)
    wecom_userid = Column(String(64), default="")
    enabled = Column(Boolean, default=True, nullable=False)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id = Column(String(32), primary_key=True, default=new_id)
    name = Column(String(128), nullable=False, unique=True)
    description = Column(Text, default="")
    repository_url = Column(String(255), default="")

    environments = relationship("Environment", back_populates="project", cascade="all, delete-orphan")
    test_cases = relationship("TestCase", back_populates="project", cascade="all, delete-orphan")
    flows = relationship("AutomationFlow", back_populates="project", cascade="all, delete-orphan")
    suites = relationship("TestSuite", back_populates="project", cascade="all, delete-orphan")
    plans = relationship("RegressionPlan", back_populates="project", cascade="all, delete-orphan")


class Environment(Base, TimestampMixin):
    __tablename__ = "environments"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    base_url = Column(String(255), default="")
    variables = Column(JSON, default=dict)
    browser = Column(String(16), default="chromium")
    environment_type = Column(String(16), default="TEST", nullable=False)
    allowed_hosts = Column(JSON, default=list)
    read_only = Column(Boolean, default=False, nullable=False)

    project = relationship("Project", back_populates="environments")


class TestCase(Base, TimestampMixin):
    __tablename__ = "test_cases"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    case_code = Column(String(64), default="", index=True)
    name = Column(String(255), nullable=False)
    module = Column(String(64), default="")
    priority = Column(String(8), default="P2")
    status = Column(String(16), default="ACTIVE")
    tags = Column(JSON, default=list)
    description = Column(Text, default="")
    preconditions = Column(Text, default="")
    expected = Column(Text, default="")
    created_by = Column(String(64), default="")

    project = relationship("Project", back_populates="test_cases")
    steps = relationship("TestStep", back_populates="case", cascade="all, delete-orphan", order_by="TestStep.sort_order")
    bindings = relationship("CaseFlowBinding", back_populates="case", cascade="all, delete-orphan")


class TestStep(Base):
    """测试用例的人工步骤（用于文档与评审），执行时以下方 AutomationFlow 为准。"""

    __tablename__ = "test_steps"

    id = Column(String(32), primary_key=True, default=new_id)
    case_id = Column(String(32), ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    sort_order = Column(Integer, default=1, nullable=False)
    action = Column(String(64), default="")
    element_id = Column(String(128), default="")
    locator = Column(JSON, default=dict)
    value = Column(Text, default="")
    expected = Column(Text, default="")
    timeout = Column(Integer, default=15000)
    retry = Column(Integer, default=0)
    screenshot = Column(Boolean, default=False)

    case = relationship("TestCase", back_populates="steps")


class AutomationFlow(Base, TimestampMixin):
    """可执行的自动化流程。definition 为 ActionSpec 步骤数组。"""

    __tablename__ = "automation_flows"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    version = Column(Integer, default=1, nullable=False)
    engine = Column(String(16), default="playwright", nullable=False)
    environment_id = Column(String(32), default="")
    variables = Column(JSON, default=dict)
    definition = Column(JSON, default=dict)

    project = relationship("Project", back_populates="flows")
    bindings = relationship("CaseFlowBinding", back_populates="flow", cascade="all, delete-orphan")


class CaseFlowBinding(Base):
    """测试用例 ↔ 自动化流程 的唯一绑定方式（替代硬关联字段）。"""

    __tablename__ = "case_flow_bindings"

    id = Column(String(32), primary_key=True, default=new_id)
    case_id = Column(String(32), ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    flow_id = Column(String(32), ForeignKey("automation_flows.id", ondelete="CASCADE"), nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)

    case = relationship("TestCase", back_populates="bindings")
    flow = relationship("AutomationFlow", back_populates="bindings")


class TestSuite(Base, TimestampMixin):
    """测试套件：把多个测试用例组织成一个可复用的集合（Smoke / 登录回归 / 订单回归…）。

    M2 起 RegressionPlan 优先通过 suite_id 引用套件；
    旧的 case_ids(JSON) 字段保留不动，老计划继续可用。
    """

    __tablename__ = "test_suites"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    enabled = Column(Boolean, default=True, nullable=False)

    project = relationship("Project", back_populates="suites")
    members = relationship(
        "TestSuiteCase",
        back_populates="suite",
        cascade="all, delete-orphan",
        order_by="TestSuiteCase.sort_order",
    )


class TestSuiteCase(Base):
    """套件成员。sort_order 决定执行顺序，enabled 控制单个用例是否参与本次执行。"""

    __tablename__ = "test_suite_cases"

    id = Column(String(32), primary_key=True, default=new_id)
    suite_id = Column(String(32), ForeignKey("test_suites.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(String(32), ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    sort_order = Column(Integer, default=1, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)

    suite = relationship("TestSuite", back_populates="members")
    case = relationship("TestCase")


class RegressionPlan(Base, TimestampMixin):
    __tablename__ = "regression_plans"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    environment_id = Column(String(32), ForeignKey("environments.id", ondelete="SET NULL"), nullable=True)
    # suite_id 优先；为空时回落到 case_ids（M1 行为，保持兼容）
    suite_id = Column(String(32), ForeignKey("test_suites.id", ondelete="SET NULL"), nullable=True, index=True)
    case_ids = Column(JSON, default=list)
    browser = Column(String(16), default="chromium")
    trigger_type = Column(String(16), default="MANUAL", nullable=False)
    schedule = Column(String(64), default="")
    retry_count = Column(Integer, default=0)
    fail_retry = Column(Boolean, default=False)
    notification_enabled = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True, nullable=False)

    project = relationship("Project", back_populates="plans")
    suite = relationship("TestSuite")


class ExecutionRun(Base):
    __tablename__ = "execution_runs"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id = Column(String(32), ForeignKey("regression_plans.id", ondelete="SET NULL"), nullable=True, index=True)
    environment_id = Column(String(32), default="")
    commit_sha = Column(String(64), default="")
    branch = Column(String(128), default="")
    trigger_source = Column(String(16), default="MANUAL", nullable=False)
    pipeline_id = Column(String(64), default="")
    pipeline_url = Column(String(255), default="")
    # M4：CLI 的 --base-url 覆盖。只影响这次执行的 base_url，不改环境配置本身。
    # 覆盖值同样要过 Safety Gate（域名白名单照旧检查覆盖后的主机），
    # 所以它不能用来越过安全门禁，只能用来指向同一环境集合里的另一个地址。
    base_url_override = Column(String(255), default="")
    status = Column(String(16), default="QUEUED", nullable=False, index=True)
    total_cases = Column(Integer, default=0)
    passed_cases = Column(Integer, default=0)
    failed_cases = Column(Integer, default=0)
    skipped_cases = Column(Integer, default=0)
    error_cases = Column(Integer, default=0)
    flaky_cases = Column(Integer, default=0)
    cancelled_cases = Column(Integer, default=0)
    # M2：失败用例重跑 —— 记录来源 Run，以及本次显式指定的用例清单（覆盖 Plan/Suite）
    source_run_id = Column(String(32), ForeignKey("execution_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    case_ids = Column(JSON, nullable=True)
    rerun_mode = Column(String(16), default="")
    # M2：停止执行。cancel_requested 是跨进程唯一可靠的停止信号
    # （inline 模式还有进程内 Event 可做到即时；Celery 模式靠这个字段轮询）
    cancel_requested = Column(Boolean, default=False, nullable=False)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, default=0)
    error_message = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    case_results = relationship("CaseResult", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        # M4 幂等的最终保障：同一个 (项目, 计划, Pipeline) 只能有一条 CI 触发的 Run。
        # 必须是「部分」唯一索引（带 WHERE），原因有两个：
        #   1) 手动执行的 pipeline_id 是空串，加上去会挡住同一个计划的第二次手动执行；
        #   2) 手动重跑会继承原 Run 的 pipeline_id，但它是一次新执行而不是同一个
        #      Pipeline 事件，不能被视为重复。
        # SQLite 与 PostgreSQL 都支持部分索引；靠它 + 应用层捕获 IntegrityError 双保险，
        # 而不是 if not exists（并发下必然漏）。
        Index(
            "uq_execution_runs_ci_idempotency",
            "project_id",
            "plan_id",
            "pipeline_id",
            unique=True,
            sqlite_where=text("pipeline_id <> '' AND trigger_source IN ('CI', 'CLI', 'GITLAB')"),
            postgresql_where=text("pipeline_id <> '' AND trigger_source IN ('CI', 'CLI', 'GITLAB')"),
        ),
    )


class CaseResult(Base):
    __tablename__ = "case_results"

    id = Column(String(32), primary_key=True, default=new_id)
    run_id = Column(String(32), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(String(32), ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True, index=True)
    case_code = Column(String(64), default="")
    case_name = Column(String(255), default="")
    flow_id = Column(String(32), default="")
    status = Column(String(16), default="PENDING", nullable=False, index=True)
    # M2：attempts_count > 1 且最终通过 => FLAKY，不允许伪装成 PASS
    attempts_count = Column(Integer, default=1)
    flaky = Column(Boolean, default=False, nullable=False)
    attempt = Column(Integer, default=1)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, default=0)
    error_message = Column(Text, default="")

    run = relationship("ExecutionRun", back_populates="case_results")
    attempts = relationship(
        "CaseResultAttempt",
        back_populates="case_result",
        cascade="all, delete-orphan",
        order_by="CaseResultAttempt.attempt_no",
    )
    step_results = relationship("StepResult", back_populates="case_result", cascade="all, delete-orphan", order_by="StepResult.step_no")


class CaseResultAttempt(Base):
    """一次真实的重新执行。

    每一次 attempt 拥有独立的步骤结果与证据目录（attempts/{n}/），
    绝不覆盖上一次的证据 —— 这是 Flaky 判定能站得住脚的前提。
    """

    __tablename__ = "case_result_attempts"

    id = Column(String(32), primary_key=True, default=new_id)
    case_result_id = Column(String(32), ForeignKey("case_results.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_no = Column(Integer, default=1, nullable=False)
    status = Column(String(16), default="RUNNING", nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, default=0)
    error_message = Column(Text, default="")
    trace_path = Column(String(512), default="")
    video_path = Column(String(512), default="")
    har_path = Column(String(512), default="")

    case_result = relationship("CaseResult", back_populates="attempts")
    step_results = relationship(
        "StepResult",
        back_populates="attempt_record",
        cascade="all, delete-orphan",
        order_by="StepResult.step_no",
    )


class StepResult(Base):
    __tablename__ = "step_results"

    id = Column(String(32), primary_key=True, default=new_id)
    case_result_id = Column(String(32), ForeignKey("case_results.id", ondelete="CASCADE"), nullable=False, index=True)
    # M2：步骤结果挂到具体的 attempt 上，保证 Flaky 的每一步归属清楚
    attempt_id = Column(String(32), ForeignKey("case_result_attempts.id", ondelete="CASCADE"), nullable=True, index=True)
    step_no = Column(Integer, default=0, nullable=False)
    step_name = Column(String(255), default="")
    action = Column(String(64), default="")
    element_id = Column(String(128), default="")
    locator = Column(JSON, default=dict)
    status = Column(String(16), default="PENDING", nullable=False, index=True)
    expected = Column(Text, default="")
    actual = Column(Text, default="")
    duration_ms = Column(Integer, default=0)
    error = Column(Text, default="")
    attempt = Column(Integer, default=1)
    screenshot_path = Column(String(512), default="")
    page_url = Column(String(1024), default="")
    page_title = Column(String(512), default="")
    console_path = Column(String(512), default="")
    network_path = Column(String(512), default="")
    dom_path = Column(String(512), default="")
    log_path = Column(String(512), default="")
    # M3：把失败归因需要的现场信号落到步骤上。
    # 这些字段让 Fingerprint 不必去翻 Artifact 文件，也避免执行器承担分类职责。
    console_summary = Column(Text, default="")     # 该步骤内发生的 console / pageerror
    network_summary = Column(Text, default="")     # 该步骤内发生的失败请求与 4xx/5xx
    request_method = Column(String(16), default="")
    request_url = Column(String(1024), default="")
    http_status = Column(Integer, nullable=True)
    js_error = Column(Text, default="")

    case_result = relationship("CaseResult", back_populates="step_results")
    # 注意：不能把这个关系命名为 attempt —— StepResult 上已经有一个叫 attempt 的
    # 整型列（步骤自身的重试序号），同名会把列定义覆盖掉，导致实例化时报
    # "'int' object has no attribute '_sa_instance_state'"。
    attempt_record = relationship("CaseResultAttempt", back_populates="step_results")


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(String(32), primary_key=True, default=new_id)
    run_id = Column(String(32), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    case_result_id = Column(String(32), ForeignKey("case_results.id", ondelete="CASCADE"), nullable=True, index=True)
    step_result_id = Column(String(32), ForeignKey("step_results.id", ondelete="CASCADE"), nullable=True, index=True)
    # M2：证据归属到 attempt，保证重试的每一次证据可独立查看、互不覆盖
    attempt_id = Column(String(32), ForeignKey("case_result_attempts.id", ondelete="CASCADE"), nullable=True, index=True)
    type = Column(String(16), nullable=False, index=True)
    path = Column(String(1024), nullable=False)
    size = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class FailureFingerprint(Base, TimestampMixin):
    """一次失败的确定性指纹。

    同一个实际问题（例如「订单接口 500」），无论出现在哪个 Run、哪个用例、
    URL 里的 ID 是什么，都应该落到同一行。

    `fingerprint` 唯一。原始错误信息继续留在 StepResult / Artifact 里，
    这里只保存归一化后的结果（且已经过 redact），不覆盖原始数据。
    """

    __tablename__ = "failure_fingerprints"

    id = Column(String(32), primary_key=True, default=new_id)
    fingerprint = Column(String(64), nullable=False, unique=True, index=True)
    strategy = Column(String(32), default="UNKNOWN", nullable=False)
    failure_type = Column(String(32), default="unknown", nullable=False)
    exception_type = Column(String(64), default="")
    normalized_message = Column(Text, default="")
    normalized_expected = Column(Text, default="")
    normalized_actual = Column(Text, default="")
    page_url = Column(String(512), default="")
    request_method = Column(String(16), default="")
    request_url = Column(String(1024), default="")
    http_status = Column(Integer, nullable=True)
    locator = Column(String(512), default="")
    step_action = Column(String(64), default="")
    # 脱敏后的原始消息片段，方便人眼核对指纹是怎么算出来的
    sample_message = Column(Text, default="")


class FailureGroup(Base, TimestampMixin):
    """一个 Failure Group = 当前规则认为属于同一个实际问题的一组失败。

    测试人员的实际操作对象是它，不是几百条 CaseResult。
    """

    __tablename__ = "failure_groups"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    fingerprint_id = Column(String(32), ForeignKey("failure_fingerprints.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), default="", nullable=False)
    category = Column(String(32), default="UNKNOWN", nullable=False, index=True)
    # 分类证据：命中哪条规则、为什么
    rule_id = Column(String(64), default="")
    rule_reason = Column(String(512), default="")
    status = Column(String(16), default="OPEN", nullable=False, index=True)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    occurrence_count = Column(Integer, default=0, nullable=False)
    failed_case_count = Column(Integer, default=0, nullable=False)
    failed_run_count = Column(Integer, default=0, nullable=False)
    flaky_count = Column(Integer, default=0, nullable=False)
    latest_run_id = Column(String(32), default="")
    latest_case_result_id = Column(String(32), default="")

    fingerprint_ref = relationship("FailureFingerprint")
    occurrences = relationship("FailureOccurrence", back_populates="group", cascade="all, delete-orphan")

    __table_args__ = (
        # 并发安全的最终保障：两个 Run 同时算出同一个指纹时，
        # 数据库唯一约束保证只会有一条 Group（不能只靠 if not exists）。
        UniqueConstraint("project_id", "fingerprint_id", name="uq_failure_group_project_fingerprint"),
    )


class FailureOccurrence(Base):
    """一次「某个 Group 又出现了」的记录。

    它是指向真实证据的锚点：Group → Occurrence → CaseResult → Attempt → StepResult → Artifact。
    Failure Center 不复制证据系统，只是沿着这条链钻取。
    """

    __tablename__ = "failure_occurrences"

    id = Column(String(32), primary_key=True, default=new_id)
    failure_group_id = Column(String(32), ForeignKey("failure_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    fingerprint_id = Column(String(32), ForeignKey("failure_fingerprints.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(String(32), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    case_result_id = Column(String(32), ForeignKey("case_results.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id = Column(String(32), ForeignKey("case_result_attempts.id", ondelete="CASCADE"), nullable=True)
    step_result_id = Column(String(32), ForeignKey("step_results.id", ondelete="CASCADE"), nullable=True)
    case_status = Column(String(16), default="")
    is_flaky = Column(Boolean, default=False, nullable=False)
    # M3：这一次出现自己的分类。
    # 为什么要单独存：一个指纹（例如「订单接口 500」）下面可能既有稳定复现的失败，
    # 也有重试后通过的 Flaky。分组上只能显示一个分类，但每一次出现的性质必须记清楚 ——
    # 否则「Flaky 失败被当成产品问题」这件事在数据层就无从纠正。
    category = Column(String(32), default="UNKNOWN", nullable=False, index=True)
    # 幂等键：等价于 (run_id, case_result_id, attempt_id, step_result_id, failure_group_id)。
    # 用显式字符串而不是 5 列联合唯一，是因为 SQLite 与 PostgreSQL 里
    # 唯一索引中的 NULL 互不相等，step_result_id 为空时会漏判重复。
    dedupe_key = Column(String(160), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    group = relationship("FailureGroup", back_populates="occurrences")


class FailureAnalysis(Base, TimestampMixin):
    """一次 AI 失败分析的结果。

    几条不可逾越的边界（方案 §8 与 §26）：

    - **不改 `FailureGroup.category`**。AI 判断出的类别存在 `ai_suggested_category`，
      与确定性分类字段分开；把 AI 结论写回 `category` 就等于让 AI 参与测试判定。
    - **不覆盖历史**。同一个分组可以有多条分析（revision_no 递增），
      这样才能比较「第一次和第二次 AI 看法是否一致」。
    - `status` 与 `review_status` 是两件事：前者是「AI 调没调成功」，
      后者是「测试人员认不认」。把两者混在一起，就没法回答
      「AI 失败了几次」与「人对结论的态度」这两个完全不同的问题。
    """

    __tablename__ = "failure_analyses"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    failure_group_id = Column(String(32), ForeignKey("failure_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    # 分析针对的是「某一次具体出现」，不是抽象的指纹
    failure_occurrence_id = Column(String(32), ForeignKey("failure_occurrences.id", ondelete="SET NULL"), nullable=True)
    run_id = Column(String(32), ForeignKey("execution_runs.id", ondelete="SET NULL"), nullable=True, index=True)

    provider = Column(String(32), default="")
    model = Column(String(64), default="")
    status = Column(String(16), default="PENDING", nullable=False, index=True)
    # 幂等键：同一份证据 + 同一版本提示词 + 同一 provider/model 只算一次分析
    analysis_key = Column(String(64), nullable=False, index=True)
    revision_no = Column(Integer, default=1, nullable=False)
    prompt_version = Column(String(16), default="")
    evidence_fingerprint = Column(String(64), default="")

    summary = Column(Text, default="")
    root_cause = Column(Text, default="")
    evidence = Column(JSON, default=list)
    related_commit = Column(String(64), default="")
    suggested_owner = Column(String(128), default="")
    confidence = Column(Float, nullable=True)
    uncertainty = Column(Text, default="")
    recommendation = Column(Text, default="")
    # AI 的建议分类：单独一列，绝不回写 category
    ai_suggested_category = Column(String(32), default="")
    # 脱敏后的原始响应，便于排查「AI 到底回了什么」
    raw_response_redacted = Column(Text, default="")

    error_message = Column(Text, default="")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, default=0)

    review_status = Column(String(16), default="UNREVIEWED", nullable=False, index=True)
    reviewed_by = Column(String(64), default="")
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_comment = Column(Text, default="")

    group = relationship("FailureGroup")


class Alert(Base, TimestampMixin):
    """告警。**聚合单位是 FailureGroup，不是单条失败。**

    方案 §12 的核心要求：10 个用例因为同一个根因失败，只应该有一条主要告警；
    同一个问题连续多个 Run 出现，也不应该每次 Run 都新开一条。
    所以这里是「活动告警唯一 + 计数累加」的模型，而不是「一次 Run 一条记录」。
    """

    __tablename__ = "alerts"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    failure_group_id = Column(String(32), ForeignKey("failure_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    # 首次触发这条告警的 Run
    run_id = Column(String(32), ForeignKey("execution_runs.id", ondelete="SET NULL"), nullable=True)
    latest_run_id = Column(String(32), default="")
    severity = Column(String(16), default="WARNING", nullable=False, index=True)
    status = Column(String(16), default="OPEN", nullable=False, index=True)
    title = Column(String(255), default="")
    summary = Column(Text, default="")

    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    occurrence_count = Column(Integer, default=0, nullable=False)
    failed_case_count = Column(Integer, default=0, nullable=False)
    failed_run_count = Column(Integer, default=0, nullable=False)

    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String(64), default="")
    # 重新验证新建的 Run（复用 M2 的失败用例重跑，不另写一套）
    reverify_run_id = Column(String(32), default="")

    group = relationship("FailureGroup")

    __table_args__ = (
        # 同一个失败分组同时只能有一条活动告警。
        # 用部分索引而不是全表唯一：告警解决/忽略之后同一个问题还会再出现，
        # 历史上要允许留多条 CLOSED 记录。
        Index(
            "uq_alerts_active_group",
            "failure_group_id",
            unique=True,
            sqlite_where=text("status IN ('OPEN', 'ACKNOWLEDGED', 'FIXING', 'REVERIFY')"),
            postgresql_where=text("status IN ('OPEN', 'ACKNOWLEDGED', 'FIXING', 'REVERIFY')"),
        ),
    )


class NotificationLog(Base, TimestampMixin):
    """一次通知投递的记录。

    它只记录「送没送出去」。**通知失败不允许改任何测试结果、也不允许改告警状态** ——
    送信失败和问题被修复之间没有任何因果关系。
    """

    __tablename__ = "notification_logs"

    id = Column(String(32), primary_key=True, default=new_id)
    alert_id = Column(String(32), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(32), default="wecom_webhook", nullable=False)
    provider = Column(String(64), default="")
    status = Column(String(16), default="PENDING", nullable=False, index=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    message_id = Column(String(64), default="")
    error_message = Column(Text, default="")
    # 告警内容指纹：内容变了才重新发，避免同一件事反复刷屏
    content_hash = Column(String(64), default="", index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 同一告警 + 同一渠道 + 同一内容只保留一行：重试是「更新这一行」，
        # 不是「再插一行」，否则重试次数根本数不清。
        UniqueConstraint("alert_id", "channel", "content_hash", name="uq_notification_alert_content"),
    )


class RetentionJob(Base):
    """M6-B：人工 Retention Apply 的审计记录。

    只做最小化审计：谁在什么时候用什么天数执行，Preview 时估计多少，
    Apply 实际删了多少、失败多少、释放多少空间。不参与业务状态机。
    """

    __tablename__ = "retention_jobs"

    id = Column(String(32), primary_key=True, default=new_id)
    executed_by = Column(String(64), default="", nullable=False)
    executed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    retention_days = Column(Integer, default=0, nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)

    preview_candidate_artifact_count = Column(Integer, default=0, nullable=False)
    preview_candidate_path_count = Column(Integer, default=0, nullable=False)
    preview_estimated_bytes = Column(BigInteger, default=0, nullable=False)
    preview_protected_artifact_count = Column(Integer, default=0, nullable=False)
    preview_protected_run_count = Column(Integer, default=0, nullable=False)
    preview_duplicate_path_count = Column(Integer, default=0, nullable=False)

    deleted_artifact_count = Column(Integer, default=0, nullable=False)
    deleted_file_count = Column(Integer, default=0, nullable=False)
    missing_file_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    freed_bytes = Column(BigInteger, default=0, nullable=False)

    status = Column(String(16), default="COMPLETED", nullable=False, index=True)
    error_summary = Column(JSON, default=list)

    __table_args__ = (
        Index("ix_retention_jobs_executed_at", "executed_at"),
    )


__all__ = [
    "ROLES",
    "ENVIRONMENT_TYPES",
    "RUN_STATUSES",
    "CASE_RESULT_STATUSES",
    "STEP_RESULT_STATUSES",
    "ATTEMPT_STATUSES",
    "TRIGGER_TYPES",
    "RUN_TRIGGER_SOURCES",
    "CI_TRIGGER_SOURCES",
    "ARTIFACT_TYPES",
    "FAILURE_CATEGORIES",
    "FAILURE_GROUP_STATUSES",
    "ANALYSIS_STATUSES",
    "ANALYSIS_REVIEW_STATUSES",
    "ALERT_SEVERITIES",
    "ALERT_STATUSES",
    "ALERT_ACTIVE_STATUSES",
    "ALERT_TRANSITIONS",
    "NOTIFICATION_STATUSES",
    "NOTIFICATION_CHANNELS",
    "User",
    "Project",
    "Environment",
    "TestCase",
    "TestStep",
    "AutomationFlow",
    "CaseFlowBinding",
    "TestSuite",
    "TestSuiteCase",
    "RegressionPlan",
    "ExecutionRun",
    "CaseResult",
    "CaseResultAttempt",
    "StepResult",
    "Artifact",
    "FailureFingerprint",
    "FailureGroup",
    "FailureOccurrence",
    "FailureAnalysis",
    "Alert",
    "NotificationLog",
    "RetentionJob",
    "utcnow",
]
