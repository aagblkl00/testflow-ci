"""TestFlow Demo Web App —— 被测目标系统。

为什么要单独跑一个进程：TestFlow 要验证的是「测试一个外部 Web 系统」这条链路，
把被测站和测试平台放在同一个进程里会让链路失真。所以这里是一个独立的最小电商站。

启动：
    uvicorn demo_app.main:app --host 127.0.0.1 --port 8100

页面：登录 / 首页 / 搜索 / 商品 / 订单
所有可交互元素都带 data-testid，方便演示「role > label > testid > css > xpath」的定位优先级。

故障注入（M3 会用它验证失败中心）：
    GET  /__faults          查看当前开关
    POST /__faults          形如 {"orders_500": true}，只影响被测行为，不影响 TestFlow 本身
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="TestFlow Demo Web App", version="0.1.0")

USERS = {"demo": "demo123", "qa": "qa123"}

PRODUCTS = [
    {"sku": "SKU-1001", "name": "机械键盘", "price": 399, "category": "数码", "stock": 12},
    {"sku": "SKU-1002", "name": "人体工学椅", "price": 1299, "category": "家具", "stock": 5},
    {"sku": "SKU-1003", "name": "降噪耳机", "price": 899, "category": "数码", "stock": 0},
    {"sku": "SKU-1004", "name": "升降桌", "price": 1599, "category": "家具", "stock": 7},
    {"sku": "SKU-1005", "name": "显示器支架", "price": 199, "category": "数码", "stock": 30},
]

SESSIONS: dict[str, str] = {}
ORDERS: list[dict[str, Any]] = []
_ORDER_SEQ = {"value": 1000}

# 故障开关：M3 失败中心与 Flaky 验证的前提
FAULTS: dict[str, Any] = {
    "login_500": False,          # 登录接口返回 500
    "search_500": False,         # 搜索接口返回 500
    "orders_500": False,         # 创建订单返回 500
    "orders_slow_ms": 0,         # 订单接口额外延迟，用来制造超时
    "missing_element": False,    # 首页问候语元素不渲染，用来制造自动化错误
    "flaky_once": False,         # 首次失败、第二次成功，用来制造 Flaky
    # M3 新增两个开关，对应方案 §49 要求验证的 JS Error 与 Missing Test Data。
    # 少了它们，只能靠人为把断言写错来凑失败，那验不出分类规则是不是真的对。
    "js_error": False,           # 首页抛未捕获脚本异常（同时导致问候语渲染不出来）
    "missing_data": False,       # 下单所需的前置测试数据缺失
}
_FLAKY_STATE = {"consumed": False}


def _current_user(request: Request) -> str | None:
    sid = request.cookies.get("demo_session", "")
    return SESSIONS.get(sid)


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    user = _current_user(request)
    active = [key for key, value in FAULTS.items() if value]
    return {"user": user, "faults": FAULTS, "active_faults": active, **extra}


@app.get("/__faults")
def read_faults() -> JSONResponse:
    return JSONResponse({"faults": FAULTS, "flaky_consumed": _FLAKY_STATE["consumed"]})


@app.post("/__faults")
async def set_faults(payload: dict[str, Any]) -> JSONResponse:
    for key, value in (payload or {}).items():
        if key in FAULTS:
            FAULTS[key] = value
    if payload.get("reset_flaky"):
        _FLAKY_STATE["consumed"] = False
    return JSONResponse({"faults": FAULTS})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/home", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", _ctx(request, error=""))


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    if FAULTS["orders_slow_ms"]:
        time.sleep(min(int(FAULTS["orders_slow_ms"]), 30000) / 1000)
    if FAULTS["login_500"]:
        return templates.TemplateResponse(request, 
            "login.html", _ctx(request, error="服务异常：登录接口返回 500（故障注入已开启）"), status_code=500
        )
    if USERS.get(username.strip()) != password:
        return templates.TemplateResponse(request, "login.html", _ctx(request, error="账号或密码错误"), status_code=200)
    session_id = uuid.uuid4().hex
    SESSIONS[session_id] = username.strip()
    response = RedirectResponse("/home", status_code=302)
    response.set_cookie("demo_session", session_id, httponly=True)
    return response


@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("demo_session", "")
    SESSIONS.pop(sid, None)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("demo_session")
    return response


@app.get("/home", response_class=HTMLResponse)
def home(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, 
        "home.html",
        _ctx(request, order_count=len(ORDERS), product_count=len(PRODUCTS)),
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if FAULTS["search_500"]:
        return templates.TemplateResponse(request, 
            "search.html",
            _ctx(request, q=q, results=[], error="服务异常：搜索接口返回 500（故障注入已开启）"),
            status_code=500,
        )
    keyword = q.strip()
    results = [item for item in PRODUCTS if keyword and (keyword in item["name"] or keyword.lower() in item["sku"].lower())]
    return templates.TemplateResponse(request, "search.html", _ctx(request, q=q, results=results, error=""))


@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "products.html", _ctx(request, items=PRODUCTS))


@app.get("/orders", response_class=HTMLResponse)
def orders(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "orders.html", _ctx(request, items=ORDERS, error=""))


@app.post("/orders", response_class=HTMLResponse)
def create_order(request: Request, sku: str = Form(""), quantity: int = Form(1)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if FAULTS["orders_slow_ms"]:
        time.sleep(min(int(FAULTS["orders_slow_ms"]), 30000) / 1000)

    # M3：前置测试数据缺失。放在最前面 —— 数据都没了就谈不上接口调用。
    if FAULTS["missing_data"]:
        return templates.TemplateResponse(
            request,
            "orders.html",
            _ctx(request, items=ORDERS, error="测试数据不存在：下单所需的商品 SKU 已被清理（故障注入已开启）"),
            status_code=400,
        )

    if FAULTS["flaky_once"] and not _FLAKY_STATE["consumed"]:
        _FLAKY_STATE["consumed"] = True
        return templates.TemplateResponse(request, 
            "orders.html", _ctx(request, items=ORDERS, error="服务异常：订单接口偶发失败（故障注入：首次失败）"), status_code=500
        )

    if FAULTS["orders_500"]:
        return templates.TemplateResponse(request, 
            "orders.html", _ctx(request, items=ORDERS, error="服务异常：订单接口返回 500（故障注入已开启）"), status_code=500
        )

    product = next((item for item in PRODUCTS if item["sku"] == sku.strip()), None)
    if product is None:
        return templates.TemplateResponse(request, "orders.html", _ctx(request, items=ORDERS, error=f"商品不存在：{sku}"), status_code=200)
    if int(quantity) < 1:
        return templates.TemplateResponse(request, "orders.html", _ctx(request, items=ORDERS, error="数量必须大于 0"), status_code=200)
    if int(quantity) > product["stock"]:
        return templates.TemplateResponse(request, 
            "orders.html", _ctx(request, items=ORDERS, error=f"库存不足：{product['name']} 仅剩 {product['stock']} 件"), status_code=200
        )

    _ORDER_SEQ["value"] += 1
    ORDERS.append(
        {
            "order_no": f"ORD{_ORDER_SEQ['value']}",
            "sku": product["sku"],
            "name": product["name"],
            "quantity": int(quantity),
            "amount": product["price"] * int(quantity),
            "status": "已创建",
            "created_by": user,
        }
    )
    return RedirectResponse("/orders", status_code=302)


@app.get("/api/products")
def api_products() -> JSONResponse:
    return JSONResponse({"code": 0, "msg": "success", "data": PRODUCTS})


@app.get("/api/orders")
def api_orders() -> JSONResponse:
    return JSONResponse({"code": 0, "msg": "success", "data": ORDERS})


@app.get("/api/orders/{order_no}")
def api_order(order_no: str) -> JSONResponse:
    if FAULTS["orders_500"]:
        return JSONResponse({"code": 500, "msg": "Internal Server Error", "data": None}, status_code=500)
    found = next((item for item in ORDERS if item["order_no"] == order_no), None)
    if found is None:
        return JSONResponse({"code": 404, "msg": "订单不存在", "data": None}, status_code=404)
    return JSONResponse({"code": 0, "msg": "success", "data": found})
