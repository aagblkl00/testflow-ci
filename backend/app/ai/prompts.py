"""提示词。

`PROMPT_VERSION` 是分析幂等键的一部分：改了提示词，等于换了输入，
旧的分析结果不应该被当成「同一份分析的缓存」继续复用 —— 应该允许重算。
所以改提示词时**必须**同时改版本号。

提示词里反复强调的三条禁令，直接对应方案的原则 1/3/4：

1. 不要输出 PASS/FAIL，不要建议「这个用例应该通过」；
2. 不要创建 Bug、不要给出缺陷单号；
3. `suggested_owner` 只能是「建议先看哪个模块/哪个人」，
   而且必须写清依据（提交、模块、历史），不能凭 commit author 直接定责。

另外要求它必须显式写出 `uncertainty` —— 一个不肯说「我不确定」的模型，
在失败分析这种场景里是有害的。
"""

from __future__ import annotations

import json

from .schemas import FailureAnalysisContext

PROMPT_VERSION = "m5-1"

SYSTEM_PROMPT = """你是 Web 自动化测试的失败分析助手。你的输出只供测试人员参考，不参与任何判定。

硬性规则（违反即为无效输出）：
1. 你**不能**判定测试通过或失败，也不要建议「应该判 PASS/FAIL」——测试结果由测试框架决定，已经确定了。
2. 你**不能**创建缺陷单，不要输出 Bug 编号。
3. 你**不能**认定责任人。`suggested_owner` 只表示「建议优先关注哪个模块或角色」，且必须给出依据。
4. 你**不能**断言确定的责任提交。`related_commit` 只在证据明确指向某个提交时才填，否则留空字符串。
5. 每条结论都要能在 `evidence` 里找到对应依据；没有依据就不要写。
6. 必须如实填写 `uncertainty`：说明还缺哪些信息才能下结论。不确定时宁可保守。
7. `ai_suggested_category` 只能从这六个值里选一个，或在无法判断时留空：
   PRODUCT_BUG / ENVIRONMENT_ERROR / AUTOMATION_ERROR / DATA_ERROR / FLAKY / UNKNOWN
   注意：这只是你的建议，不会覆盖系统已有的确定性分类。

只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性前后缀。字段如下：
{
  "summary": "一句话总结这次失败",
  "root_cause": "可能的原因",
  "evidence": ["支撑结论的具体证据，逐条列出"],
  "related_commit": "相关提交 SHA，不确定就留空",
  "suggested_owner": "建议关注的模块或角色，说明依据",
  "confidence": 0.0,
  "uncertainty": "哪些信息不足、还需要什么",
  "recommendation": "建议测试人员接下来检查什么",
  "ai_suggested_category": ""
}
`confidence` 是 0 到 1 之间的小数。"""


