"""测试隔离：强制使用临时数据库与临时存储目录。

必须在任何 backend.* 模块导入之前设置环境变量，
避免测试读到、写坏开发者本机的 testflow.db 与 storage/ 真实数据。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="testflow_pytest_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_tmp / "storage")
os.environ["TESTFLOW_SECRET"] = "pytest-only-secret"
os.environ["TESTFLOW_CI_TOKEN"] = "pytest-only-ci-token"
os.environ["BOOTSTRAP_ADMIN_USERNAME"] = "admin"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin123"
