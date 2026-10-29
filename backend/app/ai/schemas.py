"""AI 分析用到的数据结构。

分三类：

- `EvidenceArtifact` / `FailureAnalysisContext`：**送进 AI 的东西**。
  只包含已经裁剪、已经脱敏的内容，没有任何二进制。
- `ProviderResponse`：**AI 回来的原始东西**。`raw_text` 是模型原话，
  可能不是合法 JSON —— 校验在 `service.py` 做，不在这里做，
  因为「收回来」和「收得对」是两件事，混在一起就没法区分
  「网络失败」和「模型胡说」。
- `FailureAnalysisResult`：**校验通过的结构化结论**。只有它能入库。

对 `FailureAnalysisResult` 的几个刻意约束：

- **没有 PASS/FAIL 字段**。不是「不填」，是压根不存在 —— 结构上就无法让 AI 参与
  测试判定（方案原则 1）。
- `confidence` 会做兜底归一：模型常回 0-100，统一收成 0-1。
- `evidence` 允许模型回字符串，自动包成单元素列表，少一种 422。
- `ai_suggested_category` 必须落在既有六类里，否则丢弃 —— 与其存一个
  「可能的 PRODCUT_BUG」这种脏值，不如留空。
- `suggested_owner` 字段名就写着 suggested，文档与提示词都反复强调
  它只是「建议关注」，不是责任人认定（方案原则 4）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import FAILURE_CATEGORIES


class EvidenceArtifact(BaseModel):
    """证据清单里的一条。只登记引用，不塞内容 —— 除非 Provider 明确支持读图。"""

    type: str
    name: str = ""
    size: int = 0
    step_no: int = 0
    attempt_no: int = 0


class EvidenceImage(BaseModel):
    """可选的多模态证据。只有 Provider 声明支持视觉时才会填充。"""

    media_type: str = "image/png"
    data_base64: str = ""
    name: str = ""
    bytes_size: int = 0


class FailureAnalysisContext(BaseModel):
    """一次分析的完整输入。

    字段命名与数据库列保持一致，便于阅读时对照；
    所有文本字段在这里都**已经过 redact 与裁剪**。
    """

    model_config = ConfigDict(extra="forbid")

    # 项目 / 计划 / 环境
    project_id: str = ""
    project_name: str = ""
    plan_name: str = ""
    environment_name: str = ""
    environment_type: str = ""

    # Run 与 CI 上下文
    run_id: str = ""
    run_status: str = ""
    commit_sha: str = ""
    branch: str = ""
    pipeline_id: str = ""
    pipeline_url: str = ""

    # FailureGroup（M3 的确定性结论，原样带进去供 AI 参考，不要求它推翻或附和）
    failure_group_id: str = ""
    group_title: str = ""
    category: str = "UNKNOWN"
    rule_id: str = ""
    rule_reason: str = ""
    occurrence_count: int = 0
    failed_case_count: int = 0
    failed_run_count: int = 0
    flaky_count: int = 0

    # Fingerprint
    fingerprint: str = ""
    strategy: str = ""
    failure_type: str = ""
    normalized_message: str = ""
    normalized_expected: str = ""
    normalized_actual: str = ""

    # 代表出现
    occurrence_id: str = ""
    case_code: str = ""
    case_name: str = ""
    case_status: str = ""
    attempt_no: int = 0
    step_no: int = 0

    # 失败步骤现场
    step_action: str = ""
    step_error: str = ""
    step_expected: str = ""
    step_actual: str = ""
    page_url: str = ""
    locator: str = ""
    request_method: str = ""
    request_url: str = ""
    http_status: int | None = None
    js_error: str = ""
    console_summary: str = ""
    network_summary: str = ""

    # 证据
    dom_excerpt: str = ""
    log_excerpt: str = ""
    artifacts: list[EvidenceArtifact] = Field(default_factory=list)
    images: list[EvidenceImage] = Field(default_factory=list)
    evidence_fingerprint: str = ""

    def fingerprint(self) -> str:
        """证据指纹：决定「这次分析」和「上次分析」是不是同一份输入。"""
        return self.evidence_fingerprint or hashlib.sha256(repr(self.model_dump()).encode("utf-8")).hexdigest()[:40]


class ProviderResponse(BaseModel):
    """Provider 层返回的原始结果。`ok=False` 表示这次调用没成功。"""

    ok: bool = False
    raw_text: str = ""
    parsed: dict[str, Any] | None = None
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    attempts: int = 1
    error: str = ""


class FailureAnalysisResult(BaseModel):
    """校验通过的结构化结论。**只有这个结构能写进数据库。**"""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    root_cause: str = ""
    evidence: list[str] = Field(default_factory=list)
    related_commit: str = ""
    suggested_owner: str = ""
    confidence: float = 0.0
    uncertainty: str = ""
    recommendation: str = ""
    ai_suggested_category: str = ""

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, value: Any) -> list[str]:
        """模型经常把 evidence 写成一段文本，接受它，别因此判整次分析失败。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.splitlines() if item.strip()][:12]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()][:12]
        return [str(value)]

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> float:
        """把 0-100 的写法收成 0-1；非法值一律 0，不做猜测。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number > 1.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))

    @field_validator("ai_suggested_category", mode="before")
    @classmethod
    def _validate_category(cls, value: Any) -> str:
        """只接受既有六类；其它一律丢弃，宁可留空也不存脏值。"""
        text = str(value or "").strip().upper()
        return text if text in FAILURE_CATEGORIES else ""

    def is_meaningful(self) -> bool:
        """至少要有一句总结或一个可能原因，否则这次分析等于没产出。"""
        return bool(self.summary.strip() or self.root_cause.strip())


__all__ = [
    "EvidenceArtifact",
    "EvidenceImage",
    "FailureAnalysisContext",
    "FailureAnalysisResult",
    "ProviderResponse",
]