def build_user_prompt(context: FailureAnalysisContext) -> str:
    """把证据包渲染成一条用户消息。

    刻意用「分节文本」而不是直接把 dict 塞进 JSON：模型对结构清晰的短文本
    比对深层嵌套 JSON 更不容易看漏字段，而失败的现场信息本来就偏叙述性。
    """
    lines: list[str] = []
    add = lines.append

    add("## 基本信息")
    add(f"- 项目：{context.project_name or context.project_id}")
    add(f"- 计划：{context.plan_name or '-'}")
    add(f"- 环境：{context.environment_name or '-'}（{context.environment_type or '-'}）")
    add(f"- Run：{context.run_id}（状态 {context.run_status}）")
    if context.branch or context.commit_sha or context.pipeline_id:
        add(f"- 分支：{context.branch or '-'}　提交：{context.commit_sha or '-'}　Pipeline：{context.pipeline_id or '-'}")

    add("")
    add("## 系统已有的确定性分类（仅供参考，不要覆盖它）")
    add(f"- 分类：{context.category}")
    add(f"- 命中规则：{context.rule_id or '-'}")
    add(f"- 规则理由：{context.rule_reason or '-'}")
    add(f"- 影响：{context.failed_case_count} 个用例 / {context.failed_run_count} 次执行 / 共出现 {context.occurrence_count} 次")
    if context.flaky_count:
        add(f"- 其中 Flaky 出现：{context.flaky_count} 次")

    add("")
    add("## 失败指纹")
    add(f"- 策略：{context.strategy}　类型：{context.failure_type}")
    add(f"- 归一化错误：{context.normalized_message or '-'}")
    if context.normalized_expected or context.normalized_actual:
        add(f"- 期望：{context.normalized_expected or '-'}")
        add(f"- 实际：{context.normalized_actual or '-'}")

    add("")
    add("## 失败现场（代表出现）")
    add(f"- 用例：{context.case_code} {context.case_name}（{context.case_status}）")
    add(f"- 第 {context.attempt_no or '-'} 次尝试，第 {context.step_no or '-'} 步")
    add(f"- 动作：{context.step_action or '-'}")
    add(f"- 页面地址：{context.page_url or '-'}")
    if context.locator:
        add(f"- 定位器：{context.locator}")
    add(f"- 错误信息：{context.step_error or '-'}")
    if context.step_expected or context.step_actual:
        add(f"- 步骤期望：{context.step_expected or '-'}")
        add(f"- 步骤实际：{context.step_actual or '-'}")

    if context.request_url or context.http_status:
        add("")
        add("## 相关请求")
        add(f"- {context.request_method or 'HTTP'} {context.request_url or '-'} → {context.http_status if context.http_status else '-'}")

    if context.js_error:
        add("")
        add("## 页面脚本异常")
        add(context.js_error)

    if context.console_summary:
        add("")
        add("## 浏览器控制台")
        add("```")
        add(context.console_summary)
        add("```")

    if context.network_summary:
        add("")
        add("## 网络异常")
        add("```")
        add(context.network_summary)
        add("```")

    if context.dom_excerpt:
        add("")
        add("## 失败页面 DOM 片段")
        add("```html")
        add(context.dom_excerpt)
        add("```")

    if context.log_excerpt:
        add("")
        add("## 步骤日志")
        add("```")
        add(context.log_excerpt)
        add("```")

    if context.artifacts:
        add("")
        add("## 可用证据文件（仅清单，未提供内容）")
        for item in context.artifacts:
            add(f"- [{item.type}] {item.name}（步骤 {item.step_no}，第 {item.attempt_no} 次尝试，{item.size} 字节）")

    add("")
    add("请按系统提示的 JSON 结构输出分析。")
    return "\n".join(lines)


def build_repair_prompt(raw_text: str, error: str) -> str:
    """结构化校验失败后的修复提示。

    只重试一次。第二次还不行就认输 —— 无限追问既烧钱又会把一次
    普通的格式问题拖成长时间的挂起，而用户要的是一个明确结论
    （哪怕是「分析失败」）。
    """
    return (
        "你上一次的输出无法被解析为要求的 JSON。\n"
        f"解析错误：{error}\n\n"
        "上一次的输出是：\n"
        "----\n"
        f"{raw_text[:4000]}\n"
        "----\n\n"
        "请只输出一个合法 JSON 对象，字段与系统提示完全一致，"
        "不要 markdown 代码块，不要任何额外文字。"
    )


def render_schema_hint() -> str:
    """给接口/文档展示用的字段说明。"""
    return json.dumps(
        {
            "summary": "str",
            "root_cause": "str",
            "evidence": ["str"],
            "related_commit": "str",
            "suggested_owner": "str",
            "confidence": "float 0-1",
            "uncertainty": "str",
            "recommendation": "str",
            "ai_suggested_category": "str",
        },
        ensure_ascii=False,
        indent=2,
    )


__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT", "build_repair_prompt", "build_user_prompt", "render_schema_hint"]
