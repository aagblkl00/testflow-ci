"""M6-B：Artifact Retention 安全治理服务。

第一版只做人工 Preview / Apply：

- 候选只来自**已结束**的 Run，且按 `ExecutionRun.finished_at` 判断过期；
- 绝对不使用 `Artifact.created_at`；
- 删除前检查路径引用计数，禁止因为一个 Artifact 过期就删物理文件；
- 对活动 Run、Reverify、活动 Alert、OPEN FailureGroup 最新 Run 做保护；
- Apply 时重新计算候选与保护关系，不信任旧 Preview。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ALERT_ACTIVE_STATUSES, Alert, Artifact, ExecutionRun, FailureGroup, RetentionJob
from ..utils.timeutil import ensure_utc

TERMINAL_RUN_STATUSES = {"PASSED", "FAILED", "PARTIAL", "CANCELLED", "ERROR"}
ACTIVE_RUN_STATUSES = {"QUEUED", "RUNNING", "CANCELLING"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_days(retention_days: int | None) -> int:
    resolved = settings.artifact_retention_days if retention_days is None else int(retention_days)
    if resolved <= 0:
        raise ValueError("retention_days 必须大于 0")
    return resolved


def _cutoff(retention_days: int, *, now: datetime | None = None) -> datetime:
    return (now or _now()) - timedelta(days=retention_days)


def _protected_run_ids(db: Session) -> set[str]:
    """返回当前绝对不允许清理的 Run ID。

    规则只基于现有模型关系：
    1. QUEUED / RUNNING / CANCELLING Run；
    2. ACTIVE Alert 的 run_id / latest_run_id / reverify_run_id；
    3. OPEN FailureGroup 的 latest_run_id。
    """
    protected: set[str] = set()

    alert_rows = db.execute(
        select(Alert.run_id, Alert.latest_run_id, Alert.reverify_run_id).where(
            Alert.status.in_(ALERT_ACTIVE_STATUSES)
        )
    ).all()
    for run_id, latest_run_id, reverify_run_id in alert_rows:
        protected.update(value for value in (run_id, latest_run_id, reverify_run_id) if value)

    protected.update(
        db.execute(
            select(FailureGroup.latest_run_id).where(
                FailureGroup.status == "OPEN", FailureGroup.latest_run_id != ""
            )
        )
        .scalars()
        .all()
    )

    protected.update(
        db.execute(select(ExecutionRun.id).where(ExecutionRun.status.in_(ACTIVE_RUN_STATUSES))).scalars().all()
    )
    return protected


def _base_candidates(db: Session, cutoff: datetime) -> list[tuple[Artifact, ExecutionRun]]:
    """返回「Run 已结束且 finished_at < cutoff」的全部 Artifact 行。

    为了 SQLite / PostgreSQL 的 datetime 兼容，不把 cutoff 比较下推到 SQL；
    先按终态与 finished_at 非空取回，再用 `ensure_utc` 在 Python 比较。
    """
    rows = db.execute(
        select(Artifact, ExecutionRun)
        .join(ExecutionRun, ExecutionRun.id == Artifact.run_id)
        .where(ExecutionRun.status.in_(TERMINAL_RUN_STATUSES), ExecutionRun.finished_at.is_not(None))
        .order_by(Artifact.id)
    ).all()

    candidates: list[tuple[Artifact, ExecutionRun]] = []
    for artifact, run in rows:
        finished_at = ensure_utc(run.finished_at)
        if finished_at is not None and finished_at < cutoff:
            candidates.append((artifact, run))
    return candidates


def _path_reference_count(db: Session, path: str, *, exclude_artifact_id: str | None = None) -> int:
    query = select(func.count(Artifact.id)).where(Artifact.path == path)
    if exclude_artifact_id:
        query = query.where(Artifact.id != exclude_artifact_id)
    return int(db.execute(query).scalar_one() or 0)


def _validated_path(raw_path: str) -> Path | None:
    """解析并限制路径。返回 None 表示非法或越界。"""
    if not raw_path:
        return None
    try:
        path = Path(raw_path).resolve()
        path.relative_to(settings.storage_dir.resolve())
        return path
    except (ValueError, OSError):
        return None


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return ensure_utc(value).isoformat()


def _remove_file(path: Path) -> tuple[str, str]:
    """尝试删除单个物理文件，返回 `(status, error)`。

    status 只可能是 `deleted` / `missing` / `failed`。
    调用方必须根据 status 决定是否继续删 DB 记录。
    """
    try:
        path.unlink()
        return "deleted", ""
    except FileNotFoundError:
        return "missing", "file_not_found"
    except PermissionError as exc:
        return "failed", f"permission_denied:{exc}"
    except OSError as exc:
        return "failed", f"io_error:{type(exc).__name__}:{exc}"


def _delete_artifact_record(db: Session, artifact: Artifact) -> None:
    """删除 Artifact DB 记录并立即提交。失败时向上抛，由调用方 rollback。"""
    db.delete(artifact)
    db.commit()


def _protected_counts(db: Session, protected_run_ids: set[str]) -> tuple[int, int]:
    if not protected_run_ids:
        return 0, 0
    rows = db.execute(select(Artifact.run_id).where(Artifact.run_id.in_(protected_run_ids))).scalars().all()
    return len(rows), len(set(rows))


def build_preview(db: Session, retention_days: int | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    days = _resolve_days(retention_days)
    cutoff = _cutoff(days, now=now)
    base = _base_candidates(db, cutoff)
    protected_run_ids = _protected_run_ids(db)
    candidates = [(artifact, run) for artifact, run in base if artifact.run_id not in protected_run_ids]
    protected_artifact_count, protected_run_count = _protected_counts(db, protected_run_ids)

    path_groups: dict[str, list[Artifact]] = {}
    for artifact, _run in candidates:
        path_groups.setdefault(artifact.path, []).append(artifact)

    candidate_path_count = len(path_groups)
    duplicate_path_count = 0
    estimated_bytes = 0
    for path, artifacts in path_groups.items():
        total_references = _path_reference_count(db, path)
        if total_references > 1:
            duplicate_path_count += 1
        validated = _validated_path(path)
        if total_references == len(artifacts) and validated is not None and validated.is_file():
            estimated_bytes += int(artifacts[0].size or 0)

    run_by_id = {run.id: run for _artifact, run in candidates}
    sample_artifacts = [
        {
            "id": artifact.id,
            "run_id": artifact.run_id,
            "type": artifact.type,
            "path": artifact.path,
            "size": int(artifact.size or 0),
        }
        for artifact, _run in candidates[:5]
    ]
    sample_runs = [
        {
            "id": run.id,
            "project_id": run.project_id,
            "status": run.status,
            "finished_at": _iso(run.finished_at),
        }
        for run in list(run_by_id.values())[:5]
    ]

    return {
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "candidate_artifact_count": len(candidates),
        "candidate_path_count": candidate_path_count,
        "estimated_bytes": estimated_bytes,
        "protected_artifact_count": protected_artifact_count,
        "protected_run_count": protected_run_count,
        "duplicate_path_count": duplicate_path_count,
        "sample_artifacts": sample_artifacts,
        "sample_runs": sample_runs,
    }


def apply_retention(
    db: Session,
    retention_days: int | None = None,
    *,
    actor: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    days = _resolve_days(retention_days)
    cutoff = _cutoff(days, now=now)
    preview = build_preview(db, days, now=now)
    base = _base_candidates(db, cutoff)

    deleted_artifact_count = 0
    deleted_file_count = 0
    missing_file_count = 0
    failed_count = 0
    freed_bytes = 0
    skipped_count = 0
    errors: list[dict[str, str]] = []

    for artifact, _run in base:
        # Apply 时逐条重新检查，不能信任 Preview 那一刻的候选。
        protected_run_ids = _protected_run_ids(db)
        current_run = db.get(ExecutionRun, artifact.run_id)
        if current_run is None:
            failed_count += 1
            errors.append({"artifact_id": artifact.id, "error": "run_missing"})
            continue

        finished_at = ensure_utc(current_run.finished_at)
        if (
            current_run.status not in TERMINAL_RUN_STATUSES
            or finished_at is None
            or finished_at >= cutoff
            or current_run.id in protected_run_ids
        ):
            skipped_count += 1
            continue

        path = _validated_path(artifact.path)
        if path is None:
            failed_count += 1
            errors.append({"artifact_id": artifact.id, "error": "invalid_path"})
            continue

        remaining_references = _path_reference_count(db, artifact.path, exclude_artifact_id=artifact.id)
        if remaining_references > 0:
            try:
                _delete_artifact_record(db, artifact)
                deleted_artifact_count += 1
            except SQLAlchemyError as exc:
                db.rollback()
                failed_count += 1
                errors.append({"artifact_id": artifact.id, "error": f"db_delete_failed:{type(exc).__name__}"})
            continue

        if not path.exists():
            # 文件已经不存在：可视为物理文件已满足清理结果，但仍记录 missing 状态。
            try:
                _delete_artifact_record(db, artifact)
                deleted_artifact_count += 1
                missing_file_count += 1
            except SQLAlchemyError as exc:
                db.rollback()
                failed_count += 1
                errors.append({"artifact_id": artifact.id, "error": f"db_delete_failed:{type(exc).__name__}"})
            continue

        if not path.is_file():
            failed_count += 1
            errors.append({"artifact_id": artifact.id, "error": "not_a_file"})
            continue

        remove_status, remove_error = _remove_file(path)
        if remove_status == "failed":
            failed_count += 1
            errors.append({"artifact_id": artifact.id, "error": remove_error})
            continue

        try:
            _delete_artifact_record(db, artifact)
            deleted_artifact_count += 1
            if remove_status == "deleted":
                deleted_file_count += 1
                freed_bytes += int(artifact.size or 0)
            else:
                missing_file_count += 1
        except SQLAlchemyError as exc:
            db.rollback()
            failed_count += 1
            errors.append({"artifact_id": artifact.id, "error": f"db_delete_failed:{type(exc).__name__}"})

    status = "COMPLETED" if failed_count == 0 else ("FAILED" if deleted_artifact_count == 0 and base else "PARTIAL")
    job = RetentionJob(
        executed_by=actor[:64],
        executed_at=_now(),
        retention_days=days,
        cutoff=cutoff,
        preview_candidate_artifact_count=preview["candidate_artifact_count"],
        preview_candidate_path_count=preview["candidate_path_count"],
        preview_estimated_bytes=preview["estimated_bytes"],
        preview_protected_artifact_count=preview["protected_artifact_count"],
        preview_protected_run_count=preview["protected_run_count"],
        preview_duplicate_path_count=preview["duplicate_path_count"],
        deleted_artifact_count=deleted_artifact_count,
        deleted_file_count=deleted_file_count,
        missing_file_count=missing_file_count,
        failed_count=failed_count,
        freed_bytes=freed_bytes,
        status=status,
        error_summary=errors[:100],
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    return {
        "job_id": job.id,
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "status": status,
        "preview_candidate_artifact_count": job.preview_candidate_artifact_count,
        "preview_candidate_path_count": job.preview_candidate_path_count,
        "preview_estimated_bytes": job.preview_estimated_bytes,
        "protected_artifact_count": job.preview_protected_artifact_count,
        "protected_run_count": job.preview_protected_run_count,
        "duplicate_path_count": job.preview_duplicate_path_count,
        "deleted_artifact_count": job.deleted_artifact_count,
        "deleted_file_count": job.deleted_file_count,
        "missing_file_count": job.missing_file_count,
        "failed_count": job.failed_count,
        "skipped_count": skipped_count,
        "freed_bytes": job.freed_bytes,
        "errors": errors[:100],
    }


__all__ = ["apply_retention", "build_preview", "ACTIVE_RUN_STATUSES", "TERMINAL_RUN_STATUSES"]
