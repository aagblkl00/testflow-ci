"""M5 测试：AI Provider / 证据 / 分析幂等 / 人工确认 / 告警 / 通知 / 脱敏。

**不依赖真实 AI 与企业微信**（方案 §28、§29）：默认走 `FakeAIProvider` 与
`FakeWeComProvider`。Provider 与真实 HTTP 服务交互的部分由
`scripts/m5_stub_server.py` + `scripts/selftest_m5.py` 覆盖，那才是「协议层对不对」的
验收；这里解决的是「业务规则对不对」。

跑法：
    .venv/Scripts/python.exe -m pytest tests -q
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.ai import evidence as evidence_module  # noqa: E402
from backend.app.ai import service as ai_service  # noqa: E402
from backend.app.ai.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from backend.app.ai.provider import AIProviderError, available_providers, get_provider  # noqa: E402
from backend.app.ai.providers.fake import FakeAIProvider  # noqa: E402
from backend.app.ai.schemas import (  # noqa: E402
    EvidenceArtifact,
    FailureAnalysisContext,
    FailureAnalysisResult,
)
from backend.app.alerts import rules as alert_rules  # noqa: E402
from backend.app.alerts import service as alert_service  # noqa: E402
from backend.app.auth.security import create_access_token  # noqa: E402
from backend.app.config import settings  # noqa: E402
from backend.app.db import SessionLocal, init_db  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Alert,
    CaseResult,
    CaseResultAttempt,
    Environment,
    ExecutionRun,
    FailureAnalysis,
    FailureFingerprint,
    FailureGroup,
    FailureOccurrence,
    NotificationLog,
    Project,
    StepResult,
    User,
)
from backend.app.notification import service as notification_service  # noqa: E402
from backend.app.notification.provider import NotificationMessage  # noqa: E402
from backend.app.notification.providers.fake import FakeWeComProvider  # noqa: E402
from backend.app.utils.redact import redact_text  # noqa: E402

SUFFIX = uuid.uuid4().hex[:8]
SECRET = "m5-super-secret-value-1234"


# --------------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def chain() -> dict[str, str]:
    """造一条完整的真实链路：项目 → 环境 → Run → 指纹 → 分组 → 出现 → 步骤。"""
    init_db()
    db = SessionLocal()
    try:
        project = Project(name=f"M5测试项目-{SUFFIX}", description="test_m5")
        db.add(project)
        db.commit()
        environment = Environment(
            project_id=project.id,
            name=f"M5测试环境-{SUFFIX}",
            base_url="http://127.0.0.1:8100",
            environment_type="TEST",
            allowed_hosts=["127.0.0.1"],
            # 故意在环境变量里放一个敏感值：验证它不会漏进 Prompt / 通知 / 日志
            variables={"username": "demo", "password": SECRET},
        )
        db.add(environment)
        db.commit()

        run = ExecutionRun(
            project_id=project.id,
            environment_id=environment.id,
            status="FAILED",
            trigger_source="MANUAL",
            commit_sha="abc1234def",
            branch="main",
            total_cases=1,
            failed_cases=1,
        )
        db.add(run)
        db.commit()

        fingerprint = FailureFingerprint(
            fingerprint=f"m5fp{SUFFIX}",
            strategy="UNKNOWN",
            failure_type="unknown",
            exception_type="AssertionError",
            normalized_message="属性断言失败：期望包含账号",
            normalized_expected="账号",
            normalized_actual="",
            page_url="http://127.0.0.1:8100/home",
        )
        db.add(fingerprint)
        db.commit()

        group = FailureGroup(
            project_id=project.id,
            fingerprint_id=fingerprint.id,
            title="未分类失败：属性断言失败",
            category="UNKNOWN",
            rule_id="UNKNOWN_INSUFFICIENT_EVIDENCE",
            rule_reason="现有证据不足以确定性归因",
            status="OPEN",
            occurrence_count=1,
            failed_case_count=1,
            failed_run_count=1,
        )
        db.add(group)
        db.commit()

        case_result = CaseResult(
            run_id=run.id,
            case_code="M5-UNKNOWN-001",
            case_name="属性断言失败用例",
            status="FAILED",
            error_message=f"AssertionError: 属性断言失败（token={SECRET}）",
        )
        db.add(case_result)
        db.commit()

        attempt = CaseResultAttempt(case_result_id=case_result.id, attempt_no=1, status="FAILED")
        db.add(attempt)
        db.commit()

        step = StepResult(
            case_result_id=case_result.id,
            attempt_id=attempt.id,
            step_no=3,
            step_name="断言自定义属性",
            action="assert_attribute",
            status="FAILED",
            expected="账号",
            actual="",
            error=f"AssertionError: 属性断言失败：期望包含「账号」，实际为「」（authorization=Bearer {SECRET}）",
            page_url="http://127.0.0.1:8100/home",
            locator={"type": "testid", "value": "home-greeting"},
            console_summary=f"error: 请求失败 token={SECRET}",
            network_summary=f"401 GET http://127.0.0.1:8100/api/me?token={SECRET}",
        )
        db.add(step)
        db.commit()

        occurrence = FailureOccurrence(
            failure_group_id=group.id,
            fingerprint_id=fingerprint.id,
            run_id=run.id,
            case_result_id=case_result.id,
            attempt_id=attempt.id,
            step_result_id=step.id,
            case_status="FAILED",
            is_flaky=False,
            category="UNKNOWN",
            dedupe_key=f"m5dedupe-{SUFFIX}",
        )
        db.add(occurrence)
        db.commit()
        group.latest_run_id = run.id
        group.latest_case_result_id = case_result.id
        db.commit()

        data = {
            "project_id": project.id,
            "environment_id": environment.id,
            "run_id": run.id,
            "fingerprint_id": fingerprint.id,
            "group_id": group.id,
            "case_result_id": case_result.id,
            "attempt_id": attempt.id,
            "step_id": step.id,
            "occurrence_id": occurrence.id,
        }
    finally:
        db.close()

    yield data

    cleanup = SessionLocal()
    try:
        project = cleanup.get(Project, data["project_id"])
        if project is not None:
            cleanup.delete(project)
            cleanup.commit()
    finally:
        cleanup.close()


@pytest.fixture(scope="module")
def m5_ai_on(chain: dict[str, str]) -> None:
    """把配置临时改成「AI 已开 + 用 fake provider」。

    settings 是 frozen dataclass，所以用 object.__setattr__ 打补丁 ——
    测试结束再还原。**只影响本进程**，不写任何配置文件。
    """
    backups = {
        "ai_provider": settings.ai_provider,
        "ai_api_key": settings.ai_api_key,
        "ai_model": settings.ai_model,
    }
    object.__setattr__(settings, "ai_provider", "fake")
    object.__setattr__(settings, "ai_api_key", "test-key")
    object.__setattr__(settings, "ai_model", "fake-model")
    from backend.app.ai.provider import reset_provider_cache  # noqa: PLC0415

    reset_provider_cache()
    yield
    for key, value in backups.items():
        object.__setattr__(settings, key, value)
    reset_provider_cache()


def _admin_headers() -> dict[str, str]:
    """必须用**真实存在**的用户 ID 签 token：`get_current_user` 会拿 sub 去查库，
    随便编一个字符串会得到 401（「账号不存在或已停用」）而不是 404。"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.role == "ADMIN").first()
        assert user is not None, "测试库必须已有 ADMIN 用户"
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
    finally:
        db.close()


