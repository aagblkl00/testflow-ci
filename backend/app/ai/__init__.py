"""AI Provider 与失败分析。

职责边界（方案 §3、§20）：

    FailureGroup / Occurrence / StepResult / Artifact   ← 只读
        ↓
    evidence.py    证据裁剪 + 脱敏（不让整库、整文件进 Prompt）
        ↓
    provider.py + providers/*   把请求发出去，把文本收回来
        ↓
    service.py     结构化校验、幂等、落库、人工确认

**这个包不碰任何测试结果。** 它不写 `ExecutionRun`、不写 `CaseResult`、
不写 `FailureGroup.category`，连执行器都不 import。
AI 的结论只落在 `failure_analyses` 这张自己人的表里，等测试人员来认领。

配置缺省即关闭：没配 `AI_API_KEY` / `AI_MODEL` 时 `settings.ai_enabled` 为假，
所有入口走「跳过」路径，不抛异常 —— 没开 AI 不是错误。
"""

# import 即注册内置 Provider。放在包初始化里，保证任何
# `import backend.app.ai.*` 之后 Provider 都是可用的 —— 注册这件事
# 不该依赖调用方是否记得去 import 某个具体模块。
from . import providers as _providers  # noqa: E402,F401

__all__ = ["_providers"]
