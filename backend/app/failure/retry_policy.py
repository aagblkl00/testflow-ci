"""按失败类型决定 Retry 策略。

M2 的问题是「统一按 plan.retry_count 重试」——业务断言失败也会被重试，
这会把一个真 bug 拖成 FLAKY，掩盖问题。

M3 引入 RetryPolicy：

    effective_retries = min(plan.retry_count, policy_retries(category))

- `plan.retry_count` 仍然是**上限**（用户显式开启重试的意愿），
  所以 retry_count=0 的计划行为与 M1/M2 完全一致，不会因为 M3 就突然开始重试。
- `policy_retries(category)` 决定「这类失败值不值得重试」。
- 环境被门禁拦下（blocked）永远不重试——重试没有任何意义。

数值全部来自配置，不硬编码在执行器里：
1. 内置默认值（就是下面 DEFAULT_RETRIES）；
2. 存在 `config/retry_policy.json` 时用文件覆盖；
3. 环境变量 `RETRY_POLICY_OVERRIDES` 可再覆盖（形如 `PRODUCT_BUG=0,UNKNOWN=2`）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger("testflow.failure.retry")

# 默认策略（用户给定的表）
DEFAULT_RETRIES: dict[str, int] = {
    "PRODUCT_BUG": 0,
    "ENVIRONMENT_ERROR": 1,
    "AUTOMATION_ERROR": 0,
    "DATA_ERROR": 0,
    "FLAKY": 1,
    "UNKNOWN": 1,
}

POLICY_FILE_NAME = "retry_policy.json"


def policy_file_path() -> Path:
    return settings.project_root / "config" / POLICY_FILE_NAME


def _load_file_overrides() -> dict[str, int]:
    path = policy_file_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 %s 失败，使用内置默认策略：%s", path, exc)
        return {}
    result: dict[str, int] = {}
    for key, value in (raw or {}).items():
        name = str(key).strip().upper()
        if name not in DEFAULT_RETRIES:
            continue
        try:
            result[name] = max(0, min(5, int(value)))
        except (TypeError, ValueError):
            continue
    return result


def _load_env_overrides() -> dict[str, int]:
    raw = os.getenv("RETRY_POLICY_OVERRIDES", "").strip()
    if not raw:
        return {}
    result: dict[str, int] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        name = key.strip().upper()
        if name not in DEFAULT_RETRIES:
            continue
        try:
            result[name] = max(0, min(5, int(value.strip())))
        except ValueError:
            continue
    return result


def effective_policy() -> dict[str, int]:
    """合并后的生效策略（默认 ← 文件 ← 环境变量）。"""
    merged = dict(DEFAULT_RETRIES)
    merged.update(_load_file_overrides())
    merged.update(_load_env_overrides())
    return merged


def policy_retries(category: str) -> int:
    """某个分类允许的重试次数。未知分类按 UNKNOWN 处理。"""
    name = str(category or "UNKNOWN").strip().upper()
    return effective_policy().get(name, DEFAULT_RETRIES["UNKNOWN"])


def max_policy_retries() -> int:
    """策略里最大的重试次数，用来给循环设一个安全上界。"""
    return max(effective_policy().values() or [0])


def effective_retries(
    plan_retry_count: int,
    category: str | None,
    *,
    auto_retry: bool = False,
    blocked: bool = False,
    case_is_flaky: bool = False,
) -> int:
    """算出这个失败允许的重试次数。优先级见模块 docstring。"""
    if blocked:
        return 0
    if case_is_flaky and auto_retry:
        # 历史上已经 Flaky 的用例：重试一次是最有价值的动作
        return max(policy_retries("FLAKY"), max(0, int(plan_retry_count or 0)))
    explicit = max(0, int(plan_retry_count or 0))
    if explicit > 0:
        return explicit
    if auto_retry:
        return policy_retries(category or "UNKNOWN")
    return 0


def hard_attempt_cap(plan_retry_count: int, *, auto_retry: bool = False) -> int:
    """单次用例最多跑几次（循环上界）。

    分类只有在第一次失败之后才知道，所以不能在上界里用分类收窄，
    这里只保证「不会跑超过任何可能值」。
    """
    explicit = max(0, int(plan_retry_count or 0))
    ceiling = max(explicit, max_policy_retries() if auto_retry else 0)
    return 1 + ceiling


def planned_attempts(
    plan_retry_count: int,
    category: str | None = None,
    *,
    auto_retry: bool = False,
    blocked: bool = False,
    case_is_flaky: bool = False,
) -> int:
    """这个用例最多跑几次。

    category=None 表示还不知道失败类型（第一次尝试之前），
    此时按用户配置算，真正的收窄发生在第一次失败之后（见 should_retry）。
    """
    return 1 + effective_retries(
        plan_retry_count,
        category,
        auto_retry=auto_retry,
        blocked=blocked,
        case_is_flaky=case_is_flaky,
    )


def should_retry(
    plan_retry_count: int,
    attempt_no: int,
    category: str | None = None,
    *,
    auto_retry: bool = False,
    blocked: bool = False,
    case_is_flaky: bool = False,
) -> bool:
    """第 attempt_no 次尝试失败后，还要不要再试一次。"""
    if blocked:
        return False
    return attempt_no < planned_attempts(
        plan_retry_count,
        category,
        auto_retry=auto_retry,
        case_is_flaky=case_is_flaky,
    )


def explain(
    plan_retry_count: int,
    category: str | None,
    *,
    auto_retry: bool = False,
    blocked: bool = False,
    case_is_flaky: bool = False,
) -> dict[str, Any]:
    """给人看的决策说明，用于 API 与验收断言。"""
    retries = effective_retries(
        plan_retry_count, category, auto_retry=auto_retry, blocked=blocked, case_is_flaky=case_is_flaky
    )
    if blocked:
        source = "blocked"
    elif max(0, int(plan_retry_count or 0)) > 0:
        source = "explicit_retry_count"
    elif case_is_flaky and auto_retry:
        source = "flaky_history"
    elif auto_retry:
        source = "policy"
    else:
        source = "disabled"
    return {
        "category": (category or "UNKNOWN"),
        "auto_retry": bool(auto_retry),
        "explicit_retry_count": max(0, int(plan_retry_count or 0)),
        "blocked": bool(blocked),
        "case_is_flaky": bool(case_is_flaky),
        "retries": retries,
        "attempts": 1 + retries,
        "source": source,
    }


def as_dict(policy: dict[str, int] | None = None) -> dict[str, Any]:
    active = policy or effective_policy()
    return {
        "defaults": dict(DEFAULT_RETRIES),
        "effective": active,
        "source": {
            "file": str(policy_file_path()),
            "file_exists": policy_file_path().exists(),
            "env_overrides": _load_env_overrides(),
        },
    }


__all__ = [
    "DEFAULT_RETRIES",
    "effective_policy",
    "policy_retries",
    "max_policy_retries",
    "effective_retries",
    "hard_attempt_cap",
    "planned_attempts",
    "should_retry",
    "explain",
    "as_dict",
    "policy_file_path",
]