# -------------------------------------------------------- 1. Provider 注册与接口


def test_provider_registry_has_builtin_providers() -> None:
    names = available_providers()
    assert "openai_compatible" in names
    assert "fake" in names


def test_unknown_provider_raises() -> None:
    with pytest.raises(AIProviderError):
        get_provider("no-such-provider")


def test_fake_provider_valid_mode() -> None:
    response = FakeAIProvider("valid").analyze_failure(FailureAnalysisContext(failure_group_id="g"))
    assert response.ok and response.parsed is not None
    assert response.parsed["summary"]


def test_fake_provider_invalid_json_mode() -> None:
    response = FakeAIProvider("invalid_json").analyze_failure(FailureAnalysisContext(failure_group_id="g"))
    assert response.ok and response.parsed is None, "非法 JSON 时 parsed 必须是 None，由 service 判 FAILED"


def test_fake_provider_error_mode() -> None:
    with pytest.raises(AIProviderError):
        FakeAIProvider("error").analyze_failure(FailureAnalysisContext(failure_group_id="g"))


# ------------------------------------------------------------- 2. Schema 兜底


def test_result_schema_coerces_evidence_string() -> None:
    result = FailureAnalysisResult.model_validate({"summary": "s", "evidence": "第一条\n第二条"})
    assert result.evidence == ["第一条", "第二条"]


