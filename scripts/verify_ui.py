"""M1 UI 验收脚本：用真实浏览器点一遍 TestFlow 自己的页面。

证明的是「页面能看到 PASS/FAIL」，而不是「API 返回了 JSON」。

前置：
    backend (8000) 已启动，且 frontend/dist 已构建（会被挂载到 /）

用法：
    .venv/Scripts/python.exe scripts/verify_ui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOT_DIR = ROOT / "storage" / "_ui_verify"

# 允许直接以脚本方式运行（不需要先 pip install -e .）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    import os

    from backend.app.config import settings  # noqa: PLC0415

    if settings.playwright_browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = settings.playwright_browsers_path

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, bool]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        # 1) 登录页
        page.goto("http://127.0.0.1:8000/#/login", wait_until="networkidle")
        page.wait_for_selector("text=TestFlow CI", timeout=15000)
        checks.append(("登录页渲染", page.locator("text=TestFlow CI").count() > 0))
        page.screenshot(path=str(SHOT_DIR / "01_login.png"), full_page=True)

        # 2) 登录
        page.fill("input#username", "admin")
        page.fill("input#password", "admin123")
        page.click("button[type=submit]")
        page.wait_for_selector("text=工作台", timeout=20000)
        checks.append(("登录成功并进入工作台", True))
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOT_DIR / "02_dashboard.png"), full_page=True)

        # 3) 执行中心
        page.click("text=执行中心")
        page.wait_for_selector("table", timeout=15000)
        page.wait_for_timeout(1200)
        row_count = page.locator("tbody tr").count()
        checks.append((f"执行中心列出 {row_count} 条执行记录", row_count > 0))
        page.screenshot(path=str(SHOT_DIR / "03_runs.png"), full_page=True)

        # 4) 执行详情：点第一条 Run ID
        page.locator("tbody tr").first.locator("a, span").first.click()
        page.wait_for_selector("text=用例执行结果", timeout=20000)
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        checks.append(("执行详情显示状态标签", "PASSED" in body))
        checks.append(("执行详情显示用例号", "TC-LOGIN-001" in body))
        page.screenshot(path=str(SHOT_DIR / "04_run_detail.png"), full_page=True)

        # 5) 展开用例，看步骤级 expected / actual
        page.locator(".ant-collapse-header").first.click()
        page.wait_for_timeout(900)
        detail = page.inner_text("body")
        checks.append(("步骤表已展开", "断言" in detail))
        shot_count = page.locator(".ant-image img").count()
        checks.append((f"步骤证据里有 {shot_count} 张截图缩略图", shot_count > 0))
        page.screenshot(path=str(SHOT_DIR / "05_run_detail_steps.png"), full_page=True)

        # 6) 测试用例页
        page.click("text=测试用例")
        page.wait_for_selector("table", timeout=15000)
        page.wait_for_timeout(1200)
        checks.append(("测试用例页有数据", page.locator("tbody tr").count() > 0))
        page.screenshot(path=str(SHOT_DIR / "06_cases.png"), full_page=True)

        # 7) 自动化流程页
        page.click("text=自动化流程")
        page.wait_for_selector("table", timeout=15000)
        page.wait_for_timeout(1200)
        checks.append(("自动化流程页有数据", page.locator("tbody tr").count() > 0))
        page.screenshot(path=str(SHOT_DIR / "07_flows.png"), full_page=True)

        # 8) 回归计划页
        page.click("text=回归计划")
        page.wait_for_selector("table", timeout=15000)
        page.wait_for_timeout(1200)
        checks.append(("回归计划页有数据", page.locator("tbody tr").count() > 0))
        page.screenshot(path=str(SHOT_DIR / "08_plans.png"), full_page=True)

        browser.close()

    print("=" * 66)
    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1
    print("=" * 66)
    if console_errors:
        print(f"  浏览器控制台报错 {len(console_errors)} 条：")
        for item in console_errors[:5]:
            print(f"    - {item[:160]}")
    else:
        print("  浏览器控制台无报错")
    print(f"  截图目录：{SHOT_DIR}")
    print(f"  结果：{len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
