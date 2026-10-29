"""M5 AI 分析接口。

```
POST   /api/failures/{id}/analyze     触发一次 AI 分析（幂等，可 force 出 revision）
GET    /api/failures/{id}/analyses    某个失败分组的全部分析（按 revision 倒序）
GET    /api/analyses/{id}             单条分析
PATCH  /api/analyses/{id}/review      人工确认 / 修改 / 忽略
GET    /api/ai/meta                   AI 与通知的可用性（前端据此决定按钮状态）
```

权限：全部需要登录（JWT）。AI 会消耗token、Review 是人工结论，都不该让
CI Token 顺带拥有 —— M4 的 CI Token 只在 `/api/ci/*` 生效，这里不认它。

注意这几个接口**都不改测试结果**：`analyze` 只写 `failure_analyses`，
`review` 只改这条分析记录的 review 字段。FailureGroup.category 一动不动。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..ai import service as ai_service
from ..ai.prompts import PROMPT_VERSION
from ..ai.provider import available_providers as ai_providers
from ..auth.deps import get_current_user, require_writer
from ..config import settings
from ..db import get_db
from ..models import FailureAnalysis, FailureGroup, User
from ..notification.provider import available_providers as notification_providers
from ..notification.provider import default_provider_name
from ..schemas import AiMetaOut, AnalysisReviewRequest, AnalyzeRequest

logger = logging.getLogger("testflow.api.analyses")

router = APIRouter(prefix="/api", tags=["m5-ai"])


@router.get("/ai/meta", response_model=AiMetaOut)
def ai_meta(_: User = Depends(get_current_user)) -> AiMetaOut:
    """AI 与通知当前能不能用。前端拿它决定「开始分析」按钮亮不亮。"""
    note = ""
    if not settings.ai_enabled:
        note = "AI 未配置（需要 AI_API_KEY 与 AI_MODEL），分析入口会直接跳过"
    elif not settings.wecom_enabled:
        note = "AI 已配置；企业微信未配置或通知总开关关闭，告警不会外发"
    return AiMetaOut(
        ai_enabled=settings.ai_enabled,
        provider=settings.ai_provider,
        model=settings.ai_model,
        prompt_version=PROMPT_VERSION,
        available_providers=ai_providers(),
        auto_analyze=settings.ai_auto_analyze,
        required_rules=list(settings.ai_required_rules),
        notification_enabled=settings.wecom_enabled,
        notification_provider=default_provider_name(),
        notification_max_attempts=settings.notification_max_attempts,
        note=note,
    )


@router.post("/failures/{group_id}/analyze")
def analyze_failure_group(
    group_id: str,
    payload: AnalyzeRequest | None = None,
    _: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """触发 AI 分析。

    幂等由 `analysis_key`（分组 + 代表出现 + 证据指纹 + 提示词版本 + provider + model）
    保证：同样的输入重复点，返回已有分析而不是再烧一次 token。
    要「重新分析」就带 `force=true`，它会**新建一个 revision，不覆盖旧结论**。
    """
    group = db.get(FailureGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="失败分组不存在")

    request = payload or AnalyzeRequest()
    outcome = ai_service.analyze_group(
        db,
        group_id,
        force=bool(request.force),
        occurrence_id=request.occurrence_id,
        provider_name=request.provider,
    )
    if outcome.get("skipped") and not outcome.get("analysis_id"):
        # 跳过不是错误：可能是 AI 没配，也可能是确定性分类已经够用
        return {
            "group_id": group_id,
            "skipped": True,
            "reason": outcome.get("reason", ""),
            "analysis": None,
        }
    analysis = db.get(FailureAnalysis, outcome["analysis_id"]) if outcome.get("analysis_id") else None
    return {
        "group_id": group_id,
        "skipped": False,
        "created": bool(outcome.get("created")),
        "reason": outcome.get("reason", ""),
        "analysis": ai_service.serialize_analysis(analysis) if analysis is not None else None,
    }


@router.get("/failures/{group_id}/analyses")
def list_group_analyses(
    group_id: str,
    limit: int = 20,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """某个失败分组的全部分析，按 revision 倒序。最新的在最前面。"""
    if db.get(FailureGroup, group_id) is None:
        raise HTTPException(status_code=404, detail="失败分组不存在")
    rows = ai_service.list_analyses(db, group_id, limit=max(1, min(limit, 100)))
    eligible, reason = ai_service.analysis_eligibility(db.get(FailureGroup, group_id))
    return {
        "group_id": group_id,
        "total": len(rows),
        "eligible": eligible,
        "eligibility_reason": reason,
        "items": [ai_service.serialize_analysis(row) for row in rows],
    }


@router.get("/analyses/{analysis_id}")
def get_analysis(
    analysis_id: str,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(FailureAnalysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="分析记录不存在")
    return ai_service.serialize_analysis(row)


@router.patch("/analyses/{analysis_id}/review")
def review_analysis(
    analysis_id: str,
    payload: AnalysisReviewRequest,
    user: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """人工确认 / 修改 / 忽略。

    **不会**关闭 FailureGroup，也**不会**改测试结果。
    「我认同这个分析」与「这个问题已解决」是两件事，后者要走告警的
    Reverify → RESOLVED 流程。
    """
    try:
        row = ai_service.review_analysis(
            db,
            analysis_id,
            review_status=payload.review_status,
            reviewed_by=user.username,
            review_comment=payload.review_comment,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="分析记录不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return ai_service.serialize_analysis(row)


__all__ = ["router"]
