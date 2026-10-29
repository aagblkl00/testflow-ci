"""子命令集合。每个命令暴露 `configure(parser)` 与 `run(args) -> int`。"""

from __future__ import annotations

from . import run, status

COMMANDS = (run, status)

__all__ = ["COMMANDS", "run", "status"]