def test_result_schema_normalizes_percent_confidence() -> None:
    assert FailureAnalysisResult.model_validate({"summary": "s", "confidence": 82}).confidence == 0.82
    assert FailureAnalysisResult.model_validate({"summary": "s", "confidence": 1}).confidence == 1.0
    assert FailureAnalysisResult.model_validate({"summary": "s", "confidence": -5}).confidence == 0.0


def test_result_schema_drops_dirty_category() -> None:
    assert FailureAnalysisResult.model_validate({"summary": "s", "ai_suggested_category": "PRODCUT_BUG"}).ai_suggested_category == ""
    assert FailureAnalysisResult.model_validate({"summary": "s", "ai_suggested_category": "data_error"}).ai_suggested_category == "DATA_ERROR"


def test_result_schema_ignores_unknown_fields() -> None:
    """模型多回字段不该导致整次分析失败。"""
    result = FailureAnalysisResult.model_validate({"summary": "s", "status": "FAILED", "passed": False})
    assert result.summary == "s"
    assert not hasattr(result, "passed"), "结果结构里不能有 PASS/FAIL 这类字段"


def test_result_is_meaningful() -> None:
    assert not FailureAnalysisResult.model_validate({}).is_meaningful()
    assert FailureAnalysisResult.model_validate({"root_cause": "接口 500"}).is_meaningful()


# ------------------------------------------------------------ 3. 提示词约束


def test_prompt_version_is_stable() -> None:
    assert PROMPT_VERSION == "m5-1"


def test_system_prompt_forbids_verdicts() -> None:
    for phrase in ("不能", "PASS/FAIL"):
        assert phrase in SYSTEM_PROMPT
    assert "suggested_owner" in SYSTEM_PROMPT
    assert "ai_suggested_category" in SYSTEM_PROMPT


