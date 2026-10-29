"""Playwright 执行器。

移植自 Lodestar（北极星）`backend/automation/playwright_executor.py`（425 行），保留其
成熟的证据采集能力（trace / before+after 截图 / console / network / DOM 快照 / 逐步日志）
与请求守卫机制，去掉桌面应用特有的暂停、断点、单步调试逻辑（属于 M2 执行中心能力）。

与 Lodestar 的差异：
- 入口签名改为 run(steps, environment, case_dir, variables, logger)，证据目录由调用方指定
- 状态词表对齐 TestFlow 方案 §17：PASSED / FAILED / SKIPPED / ERROR
- 日志与摘要统一走 redact()，密码类变量不会落盘
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..utils.redact import MASK, redact_text
from .action_spec import ActionSpec
from .security import ActionBlocked, assert_environment_ready, assert_step_risk_allowed, validate_request_target

STEP_PASSED = "PASSED"
STEP_FAILED = "FAILED"
STEP_SKIPPED = "SKIPPED"
STEP_ERROR = "ERROR"
# M2 扩展：停止执行时，正在执行的步骤需要一个明确终止状态
STEP_CANCELLED = "CANCELLED"

OUTCOME_PASSED = "PASSED"
OUTCOME_FAILED = "FAILED"
OUTCOME_ERROR = "ERROR"
OUTCOME_CANCELLED = "CANCELLED"

# 可中断等待的切片长度：越小停止越灵敏，越大对页面压力越小
STOP_SLICE_MS = 200


class StoppedByUser(RuntimeError):
    """执行被用户停止。只在可以安全中断的时点抛出（目前是 wait 动作）。"""

_SECURITY_VIOLATION_MARKERS = (
    "已阻止",
    "生产环境",
    "白名单",
    "只读模式",
    "支付网关",
    "门禁",
)


class AssertionFailure(AssertionError):
    """断言失败，并携带 actual。

    失败时 actual 恰恰是最该被看到的信息（方案 §9 要求每步展示 Expected / Actual）。
    如果直接抛普通 AssertionError，实际值会随着异常一起丢掉。
    """

    def __init__(self, message: str, actual: str = "") -> None:
        super().__init__(message)
        self.actual = actual


class PlaywrightExecutor:
    """无代码动作执行器。Playwright 延迟导入，保证服务在没有浏览器的机器上也能启动。"""

    def __init__(self, headless: bool = True, event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.headless = headless
        self.event = event or (lambda _name, _payload: None)
        self._control: Any = None

    # ------------------------------------------------------------------ 停止判断

    def _stopped(self) -> bool:
        control = self._control
        return bool(control is not None and control.should_stop())

    def run(
        self,
        steps: list[ActionSpec],
        environment: dict[str, Any],
        case_dir: Path,
        variables: dict[str, Any] | None = None,
        secrets: list[str] | None = None,
        control: Any = None,
        record_video: bool = False,
        record_har: bool = False,
    ) -> dict[str, Any]:
        """执行一组步骤。

        返回 {"outcome", "steps", "trace_path", "video_path", "har_path", "error_message"}。
        outcome 取值：PASSED / FAILED / ERROR / CANCELLED。

        case_dir 由调用方指定（每次 attempt 一个独立目录），保证重试的证据互不覆盖。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
            raise RuntimeError("尚未安装 Playwright，请先执行 pip install playwright 与 playwright install chromium") from exc

        self._control = control
        secrets = [item for item in (secrets or []) if item]
        variables = dict(variables or {})

        try:
            assert_environment_ready(environment)
        except ActionBlocked as exc:
            return {
                "outcome": OUTCOME_ERROR,
                "steps": [self._blocked_result(step, number, str(exc)) for number, step in enumerate(steps, 1)],
                "trace_path": "",
                "video_path": "",
                "har_path": "",
                "error_message": str(exc),
                # blocked=True 表示环境本身被门禁拦下，重试没有意义
                "blocked": True,
            }

        screenshots_dir = case_dir / "screenshots"
        logs_dir = case_dir / "logs"
        dom_dir = case_dir / "dom"
        trace_dir = case_dir / "trace"
        video_dir = case_dir / "video"
        network_dir = case_dir / "network"
        for directory in (screenshots_dir, logs_dir, dom_dir, trace_dir, video_dir, network_dir):
            directory.mkdir(parents=True, exist_ok=True)

        console_messages: list[str] = []
        network_messages: list[str] = []
        # M3：结构化现场信号。字符串日志是给人看的，这两个是给 Fingerprint 用的 ——
        # 「哪个接口返回了什么状态」「页面抛了什么脚本异常」必须能结构化取到，
        # 否则归因只能靠对 error 文本做正则猜。
        js_errors: list[str] = []
        http_errors: list[dict[str, Any]] = []
        security_blocks: list[dict[str, str]] = []
        results: list[dict[str, Any]] = []
        outcome = OUTCOME_PASSED
        error_message = ""
        trace_path = trace_dir / "trace.zip"
        har_path: Path | None = network_dir / "network.har" if record_har else None
        video_path = Path("")

        with sync_playwright() as playwright:
            browser_name = str(environment.get("browser") or "chromium").lower()
            browser_type = getattr(playwright, browser_name, playwright.chromium)
            browser = browser_type.launch(headless=bool(environment.get("headless", self.headless)))

            # Video 与 HAR 都是 context 级录制，且只在 context/page 关闭时才写盘。
            context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
            if record_video:
                context_kwargs["record_video_dir"] = str(video_dir)
                context_kwargs["record_video_size"] = {"width": 1280, "height": 720}
            if har_path is not None:
                context_kwargs["record_har_path"] = str(har_path)
                context_kwargs["record_har_mode"] = "full"
                # 不记录响应体：HAR 的价值在 URL/状态码/耗时/头信息，
                # 存 body 会让文件膨胀到几十 MB，也会把敏感响应内容落盘。
                context_kwargs["record_har_omit_content"] = True

            context = browser.new_context(**context_kwargs)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)

            def guard_request(route: Any) -> None:
                finding = validate_request_target(route.request.url, environment)
                if not finding:
                    route.continue_()
                    return
                blocked = {**finding, "url": route.request.url, "method": route.request.method}
                security_blocks.append(blocked)
                line = f"[BLOCKED] {route.request.method} {route.request.url} -> {finding['code']}"
                network_messages.append(line)
                self.event("security_request_blocked", blocked)
                route.abort("blockedbyclient")

            context.route("**/*", guard_request)
            page = context.new_page()
            # 在 page 关闭之后才能拿到视频文件，所以这里先留引用
            video = page.video if record_video else None
            def on_console(message: Any) -> None:
                console_messages.append(f"{message.type}: {message.text}")

            def on_pageerror(error: Any) -> None:
                text = str(error)
                console_messages.append(f"pageerror: {text}")
                # 未捕获的页面脚本异常：M3 用它区分「前端崩了」和「定位器失效」
                js_errors.append(text)

            def on_requestfailed(request: Any) -> None:
                failure = str(request.failure or "")
                network_messages.append(f"{request.method} {request.url} -> {failure}")
                # net::ERR_* 是环境/网络层的确定性证据（DNS、连接被拒、超时）。
                # ERR_ABORTED 要排除：它绝大多数是「页面跳转取消了上一个请求」，
                # 属于正常流程，当成环境故障会把归因带偏。
                if "net::ERR" in failure and "ERR_ABORTED" not in failure:
                    http_errors.append(
                        {
                            "status": 0,
                            "method": str(request.method or "").upper(),
                            "url": str(request.url or ""),
                            "failure": failure,
                        }
                    )

            def on_response(response: Any) -> None:
                if response.status < 400:
                    return
                network_messages.append(f"{response.status} {response.request.method} {response.url}")
                http_errors.append(
                    {
                        "status": int(response.status),
                        "method": str(response.request.method or "").upper(),
                        "url": str(response.url or ""),
                        "failure": "",
                    }
                )

            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("requestfailed", on_requestfailed)
            page.on("response", on_response)

            try:
                for number, step in enumerate(steps, 1):
                    if step.disabled:
                        results.append(self._result(step, number, STEP_SKIPPED, message="步骤已禁用"))
                        continue

                    # 停止检查点 ①：步骤边界。这里是立即生效的
                    if self._stopped():
                        results.append(self._result(step, number, STEP_CANCELLED, message="执行被用户停止"))
                        self._skip_from(results, steps, number + 1, "执行被用户停止")
                        outcome, error_message = OUTCOME_CANCELLED, "执行被用户停止"
                        self.event("run_cancelled", {"step_no": number})
                        break

                    self.event("step_started", {"step_no": number, "name": step.name, "action": step.action})
                    results.append(
                        self._run_step(
                            number, step, page, variables, environment, secrets,
                            screenshots_dir, logs_dir, dom_dir,
                            console_messages, network_messages, security_blocks,
                            js_errors, http_errors,
                        )
                    )
                    last = results[-1]

                    # 停止检查点 ②：步骤内部被中断（目前是 wait 动作的切片）
                    if last["status"] == STEP_CANCELLED:
                        self._skip_from(results, steps, number + 1, "执行被用户停止")
                        outcome, error_message = OUTCOME_CANCELLED, last.get("error") or "执行被用户停止"
                        self.event("run_cancelled", {"step_no": number})
                        break

                    if last["status"] in {STEP_FAILED, STEP_ERROR}:
                        outcome = OUTCOME_ERROR if last["status"] == STEP_ERROR else OUTCOME_FAILED
                        error_message = last.get("error") or ""
                        if not step.continue_on_failure:
                            # 失败后续步骤标记为 SKIPPED，保证步骤数与用例定义一致
                            self._skip_from(results, steps, number + 1, "前置步骤失败，未执行")
                            break

                # 空日志不落盘：既省 IO，也避免证据里出现一堆 0 字节文件
                if console_messages:
                    (logs_dir / "console.log").write_text(redact_text("\n".join(console_messages), secrets), encoding="utf-8")
                if network_messages:
                    (logs_dir / "network.log").write_text(redact_text("\n".join(network_messages), secrets), encoding="utf-8")
                if security_blocks:
                    (logs_dir / "security.log").write_text(
                        redact_text(json.dumps(security_blocks, ensure_ascii=False, indent=2), secrets), encoding="utf-8"
                    )
                context.tracing.stop(path=str(trace_path))
            finally:
                # 顺序很重要：video 与 HAR 都是在 page/context 关闭时才写盘。
                # 必须先 close 再取文件，否则拿到的路径指向一个还没被 flush 的文件。
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                if video is not None:
                    video_path = self._save_video(video, video_dir)
                try:
                    browser.close()
                except Exception:  # noqa: BLE001 - 关闭失败不应影响结果上报
                    pass

        if not trace_path.exists():
            trace_path = Path("")
        if not video_path or not Path(str(video_path)).exists() or Path(str(video_path)).stat().st_size == 0:
            video_path = Path("")
        if har_path is not None and (not har_path.exists() or har_path.stat().st_size == 0):
            har_path = None

        return {
            "outcome": outcome,
            "steps": results,
            "trace_path": str(trace_path) if str(trace_path) else "",
            "video_path": str(video_path) if str(video_path) else "",
            "har_path": str(har_path) if har_path is not None else "",
            "error_message": error_message,
            "blocked": False,
        }

    @staticmethod
    def _skip_from(results: list[dict[str, Any]], steps: list[ActionSpec], start_number: int, reason: str) -> None:
        for offset, remaining in enumerate(steps[start_number - 1 :], start_number):
            results.append(PlaywrightExecutor._result(remaining, offset, STEP_SKIPPED, message=reason))

    @staticmethod
    def _save_video(video: Any, video_dir: Path, timeout: float = 6.0) -> Path:
        """拿到录屏文件路径。只能在 page/context 关闭之后调用。

        两个坑都在这里踩过：
        1. **必须等文件真正落盘**。`video.path()` 在关闭后可能立刻返回一个还不存在的路径，
           直接判断 exists() 会误判成「没有视频」，所以要轮询等待。
        2. **不能做删除**。最初用 `save_as()` 另存后 unlink 原始文件，触发了运行环境的
           删除安全策略，抛 SystemExit 把整次执行打断。改成原子重命名（os.replace）后
           既拿到固定文件名，又不产生任何删除动作。

        失败时退回 Playwright 的原始路径；再失败就当作没有视频
        （不能因为录屏缺失让整个用例失败）。
        """
        target = video_dir / "recording.webm"
        source: Path | None = None
        deadline = time.monotonic() + max(0.5, float(timeout))
        while time.monotonic() < deadline:
            raw = None
            try:
                raw = video.path()
            except Exception:  # noqa: BLE001
                raw = None
            if raw:
                candidate = Path(raw)
                try:
                    if candidate.exists() and candidate.stat().st_size > 0:
                        source = candidate
                        break
                except OSError:
                    pass
            time.sleep(0.1)

        if source is None:
            return Path("")
        try:
            if source.resolve() != target.resolve():
                source.replace(target)  # os.replace：原子移动，不是删除
            if target.exists() and target.stat().st_size > 0:
                return target
        except Exception:  # noqa: BLE001 - 重命名失败就沿用原名
            pass
        return source

    def _run_step(
        self,
        number: int,
        step: ActionSpec,
        page: Any,
        variables: dict[str, Any],
        environment: dict[str, Any],
        secrets: list[str],
        screenshots_dir: Path,
        logs_dir: Path,
        dom_dir: Path,
        console_messages: list[str],
        network_messages: list[str],
        security_blocks: list[dict[str, str]],
        js_errors: list[str],
        http_errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        last_error = ""
        result: dict[str, Any] = {}
        before_screenshot = ""
        # 本步骤的时间窗起点。归因信号必须只取「这一步期间发生的事」，
        # 否则前一步的 500 会赖到后面所有步骤头上。
        console_start = len(console_messages)
        network_start = len(network_messages)
        js_start = len(js_errors)
        http_start = len(http_errors)

        for attempt in range(step.retries + 1):
            # 重试期间用户点了停止，不应再开新一轮
            if self._stopped():
                result = self._result(step, number, STEP_CANCELLED, message="执行被用户停止", started=started, error="执行被用户停止")
                break
            security_block_count = len(security_blocks)
            try:
                if not before_screenshot:
                    before_screenshot = self._shoot(page, screenshots_dir / f"{number:02d}_{step.action}_before.png")
                assert_step_risk_allowed(step.risk, environment)
                if page.url and page.url != "about:blank":
                    self._validate_page_url(page.url, environment)
                message, actual = self._execute(page, step, variables, environment)
                page.wait_for_timeout(80)
                if len(security_blocks) > security_block_count:
                    raise ActionBlocked(security_blocks[-1]["message"])
                after_screenshot = self._shoot(page, screenshots_dir / f"{number:02d}_{step.action}_after.png")
                result = self._result(
                    step,
                    number,
                    STEP_PASSED,
                    message=message,
                    actual=actual,
                    screenshot=after_screenshot if step.screenshot else "",
                    started=started,
                )
                result.update(
                    {
                        "before_screenshot_path": before_screenshot,
                        "after_screenshot_path": after_screenshot,
                        "attempt": attempt + 1,
                        "page_url": page.url,
                        "page_title": self._safe_title(page),
                        "security_findings": security_blocks[security_block_count:],
                    }
                )
                last_error = ""
                break
            except StoppedByUser as exc:
                # 用户点了停止，且当前正处在可中断的操作里（wait 的切片）
                last_error = str(exc) or "执行被用户停止"
                stopped_shot = self._shoot(page, screenshots_dir / f"{number:02d}_{step.action}_stopped.png")
                result = self._result(
                    step, number, STEP_CANCELLED, message="执行被用户停止", started=started, error=last_error
                )
                result.update(
                    {
                        "before_screenshot_path": before_screenshot,
                        "after_screenshot_path": stopped_shot,
                        "screenshot_path": stopped_shot,
                        "attempt": attempt + 1,
                        "page_url": page.url,
                        "page_title": self._safe_title(page),
                        "security_findings": security_blocks[security_block_count:],
                    }
                )
                break
            except ActionBlocked as exc:
                last_error = str(exc)
                blocked_shot = self._shoot(page, screenshots_dir / f"{number:02d}_{step.action}_blocked.png")
                result = self._result(step, number, STEP_ERROR, message="执行被安全策略阻止", started=started, error=last_error)
                result.update(
                    {
                        "before_screenshot_path": before_screenshot,
                        "after_screenshot_path": blocked_shot,
                        "screenshot_path": blocked_shot,
                        "attempt": attempt + 1,
                        "page_url": page.url,
                        "page_title": self._safe_title(page),
                        "security_findings": security_blocks[security_block_count:],
                    }
                )
                break
            except Exception as exc:  # noqa: BLE001 - 任何步骤异常都要落证据
                # 断言失败时保留 actual，否则执行详情里「实际值」会是空的
                captured_actual = str(getattr(exc, "actual", "") or "")
                prefix = "AssertionError" if isinstance(exc, AssertionError) else type(exc).__name__
                last_error = f"{prefix}: {exc}"
                if attempt < step.retries:
                    self.event("step_retry", {"step_no": number, "attempt": attempt + 1, "error": last_error})
                    continue
                failed_shot = self._shoot(page, screenshots_dir / f"{number:02d}_{step.action}_failed.png")
                result = self._result(
                    step, number, STEP_FAILED, message="步骤执行失败", actual=captured_actual, started=started, error=last_error
                )
                result.update(
                    {
                        "before_screenshot_path": before_screenshot,
                        "after_screenshot_path": failed_shot,
                        "screenshot_path": failed_shot,
                        "attempt": attempt + 1,
                        "page_url": page.url,
                        "page_title": self._safe_title(page),
                        "security_findings": security_blocks[security_block_count:],
                    }
                )
                break

        if result.get("status") in {STEP_FAILED, STEP_ERROR}:
            dom_path = dom_dir / f"{number:02d}_{step.action}.html"
            try:
                dom_path.write_text(page.content(), encoding="utf-8")
                result["dom_path"] = str(dom_path)
            except Exception:  # noqa: BLE001
                result["dom_path"] = ""

        log_path = logs_dir / f"{number:02d}_{step.action}.log"
        log_payload = {
            "step_no": number,
            "name": step.name,
            "action": step.action,
            "status": result.get("status"),
            "expected": step.expected,
            "actual": result.get("actual", ""),
            "message": result.get("message", ""),
            "error": result.get("error", ""),
            "page_url": result.get("page_url", ""),
            "page_title": result.get("page_title", ""),
            "attempt": result.get("attempt", 1),
        }
        log_path.write_text(redact_text(json.dumps(log_payload, ensure_ascii=False, indent=2), secrets), encoding="utf-8")
        result["log_path"] = str(log_path)
        result["console"] = "\n".join(console_messages[-20:])
        result["network"] = "\n".join(network_messages[-20:])

        # M3：只有失败步骤才落归因信号。PASS 不进失败分析，也就没必要存这些，
        # 否则一次全绿回归会白白多出几百行无用字段。
        if result.get("status") in {STEP_FAILED, STEP_ERROR, STEP_CANCELLED}:
            self._attach_failure_signals(
                result,
                secrets,
                console_messages[console_start:],
                network_messages[network_start:],
                js_errors[js_start:],
                http_errors[http_start:],
                http_fallback=http_errors,
                js_fallback=js_errors,
            )

        self.event("step_finished", result)
        return result

    @staticmethod
    def _attach_failure_signals(
        result: dict[str, Any],
        secrets: list[str],
        console_slice: list[str],
        network_slice: list[str],
        js_slice: list[str],
        http_slice: list[dict[str, Any]],
        *,
        http_fallback: list[dict[str, Any]] | None = None,
        js_fallback: list[str] | None = None,
    ) -> None:
        """把现场信号挂到结果上，供 Failure Fingerprint 使用。

        取「本步骤窗口」优先，窗口内没有就回退到本次尝试内最近一次 —— 这不是偷懒，
        而是因为真实失败常常是「上一步的动作引发、这一步才观察到」：

            open /orders      → 服务端返回 500（这一步不算失败，页面照样打开了）
            assert_not_visible → 元素仍然可见 → 这一步才失败

        如果只看本步骤窗口，500 就丢了，归因会退化成「元素仍然可见」这种没有根因的说法。
        回退时依然取「最近一次」，并且同样走 5xx > 网络失败 > 4xx 的优先级，
        避免很早以前的一个无关请求把根因带偏。

        全部经过 redact —— 信号会进指纹表，绝不能带出 token / password。
        """
        result["console_summary"] = redact_text("\n".join(console_slice[-20:]), secrets)[:2000]
        result["network_summary"] = redact_text("\n".join(network_slice[-20:]), secrets)[:2000]

        js_window = js_slice or (js_fallback or [])
        result["js_error"] = redact_text("\n".join(js_window[-3:]), secrets)[:2000]

        last_http = PlaywrightExecutor._pick_http_signal(http_slice or (http_fallback or []))
        if last_http:
            result["http_status"] = last_http.get("status") or None
            result["request_method"] = str(last_http.get("method") or "")[:16]
            result["request_url"] = str(last_http.get("url") or "")[:1024]

    @staticmethod
    def _pick_http_signal(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        """从本步骤窗口内的 HTTP 异常里挑出「最能解释这次失败」的那一个。

        不能简单取最后一个。真实页面里 4xx 很常见而且往往无关（某个静态资源 404、
        被取消的图片请求），如果它们盖在真正的 5xx 上面，归因就会指向一个假原因。
        优先级：5xx > 网络层失败（net::ERR）> 最后一个 4xx。
        理由是「越接近根因的证据越优先」—— 服务端明确报错比一个资源 404 更能解释
        「这个步骤为什么失败」。
        """
        if not items:
            return None
        server_errors = [item for item in items if int(item.get("status") or 0) >= 500]
        if server_errors:
            return server_errors[-1]
        network_failures = [item for item in items if not item.get("status")]
        if network_failures:
            return network_failures[-1]
        return items[-1]

    # ------------------------------------------------------------------ actions

    def _execute(self, page: Any, step: ActionSpec, variables: dict[str, Any], environment: dict[str, Any]) -> tuple[str, str]:
        from .action_spec import resolve_variables

        value = resolve_variables(step.value, variables)
        expected = resolve_variables(step.expected, variables)
        locator = None
        if step.needs_locator:
            if not step.locator_value:
                raise RuntimeError(f"步骤未配置定位器：{step.name}")
            locator = self._locator(page, step.locator_type, step.locator_value)

        action = step.action
        if action == "open":
            self._validate_page_url(value, environment)
            page.goto(value, wait_until="domcontentloaded", timeout=step.timeout)
            return f"已打开 {page.url}", page.url

        if action == "click":
            locator.click(timeout=step.timeout)
            return "点击成功", ""
        if action == "fill":
            locator.fill(value, timeout=step.timeout)
            return f"已输入 {value}", value
        if action == "clear":
            locator.fill("", timeout=step.timeout)
            return "已清空", ""
        if action == "select":
            locator.select_option(label=value, timeout=step.timeout)
            return f"已选择 {value}", value
        if action == "select_value":
            locator.select_option(value=value, timeout=step.timeout)
            return f"已选择 {value}", value
        if action == "upload":
            locator.set_input_files(value, timeout=step.timeout)
            return f"已上传 {value}", value
        if action == "wait":
            return self._cancellable_wait(page, int(value or 500))
        if action == "wait_url":
            page.wait_for_url(f"**{value}**", timeout=step.timeout)
            return f"已跳转到 {page.url}", page.url
        if action == "screenshot":
            return "已截图", ""
        if action == "hover":
            locator.hover(timeout=step.timeout)
            return "悬停成功", ""
        if action == "press":
            locator.press(value, timeout=step.timeout)
            return f"已按键 {value}", ""

        if action == "assert_exists":
            try:
                locator.first.wait_for(state="attached", timeout=step.timeout)
            except Exception as exc:  # noqa: BLE001
                raise AssertionFailure(
                    f"元素在 {step.timeout}ms 内未出现（定位器 {step.locator_type}={step.locator_value}）",
                    actual=f"元素不存在（等待 {step.timeout}ms 超时）",
                ) from exc
            return f"元素存在，匹配 {locator.count()} 个", str(locator.count())
        if action == "assert_not_exists":
            count = locator.count()
            if count > 0:
                raise AssertionFailure(f"元素仍存在，期望不存在（匹配 {count} 个）", actual=f"匹配到 {count} 个元素")
            return "元素确实不存在", "0"
        if action == "assert_visible":
            try:
                locator.first.wait_for(state="visible", timeout=step.timeout)
            except Exception as exc:  # noqa: BLE001
                raise AssertionFailure(
                    f"元素在 {step.timeout}ms 内未变为可见（定位器 {step.locator_type}={step.locator_value}）",
                    actual=f"元素不可见或不存在（等待 {step.timeout}ms 超时）",
                ) from exc
            return "元素可见", "visible"
        if action == "assert_not_visible":
            if locator.first.is_visible():
                raise AssertionFailure("元素仍然可见，期望不可见", actual="visible")
            return "元素不可见", "hidden"
        if action == "assert_enabled":
            if not locator.first.is_enabled():
                raise AssertionFailure("元素当前不可用（disabled）", actual="disabled")
            return "元素可用", "enabled"
        if action == "assert_count":
            actual_count = locator.count()
            if not str(expected).strip().isdigit():
                raise AssertionFailure(f"元素数量断言需要数字期望值，当前为“{expected}”", actual=str(actual_count))
            want = int(str(expected).strip())
            if actual_count != want:
                raise AssertionFailure(f"元素数量不符：期望 {want}，实际 {actual_count}", actual=str(actual_count))
            return f"元素数量为 {actual_count}", str(actual_count)

        if action == "assert_text":
            actual = locator.first.inner_text(timeout=step.timeout)
            if expected not in actual:
                raise AssertionFailure(f"文本断言失败：期望包含“{expected}”，实际为“{actual[:200]}”", actual=actual[:500])
            return f"文本包含“{expected}”", actual[:500]
        if action == "assert_equals":
            actual = locator.first.inner_text(timeout=step.timeout)
            if actual.strip() != expected.strip():
                raise AssertionFailure(f"文本相等断言失败：期望“{expected}”，实际为“{actual[:200]}”", actual=actual[:500])
            return f"文本等于“{expected}”", actual[:500]
        if action == "assert_value":
            actual = locator.first.input_value(timeout=step.timeout)
            if expected not in actual:
                raise AssertionFailure(f"输入值断言失败：期望包含“{expected}”，实际为“{actual[:200]}”", actual=actual[:500])
            return f"输入值包含“{expected}”", actual[:500]
        if action == "assert_attribute":
            attribute = (value or "value").strip()
            actual = locator.first.get_attribute(attribute, timeout=step.timeout) or ""
            if expected not in actual:
                raise AssertionFailure(
                    f"属性“{attribute}”断言失败：期望包含“{expected}”，实际为“{actual[:200]}”", actual=actual[:500]
                )
            return f"属性“{attribute}”包含“{expected}”", actual[:500]
        if action == "assert_url":
            if expected not in page.url:
                raise AssertionFailure(f"页面地址断言失败：期望包含“{expected}”，实际为“{page.url}”", actual=page.url)
            return f"页面地址包含“{expected}”", page.url
        if action == "assert_title":
            title = self._safe_title(page)
            if expected not in title:
                raise AssertionFailure(f"页面标题断言失败：期望包含“{expected}”，实际为“{title}”", actual=title)
            return f"页面标题包含“{expected}”", title

        raise RuntimeError(f"暂不支持的动作：{action}")

    # ------------------------------------------------------------------ helpers

    def _cancellable_wait(self, page: Any, total_ms: int) -> tuple[str, str]:
        """把长等待切成小片，中间检查停止信号。

        这样「点击停止」在 wait 步骤上的响应延迟是 < STOP_SLICE_MS，
        而不是等整个 timeout 走完。
        """
        remaining = max(0, int(total_ms))
        while remaining > 0:
            if self._stopped():
                raise StoppedByUser(f"等待被用户中断（还剩 {remaining} ms）")
            slice_ms = min(STOP_SLICE_MS, remaining)
            page.wait_for_timeout(slice_ms)
            remaining -= slice_ms
        return f"已等待 {total_ms} ms", ""

    @staticmethod
    def _locator(page: Any, locator_type: str, locator_value: str) -> Any:
        kind = str(locator_type or "css").lower()
        value = str(locator_value or "")
        if kind == "testid":
            return page.get_by_test_id(value)
        if kind == "role":
            try:
                data = json.loads(value)
                return page.get_by_role(data.get("role", "button"), name=data.get("name"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return page.get_by_role(value)
        if kind == "label":
            return page.get_by_label(value)
        if kind == "placeholder":
            return page.get_by_placeholder(value)
        if kind == "text":
            return page.get_by_text(value)
        if kind == "name":
            return page.locator(f"[name={json.dumps(value)}]")
        if kind == "xpath":
            return page.locator(f"xpath={value}")
        return page.locator(value)

    @staticmethod
    def _validate_page_url(value: str, environment: dict[str, Any]) -> None:
        from urllib.parse import urlparse

        from .security import host_is_allowed, is_real_payment_url

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("打开页面需要完整的 http/https 地址")
        host = parsed.hostname or ""
        allowed = list(environment.get("allowed_hosts") or [])
        if allowed and not host_is_allowed(host, allowed):
            raise ActionBlocked(f"目标域名不在白名单中：{host}")
        if is_real_payment_url(value):
            raise ActionBlocked("检测到真实支付网关，核心门禁已阻止执行")
        if str(environment.get("environment_type") or "").upper() == "PROD":
            raise ActionBlocked("生产环境默认禁止执行，已阻止打开页面")

    @staticmethod
    def _safe_title(page: Any) -> str:
        try:
            return str(page.title())[:200]
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _shoot(page: Any, path: Path) -> str:
        try:
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001 - 截图失败不应中断执行
            return ""

    def _blocked_result(self, step: ActionSpec, number: int, error: str) -> dict[str, Any]:
        result = self._result(step, number, STEP_ERROR, message="执行被安全策略阻止", error=error)
        result["security_findings"] = [{"code": "ENVIRONMENT_BLOCKED", "severity": "block", "message": error}]
        return result

    @staticmethod
    def _result(
        step: ActionSpec,
        number: int,
        status: str,
        message: str = "",
        actual: str = "",
        screenshot: str = "",
        started: float | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        duration = int((time.perf_counter() - started) * 1000) if started is not None else 0
        now = datetime.now(timezone.utc).astimezone()
        return {
            "step_no": number,
            "name": step.name,
            "action": step.action,
            "action_label": step.label,
            "status": status,
            "expected": step.expected,
            "actual": actual,
            "message": message,
            "error": error,
            "duration_ms": duration,
            "started_at": (now - timedelta(milliseconds=duration)).isoformat(timespec="seconds"),
            "finished_at": now.isoformat(timespec="seconds"),
            "screenshot_path": screenshot,
            "before_screenshot_path": "",
            "after_screenshot_path": "",
            "log_path": "",
            "dom_path": "",
            "page_url": "",
            "page_title": "",
            "locator": {"type": step.locator_type, "value": step.locator_value} if step.locator_value else {},
            "attempt": 1,
            "console": "",
            "network": "",
            "security_findings": [],
            # M3 归因信号（仅失败步骤会被填充，见 _attach_failure_signals）
            "console_summary": "",
            "network_summary": "",
            "request_method": "",
            "request_url": "",
            "http_status": None,
            "js_error": "",
        }


__all__ = [
    "PlaywrightExecutor",
    "ActionBlocked",
    "AssertionFailure",
    "StoppedByUser",
    "STEP_PASSED",
    "STEP_FAILED",
    "STEP_SKIPPED",
    "STEP_ERROR",
    "STEP_CANCELLED",
    "OUTCOME_PASSED",
    "OUTCOME_FAILED",
    "OUTCOME_ERROR",
    "OUTCOME_CANCELLED",
    "STOP_SLICE_MS",
    "MASK",
]
