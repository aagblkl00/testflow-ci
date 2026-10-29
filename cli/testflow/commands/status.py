"""`testflow status` —— 查一次已经有 Run 的结果。

只读，不创建、不修改任何东西。退出码沿用该 Run 自己的结论；
Run 还没结束时给 2，因为「查询成功了」和「测试通过了」是两件事，
CI 里用它当门禁时不能把「还在跑」当成绿色。
"""

from __future__ import annotations

import argparse

from ..client import CiClient, CliError
from ..output import ExitCode, emit_json, error, status_summary


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True, help="执行记录 ID")
    parser.add_argument("--json", action="store_true", help="把结果 JSON 打到 stdout（日志仍走 stderr）")
    parser.add_argument("--api", default="", help="TestFlowCI 地址，默认取 TESTFLOW_URL 或 http://127.0.0.1:8000")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    with CiClient(base_url=args.api) as client:
        payload = client.ci_status(args.run_id)

    if args.json:
        emit_json(payload)
    else:
        print(status_summary(payload))

    exit_code = payload.get("exit_code")
    if exit_code is None:
        error(f"Run {args.run_id} 尚未结束（status={payload.get('status')}），无法给出可靠结论，退出码 2")
        return ExitCode.SYSTEM_ERROR
    return int(exit_code)


__all__ = ["configure", "run"]
