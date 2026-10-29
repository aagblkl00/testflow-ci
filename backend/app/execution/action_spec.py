"""ActionSpec 与变量系统。

移植自 Lodestar（北极星）`backend/models.py` + `backend/automation/action_spec.py`，
去掉桌面壳相关字段，并把定位器改为「inline 描述 + 候选优先级」模型
（M1 不引入元素资产表，元素资产库与自愈留给 M2/M3）。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from typing import Any

ACTION_LABELS: dict[str, str] = {
    "open": "打开页面",
    "click": "点击",
    "fill": "输入",
    "clear": "清空",
    "select": "下拉选择",
    "select_value": "按值选择",
    "upload": "上传文件",
    "wait": "等待",
    "wait_url": "等待页面跳转",
    "screenshot": "截图",
    "hover": "悬停",
    "press": "键盘操作",
    "assert_text": "校验文本包含",
    "assert_equals": "校验文本相等",
    "assert_value": "校验输入值",
    "assert_attribute": "校验属性",
    "assert_exists": "校验元素存在",
    "assert_not_exists": "校验元素不存在",
    "assert_visible": "校验元素可见",
    "assert_not_visible": "校验元素不可见",
    "assert_enabled": "校验元素可用",
    "assert_url": "校验页面地址",
    "assert_title": "校验页面标题",
    "assert_count": "校验元素数量",
}

ASSERT_ACTIONS: frozenset[str] = frozenset(action for action in ACTION_LABELS if action.startswith("assert_"))

ACTIONS_WITHOUT_LOCATOR: frozenset[str] = frozenset({"open", "wait", "wait_url", "assert_url", "assert_title", "screenshot"})

# 定位优先级（方案 §5）：role > label > test_id > CSS > XPath
LOCATOR_PRIORITY: tuple[str, ...] = ("role", "label", "testid", "css", "xpath", "placeholder", "text", "name")

LOCATOR_TYPES: frozenset[str] = frozenset(LOCATOR_PRIORITY)

VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(slots=True)
class ActionSpec:
    name: str
    action: str
    locator_type: str = "css"
    locator_value: str = ""
    element_id: str = ""
    value: str = ""
    expected: str = ""
    timeout: int = 15000
    retries: int = 0
    screenshot: bool = False
    risk: str = "read"
    continue_on_failure: bool = False
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def needs_locator(self) -> bool:
        return self.action not in ACTIONS_WITHOUT_LOCATOR


_DEFAULTS: dict[str, Any] = {
    "name": "",
    "action": "click",
    "locator_type": "css",
    "locator_value": "",
    "element_id": "",
    "value": "",
    "expected": "",
    "timeout": 15000,
    "retries": 0,
    "screenshot": False,
    "risk": "read",
    "continue_on_failure": False,
    "disabled": False,
}

_FIELD_NAMES = {item.name for item in fields(ActionSpec)}


def normalize_action(raw: dict[str, Any]) -> ActionSpec:
    """Coerce an incoming dict into a complete ActionSpec, tolerating missing keys."""
    data = {key: value for key, value in dict(raw or {}).items() if key in _FIELD_NAMES}
    for key, default in _DEFAULTS.items():
        data.setdefault(key, default)
    if not data.get("name"):
        data["name"] = data.get("action") or "测试步骤"
    if data.get("action") not in ACTION_LABELS:
        # 未知动作原样保留，执行阶段会显式报错，避免静默跳过。
        data["action"] = str(data.get("action") or "click")
    try:
        data["timeout"] = int(data.get("timeout") or 15000)
    except (TypeError, ValueError):
        data["timeout"] = 15000
    try:
        data["retries"] = max(0, int(data.get("retries") or 0))
    except (TypeError, ValueError):
        data["retries"] = 0
    data["screenshot"] = bool(data.get("screenshot"))
    data["disabled"] = bool(data.get("disabled"))
    data["continue_on_failure"] = bool(data.get("continue_on_failure"))
    return ActionSpec(**data)


def normalize_steps(raw_steps: Any) -> list[ActionSpec]:
    if not isinstance(raw_steps, (list, tuple)):
        return []
    return [normalize_action(item) for item in raw_steps if isinstance(item, dict)]


def resolve_variables(value: Any, variables: dict[str, Any]) -> str:
    """Replace ${name} placeholders. Raises KeyError with a readable hint when undefined."""
    if value is None:
        return ""
    text = str(value)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            available = "、".join(list(variables.keys())[:20]) or "（空）"
            hint = "它是敏感变量，可能只在环境变量中提供" if "password" in key.lower() else "请检查变量名大小写，或在环境/流程变量中定义"
            raise KeyError(f"测试变量未定义：{key}；{hint}。当前可用变量：{available}")
        return str(variables[key])

    return VARIABLE_PATTERN.sub(replace, text)


def build_variables(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Merge variable layers, later layers win. None values are ignored."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if isinstance(layer, dict):
            merged.update({key: value for key, value in layer.items() if value is not None})
    return merged


def redact_variables(variables: dict[str, Any], sensitive_keys: set[str] | None = None) -> dict[str, Any]:
    """Return a copy safe for logs, prompts and notifications."""
    from ..utils.redact import MASK, is_sensitive_key

    sensitive = sensitive_keys or set()
    result: dict[str, Any] = {}
    for key, value in variables.items():
        if key in sensitive or is_sensitive_key(key):
            result[key] = MASK
        else:
            result[key] = value
    return result
