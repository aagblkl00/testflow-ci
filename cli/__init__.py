"""TestFlow CI 的命令行入口。

设计原则（方案 §28）：**CLI 只通过 HTTP 与平台对话，绝不直接碰数据库。**
所以这里只有一个 httpx 客户端，没有 SQLAlchemy、没有 import backend。

用法：

    testflow run --plan smoke --env test
    testflow run --plan smoke --env test --json
    testflow status --run-id 8a6f0f726953

    python -m cli.testflow run --plan smoke --env test
"""
