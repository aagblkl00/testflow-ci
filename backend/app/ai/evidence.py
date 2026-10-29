"""证据构建器：把数据库里的失败现场变成 AI 能消费的证据包。

三条硬约束：

**1. 先裁剪，再送出去。** 一次失败可能有十几 MB 的 Trace、几十张截图、
整页 DOM。全塞进 Prompt 既贵又没用（模型看不见二进制），还会把关键信息淹没。
所以证据按方案 §5 的优先级排列，逐段限额，超出直接截断 ——
**宁可少给，不可乱给**。

**2. 全部先脱敏。** 复用 `utils/redact.py`。失败的现场文本（错误信息、
控制台、网络日志、DOM、请求 URL）是最容易夹带 token / cookie 的地方，
而 Prompt 会离开本机送到模型服务商那里 —— 这是整个系统里唯一会把数据
带出内网的地方，脱敏在这里是必须而不是加分项。

**3. 默认不读二进制。** Trace / HAR / Video 只登记**引用**（名字、大小），
不读内容：模型看不了 zip 和 mp4。截图只在 Provider 声明支持视觉时才读，
且限张数、限体积。

`evidence_fingerprint` 是把「送出去的内容」哈希一遍得到的，用于分析幂等 ——
同一份证据重复分析应该命中缓存，证据变了才应该重算。
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artifact, CaseResult, CaseResultAttempt, Environment, ExecutionRun, FailureGroup, FailureOccurrence, Project, RegressionPlan, StepResult
from ..utils.redact import redact_text
from .schemas import EvidenceArtifact, EvidenceImage, FailureAnalysisContext

# 证据优先级（方案 §5）。数字越小越先保留，截断时从后往前丢。
EVIDENCE_PRIORITY: tuple[str, ...] = (
    "step",
    "http_status",
    "request_url",
    "console",
    "network",
    "dom",
    "screenshot",
    "trace",
    "har",
    "video",
    "log",
)

# 按类型排的 Artifact 展示顺序，与上面的优先级对应
_ARTIFACT_ORDER: dict[str, int] = {
    "screenshot": 6,
    "trace": 7,
    "har": 8,
    "video": 9,
    "console": 3,
    "network": 4,
    "dom": 5,
    "log": 10,
    "report": 11,
}

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


@dataclass(slots=True)
class EvidenceLimits:
    """限额。全部可以按需调小，调大要谨慎 —— 每多一万字符就是真金白银。"""

    max_total_chars: int = 12000
    step_error: int = 2000
    step_expected: int = 800
    step_actual: int = 800
    normalized_message: int = 1200
    console: int = 1500
    network: int = 1500
    dom: int = 2000
    log: int = 1500
    js_error: int = 1000
    max_artifacts: int = 12
    max_images: int = 1
    max_image_bytes: int = 2_000_000


def _clip(text: str | None, limit: int, secrets: list[str]) -> str:
    """脱敏 + 压缩空白 + 截断。所有文本进 Prompt 前都必须过这里。"""
    if not text:
        return ""
    cleaned = redact_text(str(text), secrets)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    cleaned = _BLANK_LINES.sub("\n", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + f"\n…（已截断，原长 {len(cleaned)} 字符）"


def _clip_dom(html: str | None, limit: int, secrets: list[str]) -> str:
    """DOM 只保留结构骨架。

    整页 HTML 里 90% 是内联样式和脚本，对判断「元素为什么找不到」毫无帮助，
    反而会把真正相关的几行挤掉。所以剥掉标签属性里的样式噪声、去掉脚本内容，
    再做截断。
    """
    if not html:
        return ""
    text = redact_text(str(html), secrets)
    text = re.sub(r"(?is)<script.*?</script>", "<script/>", text)
    text = re.sub(r"(?is)<style.*?</style>", "<style/>", text)
    text = re.sub(r"\sstyle=\"[^\"]*\"", "", text)
    text = _BLANK_LINES.sub("\n", text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（DOM 已截断，原长 {len(text)} 字符）"


def _read_text_file(path: str, limit_bytes: int, limit_chars: int, secrets: list[str]) -> str:
    """有界读文件。绝不 `read_text()` 整个文件 —— 一个 200MB 的 DOM 足以把进程拖死。"""
    if not path:
        return ""
    target = Path(path)
    try:
        if not target.is_file():
            return ""
        with target.open("rb") as handle:
            raw = handle.read(limit_bytes)
    except OSError:
        return ""
    return _clip(raw.decode("utf-8", errors="replace"), limit_chars, secrets)


def _read_image(path: str, limit_bytes: int) -> EvidenceImage | None:
    if not path:
        return None
    target = Path(path)
    try:
        if not target.is_file():
            return None
        size = target.stat().st_size
        if size <= 0 or size > limit_bytes:
            return None
        data = base64.b64encode(target.read_bytes()).decode("ascii")
    except OSError:
        return None
    suffix = target.suffix.lower().lstrip(".") or "png"
    media = "image/jpeg" if suffix in {"jpg", "jpeg"} else "image/png"
    return EvidenceImage(media_type=media, data_base64=data, name=target.name, bytes_size=size)


def pick_representative_occurrence(db: Session, group: FailureGroup) -> FailureOccurrence | None:
    """代表出现 = 最近一次。分析针对具体现场，不针对抽象指纹。"""
    return db.execute(
        select(FailureOccurrence)
        .where(FailureOccurrence.failure_group_id == group.id)
        .order_by(FailureOccurrence.created_at.desc(), FailureOccurrence.id.desc())
    ).scalars().first()


def _pick_step(db: Session, occurrence: FailureOccurrence) -> StepResult | None:
    """挑出真正失败的那一步。

    优先用 occurrence 直接指向的 step_result（M3 聚合时已经挑好了）；
    拿不到就回退到该 attempt 里第一个 FAILED/ERROR 步骤。
    """
    if occurrence.step_result_id:
        step = db.get(StepResult, occurrence.step_result_id)
        if step is not None:
            return step
    if not occurrence.attempt_id:
        return None
    steps = db.execute(
        select(StepResult).where(StepResult.attempt_id == occurrence.attempt_id).order_by(StepResult.step_no)
    ).scalars().all()
    return next((item for item in steps if item.status in {"FAILED", "ERROR"}), steps[0] if steps else None)


def collect_artifacts(db: Session, *, step: StepResult | None, attempt_id: str | None, limit: int) -> list[Artifact]:
    """取证据清单并按优先级排序。只登记引用，不读内容。"""
    conditions = []
    if step is not None:
        conditions.append(Artifact.step_result_id == step.id)
    if attempt_id:
        conditions.append(Artifact.attempt_id == attempt_id)
    if not conditions:
        return []
    query = select(Artifact).where(conditions[0] if len(conditions) == 1 else conditions[0] | conditions[1])
    rows = db.execute(query).scalars().all()
    rows.sort(key=lambda item: (_ARTIFACT_ORDER.get(item.type, 99), item.created_at or item.created_at))
    return rows[:limit]


def build_context(
    db: Session,
    group: FailureGroup,
    *,
    occurrence: FailureOccurrence | None = None,
    include_images: bool = False,
    limits: EvidenceLimits | None = None,
) -> FailureAnalysisContext:
    """构造证据包。**纯读操作**，不写任何表。"""
    limits = limits or EvidenceLimits()
    occurrence = occurrence or pick_representative_occurrence(db, group)
    if occurrence is None:
        # 一个连出现记录都没有的分组不该进分析；返回空上下文让上层判空
        return FailureAnalysisContext(failure_group_id=group.id, category=group.category)

    run = db.get(ExecutionRun, occurrence.run_id)
    case = db.get(CaseResult, occurrence.case_result_id)
    attempt = db.get(CaseResultAttempt, occurrence.attempt_id) if occurrence.attempt_id else None
    step = _pick_step(db, occurrence)
    project = db.get(Project, group.project_id)
    environment = db.get(Environment, run.environment_id) if run and run.environment_id else None
    plan = db.get(RegressionPlan, run.plan_id) if run and run.plan_id else None

    # 密钥来自环境变量，用于把「原值」也掩掉（不只是按模式匹配）
    secrets: list[str] = []
    if environment is not None:
        from ..utils.redact import collect_secrets  # noqa: PLC0415 - 避免顶层循环

        secrets = collect_secrets(environment.variables or {})

    artifacts = collect_artifacts(db, step=step, attempt_id=occurrence.attempt_id, limit=limits.max_artifacts)
    dom_path = (step.dom_path if step else "") or ""
    log_path = (step.log_path if step else "") or ""
    # 指纹表里的归一化结论是给 AI 的重要参考（策略 + 错误类型 + 归一化文本），
    # 不带上就等于让模型自己去猜「这个错误属于哪一类现象」
    fingerprint_row = group.fingerprint_ref

    images: list[EvidenceImage] = []
    if include_images:
        screenshots = [item for item in artifacts if item.type == "screenshot"]
        for item in screenshots[: limits.max_images]:
            image = _read_image(item.path, limits.max_image_bytes)
            if image is not None:
                images.append(image)

    context = FailureAnalysisContext(
        project_id=group.project_id,
        project_name=project.name if project else "",
        plan_name=plan.name if plan else "",
        environment_name=environment.name if environment else "",
        environment_type=(environment.environment_type if environment else "") or "",
        run_id=occurrence.run_id,
        run_status=run.status if run else "",
        commit_sha=(run.commit_sha if run else "") or "",
        branch=(run.branch if run else "") or "",
        pipeline_id=(run.pipeline_id if run else "") or "",
        pipeline_url=redact_text((run.pipeline_url if run else "") or "", secrets),
        failure_group_id=group.id,
        group_title=group.title,
        category=group.category,
        rule_id=group.rule_id or "",
        rule_reason=group.rule_reason or "",
        occurrence_count=int(group.occurrence_count or 0),
        failed_case_count=int(group.failed_case_count or 0),
        failed_run_count=int(group.failed_run_count or 0),
        flaky_count=int(group.flaky_count or 0),
        fingerprint=(fingerprint_row.fingerprint if fingerprint_row else "") or "",
        strategy=(fingerprint_row.strategy if fingerprint_row else "") or "",
        failure_type=(fingerprint_row.failure_type if fingerprint_row else "") or "",
        normalized_message=_clip(fingerprint_row.normalized_message if fingerprint_row else "", limits.normalized_message, secrets),
        normalized_expected=_clip(fingerprint_row.normalized_expected if fingerprint_row else "", limits.step_expected, secrets),
        normalized_actual=_clip(fingerprint_row.normalized_actual if fingerprint_row else "", limits.step_actual, secrets),
        occurrence_id=occurrence.id,
        case_code=(case.case_code if case else "") or "",
        case_name=(case.case_name if case else "") or "",
        case_status=(case.status if case else occurrence.case_status) or "",
        attempt_no=int(attempt.attempt_no or 0) if attempt else 0,
        step_no=int(step.step_no or 0) if step else 0,
        step_action=(step.action if step else "") or "",
        step_error=_clip(step.error if step else "", limits.step_error, secrets),
        step_expected=_clip(step.expected if step else "", limits.step_expected, secrets),
        step_actual=_clip(step.actual if step else "", limits.step_actual, secrets),
        page_url=_clip(step.page_url if step else "", limits.step_error, secrets),
        locator=_clip(str((step.locator or {}) if step else ""), 300, secrets),
        request_method=(step.request_method if step else "") or "",
        request_url=_clip(step.request_url if step else "", 1024, secrets),
        http_status=step.http_status if step else None,
        js_error=_clip(step.js_error if step else "", limits.js_error, secrets),
        console_summary=_clip(step.console_summary if step else "", limits.console, secrets),
        network_summary=_clip(step.network_summary if step else "", limits.network, secrets),
        dom_excerpt=_clip_dom(_read_text_file(dom_path, 200_000, limits.dom * 2, secrets), limits.dom, secrets),
        log_excerpt=_read_text_file(log_path, 60_000, limits.log * 2, secrets)[: limits.log],
        artifacts=[
            EvidenceArtifact(type=item.type, name=Path(item.path).name, size=int(item.size or 0), step_no=int(step.step_no or 0) if step else 0, attempt_no=int(attempt.attempt_no or 0) if attempt else 0)
            for item in artifacts
        ],
        images=images,
    )
    # 指纹放在最后算：它要覆盖上面所有裁剪后的内容
    context.evidence_fingerprint = _fingerprint(context)
    return context


def _fingerprint(context: FailureAnalysisContext) -> str:
    """证据指纹。

    刻意**排除**易变字段（Run ID、时间、出现次数）—— 一个同样的错误在不同 Run
    里重复出现时，我们希望它命中同一份分析，而不是每次都重新问一遍 AI。
    `occurrence_count` 这类统计数字每次都会变，如果把它们算进指纹，
    缓存就永远不会命中，幂等形同虚设。
    """
    payload = {
        "group": context.failure_group_id,
        "category": context.category,
        "rule_id": context.rule_id,
        "strategy": context.strategy,
        "failure_type": context.failure_type,
        "step_action": context.step_action,
        "step_error": context.step_error,
        "step_expected": context.step_expected,
        "step_actual": context.step_actual,
        "http": [context.request_method, context.request_url, context.http_status],
        "js_error": context.js_error,
        "console": context.console_summary,
        "network": context.network_summary,
    }
    canonical = repr(sorted(payload.items(), key=lambda item: item[0]))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def context_char_count(context: FailureAnalysisContext) -> int:
    """估算送出去多少字符，用于限额断言与成本观察。"""
    parts = [
        context.normalized_message,
        context.step_error,
        context.step_expected,
        context.step_actual,
        context.console_summary,
        context.network_summary,
        context.dom_excerpt,
        context.log_excerpt,
        context.js_error,
    ]
    return sum(len(item or "") for item in parts)


__all__ = [
    "EVIDENCE_PRIORITY",
    "EvidenceLimits",
    "build_context",
    "collect_artifacts",
    "context_char_count",
    "pick_representative_occurrence",
]
