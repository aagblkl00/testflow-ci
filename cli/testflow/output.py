"""输出层：退出码定义 + 人类可读摘要。

约定（方案 §8、§19）：**JSON 只走 stdout，其它一切走 stderr。**
这样 `testflow run --json | jq .run_id` 永远拿到干净的一行 JSON，
进度、警告、提示都不会污染管道。

另外，输出里**绝不出现 Token**：所有打印都只碰服务端返回的字段，
而这些字段在建 Run 时就已经过了一次 redact。
"""

from __future__ import annotations

import json
import sys
from typing import Any


class ExitCode:
    """CI 退出码。三个值，不允许出现第四个。"""

    PASSED = 0
    TEST_FAILURE = 1
    SYSTEM_ERROR = 2


def info(message: str) -> None:
    """进度信息 → stderr。"""
    print(message, file=sys.stderr)


def warn(message: str) -> None:
    print(f"[warn] {message}", file=sys.stderr)


def error(message: str) -> None:
    print(f"[error] {message}", file=sys.stderr)


def emit_json(payload: dict[str, Any]) -> None:
    """稳定 JSON → stdout。不缩进，方便管道处理。"""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=False))


def _failure_group_lines(groups: dict[str, Any], *, indent: str = "  ") -> list[str]:
    total = int(groups.get("total") or 0)
    categories = groups.get("categories") or {}
    lines = [f"{indent}Total: {total}"]
    for name in sorted(categories):
        lines.append(f"{indent}{name}: {categories[name]}")
    return lines


def _pipeline_lines(payload: dict[str, Any], *, style: str = "compact") -> list[str]:
    lines: list[str] = []
    if style == "full":
        lines.append(f"  Trigger: {payload.get('trigger_source', '') or '-'}")
    for label, key in (("Branch", "branch"), ("Commit", "commit_sha"), ("Pipeline", "pipeline_id")):
        if payload.get(key):
            lines.append(f"  {label}: {payload[key]}")
    if payload.get("pipeline_url"):
        lines.append(f"  URL: {payload['pipeline_url']}")
    return lines


def _exit_lines(exit_code: int | None, meaning: str = "") -> list[str]:
    if exit_code is None:
        return ["Exit Code: (执行未结束)"]
    lines = [f"Exit Code: {exit_code}"]
    if meaning:
        lines.append(f"  {meaning}")
    return lines


def run_summary(payload: dict[str, Any]) -> str:
    """`testflow run` 的摘要。刻意不输出 step 级明细 ——
    CI 控制台要的是「这次能不能过」，细节去失败中心看。"""
    summary = payload.get("summary") or {}
    groups = payload.get("failure_groups") or {}

    lines = [
        "TestFlowCI",
        f"Project: {payload.get('project_id', '')}",
        f"Plan: {payload.get('plan_name') or payload.get('plan_id', '')}",
        f"Environment: {payload.get('environment') or payload.get('environment_id', '')}",
        "",
        f"Run: {payload.get('run_id', '')}",
        f"Status: {payload.get('status', '')}",
        "",
        "Cases:",
        f"  {summary.get('total', 0)} total",
        f"  {summary.get('passed', 0)} passed",
        f"  {summary.get('failed', 0)} failed",
    ]
    for key, label in (("skipped", "skipped"), ("cancelled", "cancelled"), ("flaky", "flaky")):
        if summary.get(key):
            lines.append(f"  {summary[key]} {label}")

    lines.append("")
    lines.append("Failure Groups:")
    lines.extend(_failure_group_lines(groups))

    pipeline = _pipeline_lines(payload)
    if pipeline:
        lines.append("")
        lines.append("Pipeline:")
        lines.extend(pipeline)

    if payload.get("error_message"):
        lines += ["", f"Reason: {payload['error_message']}"]

    lines.append("")
    lines.append("Run completed" if payload.get("finished") else "Run still running")
    lines.extend(_exit_lines(payload.get("exit_code"), str(payload.get("exit_code_meaning") or "")))
    return "\n".join(lines)


def status_summary(payload: dict[str, Any]) -> str:
    """`testflow status` 的摘要。比 run 多显示 Flaky 与 Pipeline 元数据。"""
    summary = payload.get("summary") or {}
    groups = payload.get("failure_groups") or {}

    lines = [
        f"Run: {payload.get('run_id', '')}",
        f"Status: {payload.get('status', '')}",
        "",
        "Cases:",
        f"  Total: {summary.get('total', 0)}",
        f"  Passed: {summary.get('passed', 0)}",
        f"  Failed: {summary.get('failed', 0)}",
        f"  Skipped: {summary.get('skipped', 0)}",
        f"  Cancelled: {summary.get('cancelled', 0)}",
        "",
        "Failure Groups:",
    ]
    lines.extend(_failure_group_lines(groups))
    lines += ["", "Flaky:", f"  {summary.get('flaky', 0)}", "", "Pipeline:"]
    lines.extend(_pipeline_lines(payload, style="full") or ["  -"])

    if payload.get("error_message"):
        lines += ["", f"Reason: {payload['error_message']}"]

    lines += ["", "Exit Code:", f"  {payload.get('exit_code') if payload.get('exit_code') is not None else '(未结束)'}"]
    return "\n".join(lines)


__all__ = ["ExitCode", "emit_json", "error", "info", "run_summary", "status_summary", "warn"]
