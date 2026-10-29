"""AI 分析服务：资格判定 → 证据 → 调用 → 结构化校验 → 落库 → 人工确认。

**这个模块永不让 AI 影响测试结果。** 它写死的规矩：

- 只写 `failure_analyses` 一张表，不碰 `failure_groups.category`、
  不碰 `execution_runs`、不碰 `case_results`；
- 任何异常都被吞掉并转成 `status=FAILED`，**绝不向上抛** ——
  调用它的地方是 Run 结束后的钩子，那里抛异常等于让 AI 故障影响一次回归；
- AI 返回内容不合规就判 FAILED，而不是「凑合存一段文本」。存了半成品，
  后面的人就会把它当结论用。

资格判定（方案 §6）：默认只有 `UNKNOWN` 进 AI。`PRODUCT_BUG` /
`AUTOMATION_ERROR` / `ENVIRONMENT_ERROR` / `DATA_ERROR` / `FLAKY`
的确定性结论已经足够，再让模型去「重新判断」一遍，只会制造两套互相矛盾的结论。
需要放开时用 `AI_ANALYSIS_REQUIRED_RULES` 显式列规则 ID，
而不是把开关拨成「全都进 AI」。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ANALYSIS_REVIEW_STATUSES, FailureAnalysis, FailureGroup, FailureOccurrence, utcnow
from ..utils.redact import redact_text
from ..utils.timeutil import elapsed_ms, is_within
from . import evidence as evidence_module
from . import prompts
from .provider import AIProviderError, get_provider
from .schemas import FailureAnalysisContext, FailureAnalysisResult

logger = logging.getLogger("testflow.ai")

# 判定为「还在分析中」的时间窗：超过这个时长的 RUNNING 记录视为已死，
# 允许重新分析。避免进程被 kill 之后这条分组永远卡在 RUNNING。
IN_FLIGHT_SECONDS = 300
RAW_RESPONSE_LIMIT = 4000


def analysis_eligibility(group: FailureGroup, *, force: bool = False) -> tuple[bool, str]:
    """这个失败分组该不该进 AI。返回 (是否, 原因)。

    原因一定要写清楚 —— 界面上会显示「为什么没分析」，否则用户只会看到
    「没有分析结果」而不知道是没配 AI 还是规则判断不需要。
    """
    if force:
        return True, "手动触发"
    if group.category == "UNKNOWN":
        return True, "确定性规则判为 UNKNOWN，需要 AI 辅助解释"
    if group.rule_id and group.rule_id.upper() in settings.ai_required_rules:
        return True, f"规则 {group.rule_id} 被配置为需要 AI 分析"
    return False, f"确定性分类 {group.category} 已足够，不需要 AI 介入"


def compute_analysis_key(
    *,
    group_id: str,
    occurrence_id: str,
    evidence_fingerprint: str,
    prompt_version: str,
    provider: str,
    model: str,
) -> str:
    """分析幂等键。

    六段拼起来哈希：**换成别的模型、改了提示词、证据变了，都算新的分析**。
    这正是我们想要的 —— 换了模型还想复用旧结论才是不合理的。
    """
    canonical = "|".join([group_id, occurrence_id, evidence_fingerprint, prompt_version, provider, model])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def _next_revision(db: Session, group_id: str) -> int:
    rows = db.execute(
        select(FailureAnalysis.revision_no).where(FailureAnalysis.failure_group_id == group_id)
    ).scalars().all()
    return (max(rows) + 1) if rows else 1


def _find_reusable(db: Session, *, key: str) -> FailureAnalysis | None:
    """找可以直接复用的分析。

    只有这两种情况算「可复用」：
    - COMPLETED：已经得到结论，重复请求直接返回它；
    - 5 分钟内仍 RUNNING：还在跑，避免同一个分组被并发调两次 AI。
    FAILED 不算 —— 失败就是用来重试的。
    """
    rows = db.execute(
        select(FailureAnalysis)
        .where(FailureAnalysis.analysis_key == key)
        .order_by(FailureAnalysis.revision_no.desc())
    ).scalars().all()
    for row in rows:
        if row.status == "COMPLETED":
            return row
        if row.status == "RUNNING" and is_within(row.started_at, IN_FLIGHT_SECONDS):
            return row
    return None


def analyze_group(
    db: Session,
    group_id: str,
    *,
    force: bool = False,
    occurrence_id: str | None = None,
    provider_name: str | None = None,
) -> dict[str, Any]:
    """对某个失败分组做一次 AI 分析。**不抛异常。**

    返回的 dict 里 `created=False` 表示命中已有分析（幂等），
    `skipped=True` 表示按规则不该分析或 AI 没配。
    """
    group = db.get(FailureGroup, group_id)
    if group is None:
        return {"group_id": group_id, "skipped": True, "reason": "失败分组不存在", "created": False}

    eligible, reason = analysis_eligibility(group, force=force)
    if not eligible:
        return {"group_id": group_id, "skipped": True, "reason": reason, "created": False}

    try:
        provider = get_provider(provider_name)
    except AIProviderError as exc:
        return {"group_id": group_id, "skipped": True, "reason": exc.message, "created": False}

    if not provider.available():
        return {"group_id": group_id, "skipped": True, "reason": "AI 未配置或不可用", "created": False}

    occurrence = db.get(FailureOccurrence, occurrence_id) if occurrence_id else None
    context = evidence_module.build_context(
        db, group, occurrence=occurrence, include_images=provider.supports_vision
    )
    if not context.occurrence_id:
        return {"group_id": group_id, "skipped": True, "reason": "该分组还没有失败现场记录", "created": False}

    key = compute_analysis_key(
        group_id=group.id,
        occurrence_id=context.occurrence_id,
        evidence_fingerprint=context.evidence_fingerprint,
        prompt_version=prompts.PROMPT_VERSION,
        provider=provider.name,
        model=provider.model,
    )
    if not force:
        reusable = _find_reusable(db, key=key)
        if reusable is not None:
            return {
                "group_id": group_id,
                "analysis_id": reusable.id,
                "status": reusable.status,
                "created": False,
                "skipped": False,
                "reason": "命中已有分析（证据与提示词版本未变）",
                "revision_no": reusable.revision_no,
            }

    row = FailureAnalysis(
        project_id=group.project_id,
        failure_group_id=group.id,
        failure_occurrence_id=context.occurrence_id,
        run_id=context.run_id or None,
        provider=provider.name,
        model=provider.model,
        status="RUNNING",
        analysis_key=key,
        revision_no=_next_revision(db, group.id),
        prompt_version=prompts.PROMPT_VERSION,
        evidence_fingerprint=context.evidence_fingerprint,
        started_at=utcnow(),
        review_status="UNREVIEWED",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # `_execute_analysis` 自己已经包了 Provider 异常，这里再包一层是防御性的：
    # 它一旦抛出来就会顺着「Run 结束后的钩子」往上冒，那是绝对不允许的。
    # AI 的任何问题都只能体现为这一行记录的状态，不能影响一次回归。
    try:
        _execute_analysis(db, row, context, provider)
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI 分析出现未预期异常 group=%s", group_id)
        db.rollback()
        try:
            _mark_failed(db, row, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 - 连标记失败都失败，就只剩日志了
            db.rollback()
            logger.exception("标记分析失败状态时又出错 analysis=%s", row.id)

    return {
        "group_id": group_id,
        "analysis_id": row.id,
        "status": row.status,
        "created": True,
        "skipped": False,
        "reason": row.error_message if row.status == "FAILED" else "",
        "revision_no": row.revision_no,
        "error_message": row.error_message,
    }


def _execute_analysis(db: Session, row: FailureAnalysis, context: FailureAnalysisContext, provider: Any) -> None:
    """真正调用 + 校验 + 落库。所有异常就地转成 FAILED。

    耗时用 `time.perf_counter()` 而不是「finished_at - started_at」：
    row 在上一行已经 commit + refresh 过一次，SQLite 读回来的 started_at 是
    naive 的，直接相减会抛 TypeError（详见 utils/timeutil.py 的说明）。
    """
    started = time.perf_counter()
    try:
        response = provider.analyze_failure(context)
    except AIProviderError as exc:
        _mark_failed(db, row, exc.message, duration_ms=elapsed_ms(started))
        return
    except Exception as exc:  # noqa: BLE001 - Provider 是外部的，什么异常都可能
        logger.warning("AI 调用出现未预期异常：%s", type(exc).__name__)
        _mark_failed(db, row, f"{type(exc).__name__}: {exc}", duration_ms=elapsed_ms(started))
        return

    parsed = response.parsed
    raw_text = response.raw_text
    repair_used = False

    if not response.ok:
        _mark_failed(db, row, response.error or "AI 调用失败", duration_ms=elapsed_ms(started))
        return

    # 第一次没解析出 JSON：按方案 §7 做一次「修复提示」重试，不做无限追问
    if parsed is None:
        repair_used = True
        try:
            repaired = provider.analyze_failure(
                context, repair_hint=prompts.build_repair_prompt(raw_text, "输出不是合法 JSON 对象")
            )
        except Exception as exc:  # noqa: BLE001
            _mark_failed(db, row, f"修复重试失败：{type(exc).__name__}", duration_ms=elapsed_ms(started))
            return
        if repaired.ok and repaired.parsed is not None:
            parsed = repaired.parsed
            raw_text = repaired.raw_text
        else:
            _mark_failed(
                db,
                row,
                "AI 连续两次未返回合法 JSON，已放弃本次分析" + ("（含一次修复重试）" if repair_used else ""),
                raw_text=raw_text,
                duration_ms=elapsed_ms(started),
            )
            return

    try:
        result = FailureAnalysisResult.model_validate(parsed)
    except ValidationError as exc:
        _mark_failed(
            db, row, f"AI 返回结构不符合约定：{exc.errors()[:2]}", raw_text=raw_text, duration_ms=elapsed_ms(started)
        )
        return

    if not result.is_meaningful():
        _mark_failed(
            db, row, "AI 返回内容为空，视为分析失败", raw_text=raw_text, duration_ms=elapsed_ms(started)
        )
        return

    row.summary = result.summary.strip()[:2000]
    row.root_cause = result.root_cause.strip()[:4000]
    row.evidence = result.evidence[:12]
    row.related_commit = result.related_commit.strip()[:64]
    row.suggested_owner = result.suggested_owner.strip()[:128]
    row.confidence = result.confidence
    row.uncertainty = result.uncertainty.strip()[:2000]
    row.recommendation = result.recommendation.strip()[:2000]
    # 注意：AI 的建议分类只进这一列，绝不回写 FailureGroup.category
    row.ai_suggested_category = result.ai_suggested_category
    row.raw_response_redacted = redact_text(raw_text, _secret_list())[:RAW_RESPONSE_LIMIT]
    row.status = "COMPLETED"
    row.error_message = ""
    row.finished_at = utcnow()
    row.duration_ms = elapsed_ms(started)
    db.commit()


def _secret_list() -> list[str]:
    return [item for item in (settings.ai_api_key, settings.wecom_webhook_secret) if item]


def _mark_failed(
    db: Session, row: FailureAnalysis, message: str, *, raw_text: str = "", duration_ms: int | None = None
) -> None:
    row.status = "FAILED"
    row.error_message = redact_text(str(message), _secret_list())[:2000]
    if raw_text:
        row.raw_response_redacted = redact_text(raw_text, _secret_list())[:RAW_RESPONSE_LIMIT]
    row.finished_at = utcnow()
    if duration_ms is not None:
        row.duration_ms = duration_ms
    db.commit()
    logger.info("AI 分析失败 group=%s：%s", row.failure_group_id, row.error_message)


def review_analysis(
    db: Session,
    analysis_id: str,
    *,
    review_status: str,
    reviewed_by: str = "",
    review_comment: str = "",
) -> FailureAnalysis:
    """人工确认 / 修改 / 忽略。

    **不改任何测试结果，也不关闭 FailureGroup。**
    「我觉得这个结论对」和「这个问题已经解决」是两件完全不同的事，
    后者要经过 Reverify 与人工 RESOLVED。
    """
    status = str(review_status or "").strip().upper()
    if status not in ANALYSIS_REVIEW_STATUSES:
        raise ValueError(f"review_status 必须是 {ANALYSIS_REVIEW_STATUSES} 之一")
    row = db.get(FailureAnalysis, analysis_id)
    if row is None:
        raise LookupError("分析记录不存在")
    row.review_status = status
    row.reviewed_by = str(reviewed_by or "")[:64]
    row.reviewed_at = utcnow()
    row.review_comment = redact_text(str(review_comment or ""), _secret_list())[:2000]
    db.commit()
    db.refresh(row)
    return row


def serialize_analysis(row: FailureAnalysis) -> dict[str, Any]:
    """给 API / 前端用的稳定结构。"""
    return {
        "id": row.id,
        "failure_group_id": row.failure_group_id,
        "failure_occurrence_id": row.failure_occurrence_id or "",
        "run_id": row.run_id or "",
        "provider": row.provider or "",
        "model": row.model or "",
        "status": row.status,
        "revision_no": int(row.revision_no or 0),
        "prompt_version": row.prompt_version or "",
        "evidence_fingerprint": row.evidence_fingerprint or "",
        "summary": row.summary or "",
        "root_cause": row.root_cause or "",
        "evidence": list(row.evidence or []),
        "related_commit": row.related_commit or "",
        "suggested_owner": row.suggested_owner or "",
        "confidence": row.confidence,
        "uncertainty": row.uncertainty or "",
        "recommendation": row.recommendation or "",
        "ai_suggested_category": row.ai_suggested_category or "",
        "error_message": row.error_message or "",
        "duration_ms": int(row.duration_ms or 0),
        "review_status": row.review_status or "UNREVIEWED",
        "reviewed_by": row.reviewed_by or "",
        "reviewed_at": row.reviewed_at.isoformat(timespec="seconds") if row.reviewed_at else None,
        "review_comment": row.review_comment or "",
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
    }


def list_analyses(db: Session, group_id: str, *, limit: int = 20) -> list[FailureAnalysis]:
    return list(
        db.execute(
            select(FailureAnalysis)
            .where(FailureAnalysis.failure_group_id == group_id)
            .order_by(FailureAnalysis.revision_no.desc())
            .limit(limit)
        ).scalars().all()
    )


__all__ = [
    "IN_FLIGHT_SECONDS",
    "analysis_eligibility",
    "analyze_group",
    "compute_analysis_key",
    "list_analyses",
    "review_analysis",
    "serialize_analysis",
]
