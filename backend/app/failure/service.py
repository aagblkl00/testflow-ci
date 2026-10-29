"""Failure Analysis Service：把一次执行的结果变成可调查的问题。

职责边界（方案 §44）：

    Executor               →  执行，产出 Run / Case / Attempt / Step / Artifact
    FailureAnalysisService →  Fingerprint、Classification、Group、Occurrence

**Executor 里没有任何 FailureGroup 相关逻辑**，它甚至不知道失败中心的存在。
这样做的直接好处是：改分类规则不用碰执行器，改执行器不用担心聚合。

聚合入口 `analyze_run()` 是**幂等**的。同一个 Run 处理多少次，结果都一样：

    Fingerprint  → unique(fingerprint)
    Group        → unique(project_id, fingerprint_id)
    Occurrence   → unique(dedupe_key)

三层都靠数据库唯一约束兜底，而不是 `if not exists: create()`。
后者在「两个 Run 同时算出同一个指纹」时会真的建出两条 Group ——
这不是理论上可能，而是定时回归最容易踩的坑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import Integer, distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..models import (
    CaseResult,
    CaseResultAttempt,
    ExecutionRun,
    FailureFingerprint,
    FailureGroup,
    FailureOccurrence,
    StepResult,
    utcnow,
)
from . import classifier as classifier_module
from .fingerprint import FailureContext, FingerprintResult, build_context, build_title, compute

logger = logging.getLogger("testflow.failure.service")

# 只有这些状态的步骤/用例才算「真的失败了」
FAILING_STEP_STATUSES = ("FAILED", "ERROR")
FAILING_CASE_STATUSES = ("FAILED", "ERROR")
FLAKY_CASE_STATUS = "FLAKY"


@dataclass(slots=True)
class FailureFinding:
    """一次失败的分析结论。"""

    fingerprint: FingerprintResult
    classification: classifier_module.FailureClassification
    step_payload: dict[str, Any]
    is_flaky: bool

    @property
    def category(self) -> str:
        return self.classification.category


# --------------------------------------------------------------------- 输入适配


def step_payload(step: Any) -> dict[str, Any]:
    """把 StepResult 行或执行器产出的 dict 统一成同一个形状。

    两边字段名本来就是一致的（执行器的 result 字典就是写库的来源），
    所以这里不需要一张映射表 —— 这也是当初故意让执行器字段名跟列名对齐的原因。
    """
    if isinstance(step, dict):
        return step
    return {column.name: getattr(step, column.name) for column in step.__table__.columns}


def context_from_payload(payload: dict[str, Any]) -> FailureContext:
    return build_context(
        action=str(payload.get("action") or ""),
        error=str(payload.get("error") or ""),
        expected=str(payload.get("expected") or ""),
        actual=str(payload.get("actual") or ""),
        page_url=str(payload.get("page_url") or ""),
        locator=payload.get("locator") or {},
        request_method=str(payload.get("request_method") or ""),
        request_url=str(payload.get("request_url") or ""),
        http_status=payload.get("http_status"),
        console_summary=str(payload.get("console_summary") or ""),
        network_summary=str(payload.get("network_summary") or ""),
        js_error=str(payload.get("js_error") or ""),
        element_id=str(payload.get("element_id") or ""),
    )


def build_finding(payload: dict[str, Any], *, is_flaky: bool = False) -> FailureFinding:
    """从一个失败步骤算出指纹与分类。纯函数，无副作用，可单测。"""
    context = context_from_payload(payload)
    fingerprint = compute(context)
    classification = classifier_module.classify(context, is_flaky=is_flaky)
    return FailureFinding(
        fingerprint=fingerprint,
        classification=classification,
        step_payload=payload,
        is_flaky=is_flaky,
    )


def pick_decisive_attempt(case_result: CaseResult) -> CaseResultAttempt | None:
    """挑出「真正该被归因」的那一次尝试。

    - 普通失败：取最后一次失败尝试（重试都失败了，说明问题真实存在）
    - Flaky：取**第一次**失败尝试。重试已经通过了，最后一次是 PASS，
      要看的是那次没跑通的现场。
    """
    attempts: Sequence[CaseResultAttempt] = list(case_result.attempts or [])
    failing = [item for item in attempts if item.status in FAILING_STEP_STATUSES]
    if not failing:
        return None
    return failing[0] if case_result.status == FLAKY_CASE_STATUS else failing[-1]


def failing_steps(attempt: CaseResultAttempt) -> list[StepResult]:
    return [step for step in (attempt.step_results or []) if step.status in FAILING_STEP_STATUSES]


# --------------------------------------------------------------------- 幂等写入


def upsert_fingerprint(db: Session, result: FingerprintResult) -> FailureFingerprint:
    """按 fingerprint 取或建。并发时靠唯一约束 + 冲突重查。"""
    existing = db.execute(
        select(FailureFingerprint).where(FailureFingerprint.fingerprint == result.fingerprint)
    ).scalars().first()
    if existing is not None:
        return existing

    row = FailureFingerprint(
        fingerprint=result.fingerprint,
        strategy=result.strategy,
        failure_type=result.failure_type,
        exception_type=result.exception_type[:64],
        normalized_message=result.normalized_message,
        normalized_expected=result.normalized_expected,
        normalized_actual=result.normalized_actual,
        page_url=result.page_url[:512],
        request_method=result.request_method[:16],
        request_url=result.request_url[:1024],
        http_status=result.http_status,
        locator=result.locator[:512],
        step_action=result.step_action[:64],
        sample_message=result.sample_message[:500],
    )
    return _insert_or_reread(
        db,
        row,
        lambda: db.execute(
            select(FailureFingerprint).where(FailureFingerprint.fingerprint == result.fingerprint)
        ).scalars().first(),
    )


def upsert_group(
    db: Session,
    *,
    project_id: str,
    fingerprint_row: FailureFingerprint,
    finding: FailureFinding,
) -> FailureGroup:
    """按 (project_id, fingerprint_id) 取或建。

    Group 的 category 有一个明确的升级规则：
    如果已有分类是 FLAKY（因为历史上只有 Flaky 出现过），而本次来了一个
    真实失败，那么以非 Flaky 的分类为准 —— 一个能稳定复现的失败，
    不应该被同指纹的历史 Flaky 记录掩盖。
    """
    existing = db.execute(
        select(FailureGroup).where(
            FailureGroup.project_id == project_id,
            FailureGroup.fingerprint_id == fingerprint_row.id,
        )
    ).scalars().first()

    classification = finding.classification
    if existing is not None:
        if existing.category == classifier_module.CATEGORY_FLAKY and not finding.is_flaky:
            existing.category = classification.category
            existing.rule_id = classification.rule_id
            existing.rule_reason = classification.reason[:512]
        return existing

    row = FailureGroup(
        project_id=project_id,
        fingerprint_id=fingerprint_row.id,
        title=build_title(finding.fingerprint)[:255],
        category=classification.category,
        rule_id=classification.rule_id[:64],
        rule_reason=classification.reason[:512],
        status="OPEN",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    return _insert_or_reread(
        db,
        row,
        lambda: db.execute(
            select(FailureGroup).where(
                FailureGroup.project_id == project_id,
                FailureGroup.fingerprint_id == fingerprint_row.id,
            )
        ).scalars().first(),
    )


def occurrence_dedupe_key(
    *,
    group_id: str,
    run_id: str,
    case_result_id: str,
    attempt_id: str | None,
    step_result_id: str | None,
) -> str:
    parts = [group_id, run_id, case_result_id, attempt_id or "-", step_result_id or "-"]
    return "|".join(parts)[:160]


def insert_occurrence(
    db: Session,
    *,
    group: FailureGroup,
    fingerprint_id: str,
    run: ExecutionRun,
    case_result: CaseResult,
    attempt: CaseResultAttempt | None,
    step: StepResult | None,
    is_flaky: bool,
    category: str = "UNKNOWN",
) -> tuple[FailureOccurrence, bool]:
    """写入一条 occurrence。返回 (行, 是否新建)。

    「是否新建」这个返回值是幂等验收的关键：重复分析同一个 Run 时，
    这个方法必须返回 created=False，且数据库行数不变。
    """
    key = occurrence_dedupe_key(
        group_id=group.id,
        run_id=run.id,
        case_result_id=case_result.id,
        attempt_id=attempt.id if attempt else None,
        step_result_id=step.id if step else None,
    )
    existing = db.execute(
        select(FailureOccurrence).where(FailureOccurrence.dedupe_key == key)
    ).scalars().first()
    if existing is not None:
        return existing, False

    row = FailureOccurrence(
        failure_group_id=group.id,
        fingerprint_id=fingerprint_id,
        run_id=run.id,
        case_result_id=case_result.id,
        attempt_id=attempt.id if attempt else None,
        step_result_id=step.id if step else None,
        case_status=case_result.status,
        is_flaky=bool(is_flaky),
        category=category,
        dedupe_key=key,
    )
    stored = _insert_or_reread(
        db,
        row,
        lambda: db.execute(select(FailureOccurrence).where(FailureOccurrence.dedupe_key == key)).scalars().first(),
    )
    return stored, stored is row


def _insert_or_reread(db: Session, row: Any, reread: Any) -> Any:
    """插入一行；撞唯一约束就回滚到保存点后重新读取。

    用 savepoint（begin_nested）而不是整体 rollback：一次聚合里会有几十条
    插入，必须只牺牲冲突的那一条，不能把前面已经成功的记录一起丢掉。
    """
    try:
        with db.begin_nested():
            db.add(row)
    except IntegrityError:
        found = reread()
        if found is None:
            raise
        return found
    return row


# --------------------------------------------------------------------- 计数刷新


def recompute_group_counters(db: Session, group_ids: Iterable[str]) -> int:
    """按实际 occurrence 重算计数，而不是在写入路径上做 += 。

    `+= 1` 在重复聚合、并发写入下会算错；重算虽然多一次查询，
    但结果永远等于数据库里的真相，也让 M3-13 的幂等验收能被证明。
    """
    touched = 0
    for group_id in {item for item in group_ids if item}:
        group = db.get(FailureGroup, group_id)
        if group is None:
            continue
        total, cases, runs, flaky, first_seen, last_seen = db.execute(
            select(
                func.count(FailureOccurrence.id),
                func.count(distinct(FailureOccurrence.case_result_id)),
                func.count(distinct(FailureOccurrence.run_id)),
                func.sum(func.cast(FailureOccurrence.is_flaky, Integer)),
                func.min(FailureOccurrence.created_at),
                func.max(FailureOccurrence.created_at),
            ).where(FailureOccurrence.failure_group_id == group_id)
        ).one()

        group.occurrence_count = int(total or 0)
        group.failed_case_count = int(cases or 0)
        group.failed_run_count = int(runs or 0)
        group.flaky_count = int(flaky or 0)
        if first_seen:
            group.first_seen_at = first_seen
        if last_seen:
            group.last_seen_at = last_seen

        latest = db.execute(
            select(FailureOccurrence)
            .where(FailureOccurrence.failure_group_id == group_id)
            .order_by(FailureOccurrence.created_at.desc(), FailureOccurrence.id.desc())
        ).scalars().first()
        if latest is not None:
            group.latest_run_id = latest.run_id
            group.latest_case_result_id = latest.case_result_id
        touched += 1
    return touched


# --------------------------------------------------------------------- 主入口


def analyze_run(db: Session, run_id: str) -> dict[str, Any]:
    """扫描一次 Run，建立 Fingerprint / Group / Occurrence。幂等。

    PASS 的用例完全不参与 —— 方案 §54 的性能要求：正常通过不产生任何分析成本。
    """
    run = db.get(ExecutionRun, run_id)
    if run is None:
        return {"run_id": run_id, "analyzed": False, "reason": "run_not_found"}

    needs_analysis = (run.failed_cases or 0) + (run.error_cases or 0) + (run.flaky_cases or 0) > 0
    if not needs_analysis:
        return {"run_id": run_id, "analyzed": False, "reason": "no_failures", "occurrences": 0}

    cases = db.execute(
        select(CaseResult)
        .options(
            selectinload(CaseResult.attempts).selectinload(CaseResultAttempt.step_results),
        )
        .where(
            CaseResult.run_id == run_id,
            CaseResult.status.in_([*FAILING_CASE_STATUSES, FLAKY_CASE_STATUS]),
        )
        .order_by(CaseResult.case_code, CaseResult.id)
    ).scalars().all()

    group_ids: list[str] = []
    created_occurrences = 0
    skipped_without_step = 0
    category_counter: dict[str, int] = {}

    for case_result in cases:
        is_flaky = case_result.status == FLAKY_CASE_STATUS
        attempt = pick_decisive_attempt(case_result)
        if attempt is None:
            skipped_without_step += 1
            continue
        steps = failing_steps(attempt)
        if not steps:
            skipped_without_step += 1
            continue

        for step in steps:
            finding = build_finding(step_payload(step), is_flaky=is_flaky)
            fingerprint_row = upsert_fingerprint(db, finding.fingerprint)
            group = upsert_group(
                db,
                project_id=run.project_id,
                fingerprint_row=fingerprint_row,
                finding=finding,
            )
            _occurrence, created = insert_occurrence(
                db,
                group=group,
                fingerprint_id=fingerprint_row.id,
                run=run,
                case_result=case_result,
                attempt=attempt,
                step=step,
                is_flaky=is_flaky,
                category=finding.category,
            )
            group_ids.append(group.id)
            if not created:
                # 幂等：这条已经记过了（重复分析 / 服务重启后重跑），不做任何重复计数
                continue
            created_occurrences += 1
            category_counter[finding.category] = category_counter.get(finding.category, 0) + 1

    db.flush()
    recompute_group_counters(db, group_ids)
    db.commit()

    summary = {
        "run_id": run_id,
        "analyzed": True,
        "failed_cases": len(cases),
        "occurrences": created_occurrences,
        "groups_touched": len(set(group_ids)),
        "skipped_without_step": skipped_without_step,
        "categories": category_counter,
    }
    logger.info("失败分析完成 %s", summary)
    return summary


__all__ = [
    "FailureFinding",
    "analyze_run",
    "build_finding",
    "context_from_payload",
    "failing_steps",
    "insert_occurrence",
    "occurrence_dedupe_key",
    "pick_decisive_attempt",
    "recompute_group_counters",
    "step_payload",
    "upsert_fingerprint",
    "upsert_group",
]
