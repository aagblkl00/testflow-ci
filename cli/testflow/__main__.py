"""`testflow` 命令入口。

两种调用方式都支持，行为完全一致：

    testflow run --plan smoke --env test          # 安装后（pyproject 的 console_scripts）
    python -m cli.testflow run --plan smoke --env test

退出码只有三个（方案 §4）：

    0  TestFlowCI 完成执行，Run 通过
    1  TestFlowCI 完成执行，但有用例未通过（含被测环境问题）
    2  TestFlowCI 自己没能完成可靠执行（含参数非法、Token 无效、等待超时）
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .client import CliError
from .commands import COMMANDS
from .output import ExitCode, error

PROGRAM = "testflow"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="TestFlow CI 命令行：触发 Web 回归并读取结果",
        epilog=(
            "环境变量：TESTFLOW_CI_TOKEN（必填，CI Token）、TESTFLOW_URL（平台地址）。"
            " 刻意不提供 --token 参数 —— 命令行参数会留在 history 与 ps 里。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{run,status}")
    for module in COMMANDS:
        name = module.__name__.rsplit(".", 1)[-1]
        # 取 docstring 第一行当帮助文本，顺手去掉 Markdown 反引号 ——
        # argparse 不渲染 Markdown，`testflow run` 会原样带出来，很难看。
        summary = (module.__doc__ or "").strip().splitlines()[0].replace("`", "").strip()
        sub = subparsers.add_parser(name, help=summary)
        module.configure(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return ExitCode.SYSTEM_ERROR

    try:
        return int(handler(args))
    except CliError as exc:
        error(exc.message)
        return int(exc.exit_code)
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        error("已中断")
        return ExitCode.SYSTEM_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
