"""M4 CI Integration。

职责边界（方案 §27）：

    Web / CLI / GitLab
        ↓
    本包 service.py            解析项目/计划/环境 + 幂等创建 Run + 结果映射
        ↓
    execution.service.create_run / dispatch_run / execute_run_sync
        ↓
    failure.service.analyze_run

**CI 只是触发入口，不是新的执行器。** 这里一行执行逻辑都没有：
创建 Run 走 `create_run`，执行走 `dispatch_run`，失败分析由 `execute_run_sync`
自己触发。三种入口（网页、CLI、GitLab）跑的是同一条链路。

Exit Code 语义（方案 §4 与 §21）是本包最容易写错的地方，见 `service.run_exit_code`：

    0  TestFlowCI 完成了执行，Run 通过
    1  TestFlowCI 完成了执行，但有用例未通过（**包括被测环境连不上**）
    2  TestFlowCI 自己没能完成可靠执行

`ENVIRONMENT_ERROR` 属于 1 而不是 2 —— 测试已经跑完并准确定位到环境问题，
这是 TestFlowCI 干得对，不是它坏了。
"""
