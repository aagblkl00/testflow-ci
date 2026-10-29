"""允许 `python -m backend.app.migrations` 直接执行迁移。"""

from . import _main

if __name__ == "__main__":
    raise SystemExit(_main())
