"""M5 本地 stub 服务：OpenAI-compatible 端点 + 企业微信 Webhook 端点。

**它的用途是把「Provider 与真实 HTTP 服务交互」这件事测实。**

为什么需要它：本机既没有 AI 的 API Key，也没有企业微信 Webhook 地址。
如果因此就不测，那 Provider 里的 URL 拼接、请求体、响应解析、errcode 判定、
重试策略、脱敏全都是「写完没跑过」——而这类代码恰恰是最容易写错的。

所以这里起一个真的 HTTP 服务（真 socket、真状态码、真 JSON），让
`OpenAICompatibleProvider` 和 `WeComWebhookProvider` 按真实方式去请求它。
这**不能**替代「真实 AI / 真实企业微信」的验收（那需要真凭据），
但它能证明「换成一个符合协议的真实服务，我们的代码是对的」。

用法：
    python scripts/m5_stub_server.py --port 8300

控制接口（给自测脚本用）：
    POST /__mode    {"ai": "valid|invalid_json|http_500_once|no_response_format", "wecom": "ok|fail|retry_then_ok|fatal|http_500"}
    GET  /__requests  查看收到的请求（用于断言「真的发出去了什么」）
    POST /__reset     清空计数与记录
"""

from __future__ import annotations

import argparse
import json
import threading
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="M5 Stub Server")

STATE: dict[str, Any] = {"ai": "valid", "wecom": "ok"}
COUNTS: dict[str, int] = {"ai": 0, "wecom": 0}
REQUESTS: list[dict[str, Any]] = []
LOCK = threading.Lock()

VALID_ANALYSIS = {
    "summary": "疑似订单提交接口异常导致下单中断",
    "root_cause": "提交订单时服务端返回错误，页面仅渲染错误提示，订单未创建",
    "evidence": [
        "失败步骤为提交订单后的断言，错误横幅 order-error 处于可见状态",
        "步骤现场记录到 POST /orders 相关的异常响应",
        "前置的登录与打开订单页步骤均通过",
    ],
    "related_commit": "cafebabe",
    "suggested_owner": "建议先看订单服务模块（依据：失败集中在提交环节）",
    "confidence": 0.79,
    "uncertainty": "没有服务端日志，无法区分是空指针还是下游超时",
    "recommendation": "复现同一笔下单请求，并检查订单服务在同一时间点的错误日志",
    "ai_suggested_category": "PRODUCT_BUG",
}


def _record(kind: str, request: Request, body: Any) -> None:
    with LOCK:
        REQUESTS.append(
            {
                "kind": kind,
                "path": request.url.path,
                "authorization": request.headers.get("authorization", ""),
                "token_header": request.headers.get("x-testflow-token", ""),
                "body": body,
            }
        )


@app.post("/__mode")
async def set_mode(payload: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:
    with LOCK:
        if payload.get("ai"):
            STATE["ai"] = str(payload["ai"])
            COUNTS["ai"] = 0
        if payload.get("wecom"):
            STATE["wecom"] = str(payload["wecom"])
            COUNTS["wecom"] = 0
    return JSONResponse({"state": STATE, "counts": COUNTS})


@app.get("/__requests")
async def list_requests() -> JSONResponse:
    with LOCK:
        return JSONResponse({"requests": list(REQUESTS), "counts": dict(COUNTS), "state": dict(STATE)})


@app.post("/__reset")
async def reset() -> JSONResponse:
    with LOCK:
        REQUESTS.clear()
        COUNTS.update({"ai": 0, "wecom": 0})
        STATE.update({"ai": "valid", "wecom": "ok"})
    return JSONResponse({"ok": True})


# ----------------------------------------------------------- OpenAI-compatible


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    body = await request.json()
    _record("ai", request, body)
    with LOCK:
        COUNTS["ai"] += 1
        call_no = COUNTS["ai"]
        mode = STATE["ai"]

    # 只认 POST，且必须带 Authorization —— 顺便验证 Provider 真的把 key 带上了
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return JSONResponse({"error": {"message": "missing api key"}}, status_code=401)

    if mode == "http_500_once" and call_no == 1:
        return JSONResponse({"error": {"message": "temporary upstream failure"}}, status_code=503)

    # 模拟「网关不支持 response_format」
    if mode == "no_response_format" and "response_format" in body:
        return JSONResponse(
            {"error": {"message": "Unrecognized request argument supplied: response_format"}}, status_code=400
        )

    if mode == "invalid_json":
        # 连着两次都返回一段人话，验证「修复重试后仍失败 → FAILED」
        text = "抱歉，我无法输出 JSON。看起来像是订单服务的问题。"
    elif mode == "empty":
        text = ""
    elif mode == "percent_confidence":
        text = json.dumps({**VALID_ANALYSIS, "confidence": 88}, ensure_ascii=False)
    else:
        # 故意包一层 markdown 代码块：真实模型十次里有三次这么干
        text = "```json\n" + json.dumps(VALID_ANALYSIS, ensure_ascii=False) + "\n```"

    return JSONResponse(
        {
            "id": f"chatcmpl-stub-{call_no}",
            "object": "chat.completion",
            "model": body.get("model", "stub-model"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        }
    )


# --------------------------------------------------------------- 企业微信


@app.post("/wecom/webhook")
async def wecom_webhook(request: Request) -> JSONResponse:
    body = await request.json()
    _record("wecom", request, body)
    with LOCK:
        COUNTS["wecom"] += 1
        call_no = COUNTS["wecom"]
        mode = STATE["wecom"]

    if mode == "http_500":
        return JSONResponse({"error": "server error"}, status_code=500)
    if mode == "retry_then_ok" and call_no == 1:
        # 45009 = 接口调用超过限制：值得重试
        return JSONResponse({"errcode": 45009, "errmsg": "api freq out of limit"})
    if mode == "fail":
        return JSONResponse({"errcode": -1, "errmsg": "system busy"})
    if mode == "fatal":
        return JSONResponse({"errcode": 93000, "errmsg": "invalid webhook url"})

    return JSONResponse({"errcode": 0, "errmsg": "ok", "msgid": f"stub-msg-{call_no}"})


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 stub：OpenAI-compatible + 企业微信 Webhook")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
