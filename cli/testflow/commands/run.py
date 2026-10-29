"""`testflow run` —— 触发一次回归并（默认）等它跑完。

退出码来自服务端算好的 `exit_code`，CLI 自己不重新判断业务结果 ——
判断权只有一处（`ci.service.run_exit_code`），否则 CLI 和 Web 迟早给出不同结论。
CLI 只负责一件事：**等待超时算系统错误（2），且不改动 Run 状态。**
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from ..client import CiClient, CliError
from ..output import ExitCode, emit_json, error, info, run_summary, warn

DEFAULT_TIMEOUT = 3600
DEFAULT_POLL_INTERVAL = 2.0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", required=True, help="回归计划（ID 或名称）")
    parser.add_argument("--env", dest="environment", required=True, help="执行环境（ID 或名称）")
    parser.add_argument("--project", default="", help="项目（ID 或名称）；不填时按计划所属项目推断")
    parser.add_argument("--base-url", default="", help="仅覆盖本次执行的 base_url（仍会过安全门禁）")
    parser.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否等待执行结束（默认等待；--no-wait 只触发并立即返回）",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="等待上限（秒），默认 3600")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL, help="轮询间隔（秒），默认 2")
    parser.add_argument("--json", action="store_true", help="把结果 JSON 打到 stdout（日志仍走 stderr）")
    parser.add_argument("--api", default="", help="TestFlowCI 地址，默认取 TESTFLOW_URL 或 http://127.0.0.1:8000")
    parser.add_argument("--pipeline-id", default="", help="CI Pipeline ID（幂等键的一部分）")
    parser.add_argument("--pipeline-url", default="", help="CI Pipeline 地址")
    parser.add_argument("--commit-sha", default="", help="提交 SHA")
    parser.add_argument("--branch", default="", help="分支")
    parser.add_argument("--triggered-by", default="", help="触发人/来源标识")
    parser.set_defaults(handler=run)


def _poll_once(client: CiClient, run_id: str) -> dict[str, Any]:
    return client.ci_status(run_id)


def run(args: argparse.Namespace) -> int:
    if args.interval < 1:
        # 方案 §33：不能 100ms 轰炸 API。1 秒是硬下限，不给「我就要快」的口子。
        raise CliError("轮询间隔不能小于 1 秒（避免对平台造成无意义的压力）")

    with CiClient(base_url=args.api) as client:
        try:
            payload = client.create_run(
                plan=args.plan,
                environment=args.environment,
                project=args.project,
                base_url=args.base_url,
                source="cli",
                pipeline_id=args.pipeline_id,
                pipeline_url=args.pipeline_url,
                commit_sha=args.commit_sha,
                branch=args.branch,
                triggered_by=args.triggered_by,
            )
        except CliError as exc:
            raise _with_candidates(exc, client, args) from None

        run_id = str(payload.get("run_id") or "")
        created = bool(payload.get("created"))
        if created:
            info(f"已创建执行 Run {run_id}（计划 {args.plan}，环境 {args.environment}）")
        else:
            info(f"幂等命中：Pipeline {args.pipeline_id} 已有执行 Run {run_id}，不重复创建")

        if not args.wait:
            warn("--no-wait：未等待执行结果，退出码只表示「触发成功」，不代表测试通过")
            _emit(payload, args.json)
            return ExitCode.PASSED

        payload = _wait_for(client, run_id, args)
        _emit(payload, args.json)
        exit_code = payload.get("exit_code")
        return int(exit_code) if exit_code is not None else ExitCode.SYSTEM_ERROR


def _wait_for(client: CiClient, run_id: str, args: argparse.Namespace) -> dict[str, Any]:
    """轮询直到结束或超时。超时只影响 CLI 自己的退出码，绝不去改 Run。"""
    deadline = time.monotonic() + max(1.0, float(args.timeout))
    last: dict[str, Any] = {}
    waited = 0.0
    while True:
        last = _poll_once(client, run_id)
        if last.get("finished"):
            return last
        if time.monotonic() >= deadline:
            error(
                f"等待超时（{args.timeout:.0f}s）：Run {run_id} 仍在服务端继续执行，"
                "CLI 退出码 2 只代表「本次等待没能拿到结论」，不代表测试失败。"
            )
            timeout_payload = dict(last)
            timeout_payload["timeout"] = True
            timeout_payload["exit_code"] = ExitCode.SYSTEM_ERROR
            timeout_payload["exit_code_meaning"] = "CLI 等待超时，未能取得执行结论（Run 仍在服务端继续执行）"
            return timeout_payload
        time.sleep(min(float(args.interval), max(0.0, deadline - time.monotonic())))
        waited += float(args.interval)
        if waited >= 10:
            info(f"  等待中… 当前状态 {last.get('status')}")
            waited = 0.0


def _emit(payload: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        emit_json(payload)
    else:
        print(run_summary(payload))


def _with_candidates(exc: CliError, client: CiClient, args: argparse.Namespace) -> CliError:
    """404 时补一句「当前可用的名字有哪些」，省掉一轮来回排查。"""
    message = exc.message
    if "不存在" in message:
        if "计划" in message:
            names = client.candidates("plans", args.project)
            if names:
                message += f"\n        当前可用计划：{', '.join(names)}"
        elif "环境" in message:
            names = client.candidates("environments", args.project)
            if names:
                message += f"\n        当前可用环境：{', '.join(names)}"
        elif "项目" in message:
            names = client.candidates("project")
            if names:
                message += f"\n        当前可用项目：{', '.join(names)}"
    return CliError(message, exit_code=exc.exit_code)


__all__ = ["DEFAULT_POLL_INTERVAL", "DEFAULT_TIMEOUT", "configure", "run"]
