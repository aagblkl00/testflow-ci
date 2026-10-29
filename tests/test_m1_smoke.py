"""M1 冒烟测试。

覆盖不需要浏览器的部分：认证、脱敏、变量系统、安全门禁、API 基本链路。
真实浏览器执行由 scripts/seed_demo.py 与 scripts/verify_ui.py 覆盖。

运行：
    .venv/Scripts/python.exe -m pytest tests -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.auth.security import create_access_token, decode_access_token, hash_password, verify_password  # noqa: E402
from backend.app.execution.action_spec import normalize_action, normalize_steps, resolve_variables  # noqa: E402
from backend.app.execution.playwright_executor import AssertionFailure  # noqa: E402
from backend.app.execution.security import ActionBlocked, assert_environment_ready, assert_step_risk_allowed, validate_request_target  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.utils.redact import MASK, collect_secrets, redact_mapping, redact_text  # noqa: E402

LOCAL_ENV = {
    "name": "本地 Demo 测试环境",
    "base_url": "http://127.0.0.1:8100",
    "environment_type": "TEST",
    "allowed_hosts": ["127.0.0.1", "localhost"],
    "read_only": False,
}


# --------------------------------------------------------------- 认证

def test_password_hash_roundtrip() -> None:
    stored = hash_password("admin123")
    assert stored != "admin123"
    assert verify_password("admin123", stored)
    assert not verify_password("wrong", stored)


def test_password_hash_is_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_access_token_roundtrip_and_tamper() -> None:
    token = create_access_token("user-1", "ADMIN")
    payload = decode_access_token(token)
    assert payload is not None and payload["sub"] == "user-1" and payload["role"] == "ADMIN"
    assert decode_access_token(token + "x") is None
    assert decode_access_token("garbage") is None


# --------------------------------------------------------------- 脱敏

def test_redact_mapping_masks_sensitive_keys() -> None:
    masked = redact_mapping({"username": "demo", "password": "demo123", "Authorization": "Bearer abc", "nested": {"api_key": "k"}})
    assert masked["username"] == "demo"
    assert masked["password"] == MASK
    assert masked["Authorization"] == MASK
    assert masked["nested"]["api_key"] == MASK


def test_redact_mapping_keeps_non_secret_type_fields() -> None:
    # environment_type / case_type 这类字段名里带 type，不能被误伤
    masked = redact_mapping({"environment_type": "TEST", "artifact_type": "trace"})
    assert masked["environment_type"] == "TEST"
    assert masked["artifact_type"] == "trace"


def test_redact_text_masks_literals_and_headers() -> None:
    text = "login with demo123 and Authorization: Bearer sk-abcdef123456\npassword=demo123"
    result = redact_text(text, ["demo123"])
    assert "demo123" not in result
    assert "sk-abcdef123456" not in result


def test_collect_secrets_finds_nested_values() -> None:
    secrets = collect_secrets({"variables": {"username": "demo", "password": "demo123", "token": "t0ken"}})
    assert "demo123" in secrets and "t0ken" in secrets and "demo" not in secrets


# --------------------------------------------------------------- 变量系统

def test_resolve_variables_replaces_placeholders() -> None:
    assert resolve_variables("${base_url}/login", {"base_url": "http://x"}) == "http://x/login"


def test_resolve_variables_raises_with_hint() -> None:
    with pytest.raises(KeyError) as exc:
        resolve_variables("${password}", {"username": "demo"})
    assert "password" in str(exc.value)


def test_normalize_action_fills_defaults() -> None:
    step = normalize_action({"action": "click", "locator_value": "#submit"})
    assert step.action == "click"
    assert step.timeout == 15000
    assert step.risk == "read"
    assert step.name == "click"


def test_normalize_action_tolerates_bad_numbers() -> None:
    step = normalize_action({"action": "wait", "timeout": "abc", "retries": -5})
    assert step.timeout == 15000
    assert step.retries == 0


def test_normalize_steps_skips_non_dict() -> None:
    steps = normalize_steps([{"action": "open", "value": "http://x"}, "bad", 1, None])
    assert len(steps) == 1 and steps[0].action == "open"


def test_assertion_failure_carries_actual() -> None:
    """断言失败必须带上 actual，否则执行详情里的「实际值」是空的。"""
    error = AssertionFailure("文本断言失败", actual="实际页面上的文本")
    assert isinstance(error, AssertionError)  # 保持 AssertionError 语义，错误前缀仍是 AssertionError
    assert error.actual == "实际页面上的文本"
    # 没有 actual 时不能炸
    assert AssertionFailure("元素不存在").actual == ""


# --------------------------------------------------------------- 安全门禁

def test_environment_ready_for_local_host() -> None:
    assert_environment_ready(LOCAL_ENV)  # 不抛异常即通过


def test_production_environment_is_blocked() -> None:
    with pytest.raises(ActionBlocked):
        assert_environment_ready({**LOCAL_ENV, "environment_type": "PROD"})


def test_unknown_host_is_blocked() -> None:
    with pytest.raises(ActionBlocked):
        assert_environment_ready({**LOCAL_ENV, "allowed_hosts": ["example.com"]})


def test_real_payment_gateway_is_blocked() -> None:
    finding = validate_request_target("https://www.alipay.com/pay", LOCAL_ENV)
    assert finding is not None and finding["severity"] == "block"


def test_request_outside_allowlist_is_blocked() -> None:
    finding = validate_request_target("http://evil.example.com/x", LOCAL_ENV)
    assert finding is not None and finding["code"] == "REQUEST_HOST_NOT_ALLOWED"


def test_allowed_request_passes() -> None:
    assert validate_request_target("http://127.0.0.1:8100/login", LOCAL_ENV) is None


def test_read_only_blocks_write_step() -> None:
    with pytest.raises(ActionBlocked):
        assert_step_risk_allowed("write", {**LOCAL_ENV, "read_only": True})


def test_read_only_allows_read_step() -> None:
    assert_step_risk_allowed("read", {**LOCAL_ENV, "read_only": True})


# --------------------------------------------------------------- API

def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["app"] == "TestFlow CI"


def test_login_rejects_bad_password() -> None:
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert response.status_code == 401


def test_login_and_fetch_me() -> None:
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


def test_protected_route_requires_token() -> None:
    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 401