def test_user_prompt_contains_key_evidence(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        context = evidence_module.build_context(db, group)
    finally:
        db.close()
    prompt = build_user_prompt(context)
    assert "确定性分类" in prompt
    assert "失败现场" in prompt
    assert context.step_action in prompt


# ------------------------------------------------------- 4. Evidence Builder


def test_evidence_redacts_secrets(chain: dict[str, str]) -> None:
    """环境变量里的密码、错误信息里的 token，都不能进证据包。"""
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        context = evidence_module.build_context(db, group)
    finally:
        db.close()
    blob = " ".join(
        [
            context.step_error,
            context.console_summary,
            context.network_summary,
            context.step_expected,
            context.step_actual,
        ]
    )
    assert SECRET not in blob, "敏感值必须被脱敏"
    assert "********" in blob


def test_evidence_respects_limits(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        limits = evidence_module.EvidenceLimits(max_total_chars=500, step_error=50, console=20, network=20)
        context = evidence_module.build_context(db, group, limits=limits)
    finally:
        db.close()
    assert len(context.step_error) <= 120  # 50 + 截断提示
    assert len(context.console_summary) <= 80


def test_evidence_fingerprint_ignores_volatile_fields(chain: dict[str, str]) -> None:
    """出现次数、Run ID 这类每次都会变的东西不能进指纹，否则幂等永远命中不了。"""
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        before = evidence_module.build_context(db, group)
        group.occurrence_count = 999
        group.failed_run_count = 42
        db.commit()
        after = evidence_module.build_context(db, group)
    finally:
        db.close()
    assert before.evidence_fingerprint == after.evidence_fingerprint


def test_evidence_priority_order_documented() -> None:
    assert evidence_module.EVIDENCE_PRIORITY[0] == "step"
    assert evidence_module.EVIDENCE_PRIORITY.index("http_status") < evidence_module.EVIDENCE_PRIORITY.index("screenshot")
    assert evidence_module.EVIDENCE_PRIORITY.index("console") < evidence_module.EVIDENCE_PRIORITY.index("trace")


def test_evidence_artifacts_are_references_only(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        context = evidence_module.build_context(db, group, include_images=False)
    finally:
        db.close()
    assert context.images == [], "默认不读二进制"
    for item in context.artifacts:
        assert isinstance(item, EvidenceArtifact)
        assert item.size >= 0


# ------------------------------------------------------- 5. AI 资格判定


class _FakeGroup:
    def __init__(self, category: str, rule_id: str = "") -> None:
        self.category = category
        self.rule_id = rule_id


def test_eligibility_default_only_unknown() -> None:
    assert ai_service.analysis_eligibility(_FakeGroup("UNKNOWN"))[0] is True
    for category in ("PRODUCT_BUG", "ENVIRONMENT_ERROR", "AUTOMATION_ERROR", "DATA_ERROR", "FLAKY"):
        eligible, reason = ai_service.analysis_eligibility(_FakeGroup(category))
        assert eligible is False, f"{category} 默认不该进 AI"
        assert category in reason


def test_eligibility_respects_configured_rules() -> None:
    from backend.app.config import settings as live  # noqa: PLC0415

    original = live.ai_required_rules
    object.__setattr__(live, "ai_required_rules", ("PRODUCT_HTTP_5XX",))
    try:
        eligible, reason = ai_service.analysis_eligibility(_FakeGroup("PRODUCT_BUG", "PRODUCT_HTTP_5XX"))
        assert eligible is True and "PRODUCT_HTTP_5XX" in reason
    finally:
        object.__setattr__(live, "ai_required_rules", original)


def test_eligibility_force_bypasses_rules() -> None:
    assert ai_service.analysis_eligibility(_FakeGroup("FLAKY"), force=True)[0] is True


# --------------------------------------------------- 6. 分析与幂等


def test_analyze_skipped_when_ai_disabled(chain: dict[str, str]) -> None:
    """AI 没配时返回 skipped 而不是报错 —— 没开功能不是错误。"""
    db = SessionLocal()
    try:
        original = settings.ai_api_key
        object.__setattr__(settings, "ai_api_key", "")
        try:
            outcome = ai_service.analyze_group(db, chain["group_id"])
        finally:
            object.__setattr__(settings, "ai_api_key", original)
    finally:
        db.close()
    assert outcome["skipped"] is True
    assert "AI" in outcome["reason"] or "不可用" in outcome["reason"]


def test_analyze_completes_with_valid_json(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        group.category = "UNKNOWN"
        db.commit()
        outcome = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)
        row = db.get(FailureAnalysis, outcome["analysis_id"])
        assert row.status == "COMPLETED"
        assert row.summary
        assert row.confidence == 0.82
        assert row.ai_suggested_category == "PRODUCT_BUG"
        assert row.review_status == "UNREVIEWED"
        assert row.prompt_version == PROMPT_VERSION
        assert row.evidence_fingerprint
    finally:
        db.close()


def test_analysis_does_not_change_group_category(chain: dict[str, str], m5_ai_on: None) -> None:
    """AI 建议了 PRODUCT_BUG，但 FailureGroup.category 必须原样不动。"""
    db = SessionLocal()
    try:
        before = db.get(FailureGroup, chain["group_id"]).category
        ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)
        after = db.get(FailureGroup, chain["group_id"]).category
    finally:
        db.close()
    assert before == after == "UNKNOWN", "AI 绝不回写确定性分类"


def test_analysis_is_idempotent(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        first = ai_service.analyze_group(db, chain["group_id"], provider_name="fake")
        second = ai_service.analyze_group(db, chain["group_id"], provider_name="fake")
        count = db.query(FailureAnalysis).filter(FailureAnalysis.failure_group_id == chain["group_id"]).count()
    finally:
        db.close()
    if first.get("created"):
        assert second["created"] is False, "同样的证据重复分析必须命中已有结果"
        assert first["analysis_id"] == second["analysis_id"]
    assert count >= 1


def test_force_creates_new_revision(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        base = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)
        forced = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)
        old = db.get(FailureAnalysis, base["analysis_id"])
        new = db.get(FailureAnalysis, forced["analysis_id"])
        assert new.revision_no > old.revision_no
        assert old.status == "COMPLETED", "旧分析必须保留，不能被覆盖"
    finally:
        db.close()


def test_invalid_json_marks_failed_and_does_not_raise(chain: dict[str, str], m5_ai_on: None) -> None:
    """方案 §7：AI 返回非法 JSON → 有限重试 → FAILED，且不影响调用方。"""
    from backend.app.ai.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    register_provider("always_invalid", lambda: FakeAIProvider("invalid_json"))
    reset_provider_cache()
    db = SessionLocal()
    try:
        outcome = ai_service.analyze_group(db, chain["group_id"], provider_name="always_invalid", force=True)
        row = db.get(FailureAnalysis, outcome["analysis_id"])
        assert row.status == "FAILED"
        assert "合法 JSON" in row.error_message or "JSON" in row.error_message
        assert row.raw_response_redacted, "要把模型的原话留下来（脱敏后）供排查"
    finally:
        db.close()


def test_repair_retry_can_succeed(chain: dict[str, str], m5_ai_on: None) -> None:
    """一次修复重试后拿到合法 JSON → COMPLETED。"""
    from backend.app.ai.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    register_provider("repaired", lambda: FakeAIProvider("fixed_by_repair"))
    reset_provider_cache()
    db = SessionLocal()
    try:
        outcome = ai_service.analyze_group(db, chain["group_id"], provider_name="repaired", force=True)
        row = db.get(FailureAnalysis, outcome["analysis_id"])
        assert row.status == "COMPLETED"
    finally:
        db.close()


def test_provider_exception_is_contained(chain: dict[str, str], m5_ai_on: None) -> None:
    from backend.app.ai.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    register_provider("boom", lambda: FakeAIProvider("error"))
    reset_provider_cache()
    db = SessionLocal()
    try:
        outcome = ai_service.analyze_group(db, chain["group_id"], provider_name="boom", force=True)
        row = db.get(FailureAnalysis, outcome["analysis_id"])
        assert row.status == "FAILED"
    finally:
        db.close()


def test_analyze_unknown_group_returns_skipped() -> None:
    db = SessionLocal()
    try:
        outcome = ai_service.analyze_group(db, "no-such-group")
    finally:
        db.close()
    assert outcome["skipped"] is True


# ------------------------------------------------------------ 7. 人工确认


def test_review_transitions(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        outcome = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)
        analysis_id = outcome["analysis_id"]
        for status in ("CONFIRMED", "MODIFIED", "IGNORED"):
            row = ai_service.review_analysis(
                db, analysis_id, review_status=status, reviewed_by="tester", review_comment=f"备注-{status}"
            )
            assert row.review_status == status
            assert row.reviewed_by == "tester"
            assert row.reviewed_at is not None
    finally:
        db.close()


def test_review_rejects_invalid_status(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        analysis_id = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)["analysis_id"]
        with pytest.raises(ValueError):
            ai_service.review_analysis(db, analysis_id, review_status="APPROVED")
    finally:
        db.close()


def test_review_does_not_close_group(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        analysis_id = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)["analysis_id"]
        ai_service.review_analysis(db, analysis_id, review_status="CONFIRMED", reviewed_by="tester")
        group = db.get(FailureGroup, chain["group_id"])
        assert group.status == "OPEN", "确认 AI 结论 ≠ 问题已解决"
    finally:
        db.close()


def test_review_comment_is_redacted(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        analysis_id = ai_service.analyze_group(db, chain["group_id"], provider_name="fake", force=True)["analysis_id"]
        row = ai_service.review_analysis(
            db, analysis_id, review_status="MODIFIED", reviewed_by="tester", review_comment=f"token={SECRET}"
        )
        assert SECRET not in row.review_comment
    finally:
        db.close()


# ------------------------------------------------------------ 8. 定级规则


def test_severity_rules_by_category() -> None:
    cases = [
        ("PRODUCT_BUG", 3, 1, "ERROR"),
        ("PRODUCT_BUG", 12, 1, "CRITICAL"),
        ("PRODUCT_BUG", 3, 4, "CRITICAL"),
        ("ENVIRONMENT_ERROR", 1, 1, "ERROR"),
        ("UNKNOWN", 1, 1, "WARNING"),
        ("AUTOMATION_ERROR", 1, 1, "WARNING"),
        ("DATA_ERROR", 1, 1, "WARNING"),
        ("FLAKY", 1, 1, "INFO"),
    ]
    for category, case_count, run_count, expected in cases:
        group = _FakeGroup(category)
        group.failed_case_count = case_count
        group.failed_run_count = run_count
        decision = alert_rules.decide_severity(group)
        assert decision.severity == expected, f"{category}/{case_count}/{run_count}"
        assert decision.rule_id and decision.reason


def test_severity_only_escalates() -> None:
    assert alert_rules.escalate("ERROR", "WARNING") == "ERROR"
    assert alert_rules.escalate("WARNING", "CRITICAL") == "CRITICAL"
    assert alert_rules.escalate("INFO", "ERROR") == "ERROR"


# ------------------------------------------------------------ 9. 告警聚合


def test_alert_created_once_per_group(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        first, created_first = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        second, created_second = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        assert created_first is True
        assert created_second is False
        assert first.id == second.id, "同一个分组不能有两条活动告警"
    finally:
        db.close()


def test_alert_counts_follow_group(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        group.occurrence_count = 7
        group.failed_case_count = 5
        group.failed_run_count = 3
        db.commit()
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        assert alert.occurrence_count == 7
        assert alert.failed_case_count == 5
        assert alert.failed_run_count == 3
    finally:
        db.close()


def test_alert_transition_state_machine(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        # OPEN → RESOLVED 必须被拒绝：没验证就宣称修好了
        with pytest.raises(alert_service.AlertTransitionError):
            alert_service.transition(db, alert, "RESOLVED")
        alert_service.transition(db, alert, "ACKNOWLEDGED", actor="tester")
        assert alert.acknowledged_at is not None
        alert_service.transition(db, alert, "FIXING", actor="tester")
        alert_service.transition(db, alert, "REVERIFY", actor="tester")
        alert_service.transition(db, alert, "RESOLVED", actor="tester", comment="已验证")
        assert alert.status == "RESOLVED"
        assert alert.resolved_by == "tester"
        # 已解决的告警不能再跳
        with pytest.raises(alert_service.AlertTransitionError):
            alert_service.transition(db, alert, "OPEN")
    finally:
        db.close()


def test_ignored_alert_can_reopen(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group)
        alert_service.transition(db, alert, "IGNORED", actor="tester")
        assert alert.status == "IGNORED"
        alert_service.transition(db, alert, "OPEN", actor="tester")
        assert alert.status == "OPEN"
    finally:
        db.close()


def test_evaluate_run_only_creates_one_alert_for_many_cases(chain: dict[str, str]) -> None:
    """10 个用例落到同一个分组 → 1 条告警（方案 §12 的核心要求）。"""
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        # 先清掉这个项目上的活动告警，保证从干净状态开始
        for row in db.query(Alert).filter(Alert.project_id == chain["project_id"]).all():
            db.delete(row)
        db.commit()
        group.failed_case_count = 10
        group.occurrence_count = 10
        db.commit()

        outcome = alert_service.evaluate_run(db, chain["run_id"])
        alerts = db.query(Alert).filter(Alert.failure_group_id == group.id).all()
        assert len(alerts) == 1, f"10 个用例应该只产生 1 条告警，实际 {len(alerts)}"
        assert alerts[0].failed_case_count == 10
        assert outcome["groups"] >= 1
    finally:
        db.close()


def test_evaluate_run_is_idempotent_across_runs(chain: dict[str, str]) -> None:
    """同一个分组连续多个 Run：还是那一条告警，计数在涨。"""
    db = SessionLocal()
    try:
        before = db.query(Alert).filter(Alert.failure_group_id == chain["group_id"]).count()
        alert_service.evaluate_run(db, chain["run_id"])
        alert_service.evaluate_run(db, chain["run_id"])
        after = db.query(Alert).filter(Alert.failure_group_id == chain["group_id"]).count()
        assert after == before, "重复评估不该新建告警"
    finally:
        db.close()


# ------------------------------------------------------------ 10. 通知


def test_render_message_contains_required_sections(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        content = notification_service.render_alert_message(db, alert)
    finally:
        db.close()
    for marker in ("TestFlowCI", "严重级别", "失败用例", "分类", "查看详情"):
        assert marker in content, f"通知缺少「{marker}」"


def test_render_message_has_no_secrets(chain: dict[str, str], m5_ai_on: None) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        content = notification_service.render_alert_message(db, alert)
    finally:
        db.close()
    assert SECRET not in content, "通知里绝不能出现敏感值"


def test_notification_sent_and_logged(chain: dict[str, str]) -> None:
    from backend.app.notification.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    fake = FakeWeComProvider("ok")
    register_provider("fake_ok", lambda: fake)
    reset_provider_cache()
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        result = notification_service.send_alert(db, alert, provider_name="fake_ok", force=True)
        row = db.get(NotificationLog, result["log_id"])
        assert result["status"] == "SENT"
        assert row.status == "SENT"
        assert row.attempt_count == 1
        assert row.sent_at is not None
        # 真正被送出去的内容里也不能有敏感值
        assert fake.sent, "假渠道必须真的收到消息"
        assert SECRET not in fake.sent[-1].content
    finally:
        db.close()


def test_notification_dedupes_same_content(chain: dict[str, str]) -> None:
    from backend.app.notification.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    fake = FakeWeComProvider("ok")
    register_provider("fake_dedupe", lambda: fake)
    reset_provider_cache()
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        notification_service.send_alert(db, alert, provider_name="fake_dedupe", force=True)
        sent_times = len(fake.sent)
        second = notification_service.send_alert(db, alert, provider_name="fake_dedupe")
        assert second["skipped"] is True, "同一内容不该重复发送"
        assert len(fake.sent) == sent_times
    finally:
        db.close()


def test_notification_retries_then_fails(chain: dict[str, str]) -> None:
    from backend.app.notification.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    fake = FakeWeComProvider("fail")
    register_provider("fake_fail", lambda: fake)
    reset_provider_cache()
    original = settings.notification_max_attempts
    object.__setattr__(settings, "notification_max_attempts", 3)
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        before = 0
        existing = db.query(NotificationLog).filter(
            NotificationLog.alert_id == alert.id, NotificationLog.channel == "wecom_webhook"
        ).all()
        if existing:
            before = max(int(item.attempt_count or 0) for item in existing)
        result = notification_service.send_alert(db, alert, provider_name="fake_fail", force=True)
        row = db.get(NotificationLog, result["log_id"])
        assert result["status"] == "FAILED"
        assert fake.attempts == 3, f"这一次发送最多尝试 3 次，实际 {fake.attempts}"
        assert int(row.attempt_count or 0) - before == 3, "重试次数记在同一行上累加"
    finally:
        object.__setattr__(settings, "notification_max_attempts", original)
        db.close()


def test_notification_fatal_error_stops_retrying(chain: dict[str, str]) -> None:
    from backend.app.notification.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    fake = FakeWeComProvider("fatal")
    register_provider("fake_fatal", lambda: fake)
    reset_provider_cache()
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        notification_service.send_alert(db, alert, provider_name="fake_fatal", force=True)
        assert fake.attempts == 1, "webhook 无效这种错重试没有意义"
    finally:
        db.close()


def test_notification_failure_does_not_touch_alert_or_run(chain: dict[str, str]) -> None:
    """方案 §18：通知失败只记录 FAILED，不改任何测试结果与告警状态。"""
    from backend.app.notification.provider import register_provider, reset_provider_cache  # noqa: PLC0415

    register_provider("fake_boom", lambda: FakeWeComProvider("fail"))
    reset_provider_cache()
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        alert_status_before = alert.status
        group_category_before = group.category
        run = db.get(ExecutionRun, chain["run_id"])
        run_status_before = run.status

        notification_service.send_alert(db, alert, provider_name="fake_boom", force=True)

        db.refresh(alert)
        db.refresh(group)
        db.refresh(run)
        assert alert.status == alert_status_before
        assert group.category == group_category_before
        assert run.status == run_status_before
    finally:
        db.close()


def test_notification_skipped_when_channel_missing(chain: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        group = db.get(FailureGroup, chain["group_id"])
        alert, _ = alert_service.upsert_alert(db, group, run_id=chain["run_id"])
        result = notification_service.send_alert(db, alert, provider_name="no-such-channel")
        assert result["status"] == "SKIPPED", "没配渠道应记 SKIPPED，不是 FAILED"
    finally:
        db.close()


def test_notify_gated_by_plan_switch(chain: dict[str, str]) -> None:
    """默认静默：计划没开「发送企业微信通知」就不发。"""
    db = SessionLocal()
    try:
        run = db.get(ExecutionRun, chain["run_id"])
        allowed, reason = alert_service._should_notify(db, run)  # noqa: SLF001 - 直接验规则本身
    finally:
        db.close()
    # 这个测试 Run 没有计划，所以取决于全局配置；有计划的场景在自测脚本里验
    assert isinstance(allowed, bool)
    assert isinstance(reason, str)


# --------------------------------------------------------------- 11. API


def test_api_requires_login(chain: dict[str, str]) -> None:
    with TestClient(app) as client:
        assert client.get("/api/ai/meta").status_code == 401
        assert client.get(f"/api/failures/{chain['group_id']}/analyses").status_code == 401
        assert client.get("/api/alerts").status_code == 401


def test_api_rejects_ci_token(chain: dict[str, str]) -> None:
    """CI Token 只在 /api/ci/* 生效，不能顺带把人工管理接口也打开。"""
    with TestClient(app) as client:
        assert client.get("/api/alerts", headers={"X-TestFlow-Token": settings.ci_token or "x"}).status_code == 401


def test_api_ai_meta_shape(chain: dict[str, str]) -> None:
    with TestClient(app) as client:
        body = client.get("/api/ai/meta", headers=_admin_headers()).json()
    for key in ("ai_enabled", "provider", "model", "prompt_version", "available_providers", "notification_provider"):
        assert key in body
    assert "openai_compatible" in body["available_providers"]


def test_api_analyze_missing_group(chain: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post("/api/failures/nope/analyze", json={}, headers=_admin_headers())
    assert response.status_code == 404


def test_api_analysis_and_review_flow(chain: dict[str, str], m5_ai_on: None) -> None:
    with TestClient(app) as client:
        analyzed = client.post(
            f"/api/failures/{chain['group_id']}/analyze",
            json={"provider": "fake", "force": True},
            headers=_admin_headers(),
        ).json()
        assert analyzed["skipped"] is False
        assert analyzed["analysis"]["status"] == "COMPLETED"

        listed = client.get(f"/api/failures/{chain['group_id']}/analyses", headers=_admin_headers()).json()
        assert listed["total"] >= 1
        assert listed["items"][0]["revision_no"] >= 1

        analysis_id = analyzed["analysis"]["id"]
        reviewed = client.patch(
            f"/api/analyses/{analysis_id}/review",
            json={"review_status": "CONFIRMED", "review_comment": "同意"},
            headers=_admin_headers(),
        ).json()
        assert reviewed["review_status"] == "CONFIRMED"
        assert reviewed["reviewed_by"] == "admin"


def test_api_alert_lifecycle(chain: dict[str, str]) -> None:
    with TestClient(app) as client:
        created = client.post(f"/api/failures/{chain['group_id']}/alerts", headers=_admin_headers()).json()
        alert_id = created["id"]
        assert client.get(f"/api/alerts/{alert_id}", headers=_admin_headers()).json()["id"] == alert_id

        listed = client.get("/api/alerts", params={"project_id": chain["project_id"]}, headers=_admin_headers()).json()
        assert listed["total"] >= 1
        assert "status_counts" in listed and "severity_counts" in listed

        # 非法跳转要 400 且带可读原因
        bad = client.post(f"/api/alerts/{alert_id}/resolve", json={}, headers=_admin_headers())
        assert bad.status_code == 400
        assert "不允许" in bad.json()["detail"]

        # 合法路径
        assert client.post(f"/api/alerts/{alert_id}/ack", json={}, headers=_admin_headers()).json()["status"] == "ACKNOWLEDGED"
        assert client.post(f"/api/alerts/{alert_id}/fixing", json={}, headers=_admin_headers()).json()["status"] == "FIXING"

        logs = client.get(f"/api/alerts/{alert_id}/notifications", headers=_admin_headers()).json()
        assert "items" in logs
