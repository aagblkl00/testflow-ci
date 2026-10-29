"""M6-B：Artifact Retention 管理接口。

Preview 只读；Apply 必须 ADMIN。两者都接受可选的 `retention_days`，
不传时使用全局 `ARTIFACT_RETENTION_DAYS`。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth.deps import require_admin
from ..db import get_db
from ..models import User
from ..schemas import RetentionRequest
from ..services.retention import apply_retention, build_preview

router = APIRouter(prefix="/api/admin/retention", tags=["admin"])


@router.post("/preview")
def retention_preview(
    payload: RetentionRequest | None = Body(default=None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return build_preview(db, payload.retention_days if payload else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/apply")
def retention_apply(
    payload: RetentionRequest | None = Body(default=None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return apply_retention(db, payload.retention_days if payload else None, actor=admin.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


__all__ = ["router"]
