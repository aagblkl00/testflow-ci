"""执行编排：把「回归计划 / 套件」变成一条真实跑完的执行记录。

M2 相对 M1 的变化：
- **Attempt 层级**：每次真实重新执行都是一个 CaseResultAttempt，证据目录 attempts/{n}/，
  绝不覆盖上一次的证据（这是 Flaky 判定能站得住脚的前提）；
- **真实 Retry**：按 plan.retry_count 重跑；FAIL → RETRY → PASS 最终记为 FLAKY，不伪装成 PASS；
- **真实 Stop**：通过 RunControl 通知执行器，正在执行的 Case 记 CANCELLED，
  未开始的 Case 记 SKIPPED，Run 最终 CANCELLED；
- **失败用例重跑**：新 Run 独立保存，source_run_id 指回原 Run，原 Run 一个字节都不改；
- **Video / HAR**：context 级录制，随 attempt 一起落盘。

职责边界：不负责执行模式（inline / celery，那是 workers 层）、不负责失败分析（M3）、不负责通知（M5）。

一次调用的完整产物：
    ExecutionRun → CaseResult → CaseResultAttempt → StepResult → Artifact
磁盘：
    storage/projects/{project_id}/runs/{run_id}/cases/{case_result_id}/attempts/{n}/{screenshots,trace,video,network,logs,dom}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import SessionLocal
from ..failure import retry_policy
from ..failure.service import analyze_run as analyze_run_failures
from ..failure.service import build_finding, step_payload
from ..models import (
    Artifact,
    AutomationFlow,
    CaseFlowBinding,
    CaseResult,
    CaseResultAttempt,
    Environment,
    ExecutionRun,
    Project,
    RegressionPlan,
    StepResult,
    TestCase,
    TestSuite,
    TestSuiteCase,
    utcnow,
)
from ..utils.redact import collect_secrets, redact_text
from .action_spec import build_variables, normalize_steps
from .control import register, unregister
from .playwright_executor import (
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    PlaywrightExecutor,
)

logger = logging.getLogger("testflow.execution")


def _case_has_flaky_history(db: Session, case_id: str) -> bool:
    """这个用例历史上是否出现过 Flaky。

    用于 RetryPolicy 的 FLAKY 分支：已经 Flaky 过的用例，重试一次最有价值。
    """
    row = db.execute(
        select(CaseResult.id).where(CaseResult.case_id == case_id, CaseResult.flaky.is_(True)).limit(1)
    ).scalars().first()
    return row is not None


def _clean(value: Any, secrets: list[str], limit: int) -> str:
    """落库前统一脱敏并截断。

    「库里本来就有明文」不是理由 —— 这些字段会出现在页面上、报告里、
    以及 M3 的失败指纹里。凭据只要有一条路径能出去，就等于没做保护。
    """
    return redact_text(str(value or ""), secrets)[:limit]


def _attempt_category(outcome: dict[str, Any], *, blocked: bool = False) -> str:
    """从一次失败的 attempt 结果里算出分类，仅用于决定重试策略。

    走的是和失败中心完全相同的 Fingerprint / Classifier 代码路径 ——
    如果这里自己写一套判断，重试策略和失败中心迟早会对同一个失败给出两个说法。
    """
    steps = outcome.get("steps") or []
    decisive = next((item for item in steps if str(item.get("status")) in {"FAILED", "ERROR"}), None)
    if decisive is None:
        return "UNKNOWN"
    finding = build_finding(step_payload(decisive))
    _ = blocked  # blocked 已由调用方直接短路，这里保持签名对称
    return finding.category

ARTIFACT_TYPE_BY_SUFFIX = {
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".zip": "trace",
    ".webm": "video",
    ".mp4": "video",
    ".har": "har",
    ".html": "dom",
    ".log": "log",
}


def _artifact_type(path: Path) -> str:
    name = path.name.lower()
    if name == "console.log":
        return "console"
    if name == "network.log":
        return "network"
    if name == "security.log":
        return "log"
    return ARTIFACT_TYPE_BY_SUFFIX.get(path.suffix.lower(), "log")


def _record_artifact(
    db: Session,
    run_id: str,
    case_result_id: str | None,
    step_result_id: str | None,
    attempt_id: str | None,
    path: str,
) -> None:
    if not path:
        return
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return
    try:
        size = file_path.stat().st_size
    except OSError:
        size = 0
    db.add(
        Artifact(
            run_id=run_id,
            case_result_id=case_result_id,
            step_result_id=step_result_id,
            attempt_id=attempt_id,
            type=_artifact_type(file_path),
            path=str(file_path),
            size=size,
        )
    )


# ---------------------------------------------------------------- 加载与解析


def _load_run_context(db: Session, run_id: str) -> tuple[ExecutionRun, Project, Environment | None, RegressionPlan | None]:
    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise LookupError(f"执行记录不存在：{run_id}")
    project = db.get(Project, run.project_id)
    if project is None:
        raise LookupError(f"项目不存在：{run.project_id}")
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    return run, project, environment, plan


def _suite_case_ids(db: Session, suite_id: str) -> list[str]:
    """套件内启用的用例，按 sort_order 排序。"""
    rows = db.execute(
        select(TestSuiteCase)
        .where(TestSuiteCase.suite_id == suite_id, TestSuiteCase.enabled.is_(True))
        .order_by(TestSuiteCase.sort_order, TestSuiteCase.id)
    ).scalars().all()
    return [row.case_id for row in rows]


def resolve_case_ids(db: Session, run: ExecutionRun, plan: RegressionPlan | None) -> list[str]:
    """决定这次执行跑哪些用例。优先级从高到低：

    1. run.case_ids —— 失败用例重跑时显式指定（M2）
    2. plan.suite_id —— 套件（M2 起的主路径）
    3. plan.case_ids —— M1 的 JSON 列表，保持兼容
    4. 项目下所有启用绑定的用例 —— 兜底（单用例试跑场景）
    """
    if isinstance(run.case_ids, list) and run.case_ids:
        return [str(item) for item in run.case_ids]
    if plan is not None and plan.suite_id:
        ids = _suite_case_ids(db, plan.suite_id)
        if ids:
            return ids
    if plan is not None and isinstance(plan.case_ids, list) and plan.case_ids:
        return [str(item) for item in plan.case_ids]
    # M4：兜底也必须限定在本项目内。原来这里查的是全局的 CaseFlowBinding，
    # 计划既没套件也没 case_ids 时会捞到别的项目的用例 —— 那不是「兜底」，
    # 是跨项目串执行。现有验收用例都走套件/case_ids 路径，不经过这里。
    rows = db.execute(
        select(CaseFlowBinding.case_id)
        .join(TestCase, TestCase.id == CaseFlowBinding.case_id)
        .where(CaseFlowBinding.enabled.is_(True), TestCase.project_id == run.project_id)
    ).scalars().all()
    return [str(item) for item in rows]


def _environment_payload(environment: Environment | None, browser: str, base_url_override: str = "") -> dict[str, Any]:
    """把环境配置摊平成执行器要的形状。

    `base_url_override` 是 M4 给 CLI 的 --base-url 留的口子，只影响这一次执行。
    注意它**不是**安全后门：下面的 allowed_hosts 照原样带过去，执行器的四道门禁
    会对覆盖后的地址重新做一次完整校验，白名单外的地址照样被拦。
    """
    if environment is None:
        return {}
    return {
        "id": environment.id,
        "name": environment.name,
        "base_url": (base_url_override or environment.base_url or ""),
        "variables": dict(environment.variables or {}),
        "browser": environment.browser or browser or "chromium",
        "environment_type": environment.environment_type or "TEST",
        "allowed_hosts": list(environment.allowed_hosts or []),
        "read_only": bool(environment.read_only),
        "headless": settings.playwright_headless,
    }


def _final_run_status(total: int, passed: int, failed: int, errored: int, skipped: int, flaky: int, cancelled: bool) -> str:
    if total == 0:
        return "ERROR"
    if cancelled:
        return "CANCELLED"
    effective_pass = passed + flaky
    if failed == 0 and errored == 0:
        return "PASSED" if effective_pass > 0 else "CANCELLED"
    if effective_pass == 0 and skipped == 0:
        return "ERROR" if failed == 0 else "FAILED"
    if effective_pass == 0:
        return "FAILED"
    return "PARTIAL"


# ---------------------------------------------------------------- 停止标志


def _db_cancel_requested(run_id: str) -> bool:
    """跨进程停止信号：Celery 模式下执行器靠轮询这个值感知停止。"""
    db = SessionLocal()
    try:
        row = db.execute(select(ExecutionRun.cancel_requested).where(ExecutionRun.id == run_id)).first()
        return bool(row and row[0])
    except Exception:  # noqa: BLE001 - 读不到就当没请求停止，不能反过来中断执行
        return False
    finally:
        db.close()


def request_cancel(db: Session, run_id: str, actor: str = "") -> ExecutionRun:
    """请求停止执行。

    三个动作缺一不可，否则就会出现「数据库说停了、浏览器还在跑」：
    1. 置位进程内 Event（inline 模式立刻生效）；
    2. 写库 cancel_requested（Celery 模式靠它跨进程）；
    3. 把状态推进到 CANCELLING，让页面能显示"停止中"。
    """
    from .control import request_stop  # noqa: PLC0415 - 避免与 workers 层形成导入环

    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise LookupError(f"执行记录不存在：{run_id}")
    if run.status not in {"QUEUED", "RUNNING", "CANCELLING"}:
        raise ValueError(f"当前状态（{run.status}）不可停止")
    run.cancel_requested = True
    if run.status != "CANCELLING":
        run.status = "CANCELLING"
    db.commit()
    in_process = request_stop(run_id, actor=actor)
    logger.info("请求停止 run=%s in_process=%s actor=%s", run_id, in_process, actor)
    return run


# ---------------------------------------------------------------- 主流程


def execute_run_sync(run_id: str) -> dict[str, Any]:
    """同步执行一次回归。inline 与 celery 两种模式共用这一个入口，保证结果一致。"""
    db = SessionLocal()
    control = register(run_id)
    control.bind_db_checker(lambda: _db_cancel_requested(run_id))
    try:
        run, project, environment, plan = _load_run_context(db, run_id)

        # 重跑场景：清掉可能从上一个 Run 继承来的停止标志，否则新 Run 会立刻自杀
        if run.cancel_requested and run.status in {"QUEUED"}:
            run.cancel_requested = False
        run.status = "RUNNING"
        run.started_at = utcnow()
        db.commit()

        plan_browser = plan.browser if plan else ""
        environment_payload = _environment_payload(
            environment, run.environment_id and plan_browser or "", run.base_url_override or ""
        )
        if not environment_payload:
            run.status = "ERROR"
            run.error_message = "执行计划未绑定环境，无法启动浏览器"
            run.finished_at = utcnow()
            db.commit()
            return {"run_id": run.id, "status": run.status, "error": run.error_message}

        case_ids = resolve_case_ids(db, run, plan)
        run.total_cases = len(case_ids)
        db.commit()

        run_root = settings.runs_dir / project.id / "runs" / run.id
        run_root.mkdir(parents=True, exist_ok=True)
        run_log_lines: list[str] = []
        secrets = collect_secrets(environment_payload.get("variables") or {})

        case_lookup: dict[str, TestCase] = {}
        if case_ids:
            found = db.execute(select(TestCase).where(TestCase.id.in_(case_ids))).scalars().all()
            case_lookup = {item.id: item for item in found}

        executor = PlaywrightExecutor(headless=settings.playwright_headless)
        counters = {"passed": 0, "failed": 0, "skipped": 0, "errored": 0, "flaky": 0, "cancelled": 0}
        cancelled = False

        for index, case_id in enumerate(case_ids, 1):
            case = case_lookup.get(case_id)

            # 停止检查点：尚未开始的用例一律记 SKIPPED，且不再新建 attempt
            if control.should_stop():
                cancelled = True
                remaining = case_ids[index - 1 :]
                for offset, remaining_id in enumerate(remaining, index):
                    pending_case = case_lookup.get(remaining_id)
                    db.add(
                        CaseResult(
                            run_id=run.id,
                            case_id=remaining_id,
                            case_code=(pending_case.case_code if pending_case else "") or "",
                            case_name=(pending_case.name if pending_case else f"未知用例 {remaining_id}") or "",
                            status="SKIPPED",
                            error_message="执行被用户停止",
                            started_at=utcnow(),
                            finished_at=utcnow(),
                        )
                    )
                    counters["skipped"] += 1
                    run_log_lines.append(f"[{offset}/{len(case_ids)}] SKIPPED 执行被用户停止")
                db.commit()
                break

            result = _execute_case(
                db=db,
                run=run,
                case=case,
                case_id=case_id,
                environment_payload=environment_payload,
                run_root=run_root,
                secrets=secrets,
                control=control,
                plan=plan,
                plan_retry_count=max(0, int(plan.retry_count or 0)) if plan else 0,
                auto_retry=bool(plan.fail_retry) if plan else False,
            )
            counters[result["bucket"]] += 1
            if result["bucket"] == "cancelled":
                cancelled = True
            run_log_lines.append(
                f"[{index}/{len(case_ids)}] {result['status']:9s} {result['case_name']}"
                f" ({result['duration_ms']} ms, {result['attempts']} 次尝试)"
                + (f" - {result['error_message']}" if result["error_message"] else "")
            )
            if cancelled:
                # 当前用例被停止，剩余用例同样记 SKIPPED
                remaining = case_ids[index:]
                for offset, remaining_id in enumerate(remaining, index + 1):
                    pending_case = case_lookup.get(remaining_id)
                    db.add(
                        CaseResult(
                            run_id=run.id,
                            case_id=remaining_id,
                            case_code=(pending_case.case_code if pending_case else "") or "",
                            case_name=(pending_case.name if pending_case else f"未知用例 {remaining_id}") or "",
                            status="SKIPPED",
                            error_message="执行被用户停止",
                            started_at=utcnow(),
                            finished_at=utcnow(),
                        )
                    )
                    counters["skipped"] += 1
                    run_log_lines.append(f"[{offset}/{len(case_ids)}] SKIPPED 执行被用户停止")
                db.commit()
                break

        # 运行级证据补充登记（trace / video / har / console / network 已随 attempt 记录，
        # 这里只兜底那些没有 case_result_id 归属的文件）
        run.passed_cases = counters["passed"]
        run.failed_cases = counters["failed"]
        run.skipped_cases = counters["skipped"]
        run.error_cases = counters["errored"]
        run.flaky_cases = counters["flaky"]
        run.cancelled_cases = counters["cancelled"]
        run.status = _final_run_status(
            run.total_cases, counters["passed"], counters["failed"], counters["errored"], counters["skipped"], counters["flaky"], cancelled
        )
        run.finished_at = utcnow()
        if run.started_at:
            run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
        if run.status in {"FAILED", "ERROR"}:
            failing = db.execute(
                select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.status.in_(["FAILED", "ERROR"]))
            ).scalars().first()
            run.error_message = (failing.error_message if failing else "")[:2000]
        elif run.status == "CANCELLED":
            run.cancelled_at = run.finished_at
            run.error_message = "执行被用户停止"

        (run_root / "run.log").write_text(redact_text("\n".join(run_log_lines), secrets), encoding="utf-8")
        db.commit()

        # ---- M3：失败分析在 Run 落定之后独立进行
        # 全绿执行在这里直接返回，不产生任何 Fingerprint / 分类成本（方案 §45、§54）。
        # 分析失败绝不能影响执行结果本身，所以整段包在 try 里。
        analysis: dict[str, Any] = {"analyzed": False, "reason": "not_run"}
        try:
            analysis = analyze_run_failures(db, run.id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("失败分析出错 run_id=%s", run_id)
            analysis = {"analyzed": False, "reason": f"{type(exc).__name__}: {exc}"}

        # ---- M5：告警评估 + AI 分析 + 企业微信通知
        # 这三件事在**独立后台线程**里做，不串在返回路径上：
        # AI 是网络调用、企业微信还可能重试，串起来会让 CLI 的 --wait 白等几十秒，
        # 而那段时间跟「测试跑得慢」是两码事。
        # 整段包在 try 里，且调用的函数自己也不抛异常 —— AI 或通知挂掉，
        # 这一行的 run 结果、用例结果、失败分组一个字段都不会变。
        try:
            from ..alerts.service import on_run_completed_async  # noqa: PLC0415 - 懒加载，避免与 alerts 形成循环导入

            on_run_completed_async(run.id)
        except Exception:  # noqa: BLE001
            logger.exception("M5 后置流程派发失败 run_id=%s", run_id)

        return {
            "run_id": run.id,
            "status": run.status,
            "total": run.total_cases,
            "passed": run.passed_cases,
            "failed": run.failed_cases,
            "skipped": run.skipped_cases,
            "error_cases": run.error_cases,
            "flaky": run.flaky_cases,
            "cancelled": run.cancelled_cases,
            "duration_ms": run.duration_ms,
            "analysis": analysis,
        }
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须落回数据库，不能悬在 RUNNING
        db.rollback()
        run = db.get(ExecutionRun, run_id)
        if run is not None and run.status in {"QUEUED", "RUNNING", "CANCELLING"}:
            run.status = "CANCELLED" if run.cancel_requested else "ERROR"
            run.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            run.finished_at = utcnow()
            if run.started_at:
                run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
            db.commit()
        logger.exception("执行失败 run_id=%s", run_id)
        return {"run_id": run_id, "status": "ERROR", "error": str(exc)}
    finally:
        unregister(run_id)
        db.close()


def _execute_case(
    *,
    db: Session,
    run: ExecutionRun,
    case: TestCase | None,
    case_id: str,
    environment_payload: dict[str, Any],
    run_root: Path,
    secrets: list[str],
    control: Any,
    plan: RegressionPlan | None,
    plan_retry_count: int = 0,
    auto_retry: bool = False,
) -> dict[str, Any]:
    """执行一个用例，含按 RetryPolicy 的真实重试。返回本次用例的统计信息。"""
    case_result = CaseResult(
        run_id=run.id,
        case_id=case_id,
        case_code=(case.case_code if case else "") or "",
        case_name=(case.name if case else f"未知用例 {case_id}") or "",
        status="RUNNING",
        started_at=utcnow(),
    )
    db.add(case_result)
    db.commit()

    flow = _resolve_flow(db, case)
    if case is None or flow is None:
        case_result.status = "SKIPPED"
        case_result.error_message = "测试用例未绑定启用的自动化流程" if case else "测试用例不存在"
        case_result.finished_at = utcnow()
        db.commit()
        return {
            "bucket": "skipped",
            "status": "SKIPPED",
            "case_name": case_result.case_name,
            "duration_ms": 0,
            "attempts": 0,
            "error_message": case_result.error_message,
        }

    case_result.flow_id = flow.id
    db.commit()

    steps = normalize_steps((flow.definition or {}).get("steps"))
    if not steps:
        case_result.status = "SKIPPED"
        case_result.error_message = "自动化流程没有可执行步骤"
        case_result.finished_at = utcnow()
        db.commit()
        return {
            "bucket": "skipped",
            "status": "SKIPPED",
            "case_name": case_result.case_name,
            "duration_ms": 0,
            "attempts": 0,
            "error_message": case_result.error_message,
        }

    variables = build_variables(
        environment_payload.get("variables"),
        {"base_url": environment_payload.get("base_url", "")},
        flow.variables or {},
    )

    executor = PlaywrightExecutor(headless=settings.playwright_headless)
    recorded: list[dict[str, Any]] = []
    final_bucket = "errored"
    final_status = "ERROR"
    final_error = ""
    cancelled = False

    # M3：循环上界只保证「不会超过任何可能值」，真正的收窄发生在第一次失败之后 ——
    # 因为失败类型（分类）只有在失败之后才知道。见 failure/retry_policy.py。
    hard_cap = retry_policy.hard_attempt_cap(plan_retry_count, auto_retry=auto_retry)
    case_is_flaky = _case_has_flaky_history(db, case_id)

    for attempt_no in range(1, hard_cap + 1):
        if attempt_no > 1 and control.should_stop():
            cancelled = True
            break

        attempt_dir = run_root / "cases" / case_result.id / "attempts" / str(attempt_no)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_row = CaseResultAttempt(
            case_result_id=case_result.id,
            attempt_no=attempt_no,
            status="RUNNING",
            started_at=utcnow(),
        )
        db.add(attempt_row)
        db.commit()

        outcome = executor.run(
            steps=steps,
            environment=environment_payload,
            case_dir=attempt_dir,
            variables=variables,
            secrets=secrets,
            control=control,
            record_video=settings.playwright_record_video,
            record_har=settings.playwright_record_har,
        )
        attempt_status = _persist_attempt(db, run, case_result, attempt_row, outcome, secrets=secrets)
        recorded.append({"attempt_no": attempt_no, "status": attempt_status, "error": outcome.get("error_message") or ""})

        if attempt_status == "PASSED":
            final_status, final_bucket, final_error = "PASSED", "passed", ""
            break
        if attempt_status == "CANCELLED":
            final_status, final_bucket, final_error = "CANCELLED", "cancelled", outcome.get("error_message") or "执行被用户停止"
            cancelled = True
            break

        blocked = bool(outcome.get("blocked"))
        if attempt_status == "ERROR" and blocked:
            # 环境本身被门禁拦下，重试没有意义
            final_status, final_bucket, final_error = "ERROR", "errored", outcome.get("error_message") or ""
            break

        final_status = "ERROR" if attempt_status == "ERROR" else "FAILED"
        final_bucket = "errored" if attempt_status == "ERROR" else "failed"
        final_error = outcome.get("error_message") or ""

        # 分类与重试解耦：这里只是为了「决定还要不要重试」而取一次分类，
        # 结论本身由 FailureAnalysisService 在 Run 结束后统一入库。
        category = _attempt_category(outcome, blocked=blocked)
        if retry_policy.should_retry(
            plan_retry_count,
            attempt_no,
            category,
            auto_retry=auto_retry,
            blocked=blocked,
            case_is_flaky=case_is_flaky,
        ):
            logger.info(
                "用例 %s 第 %s 次尝试失败（%s），按重试策略再试一次", case_result.case_code, attempt_no, category
            )
        else:
            break

    # ---- 汇总：FAIL → RETRY → PASS 必须记 FLAKY，不能伪装成 PASS
    statuses = [item["status"] for item in recorded]
    earlier_failed = any(item in {"FAILED", "ERROR"} for item in statuses[:-1]) if len(statuses) > 1 else False
    if final_status == "PASSED" and earlier_failed:
        case_result.status = "FLAKY"
        case_result.flaky = True
        final_bucket = "flaky"
        # 保留第一次失败的原因，方便排查
        first_failure = next((item["error"] for item in recorded if item["status"] in {"FAILED", "ERROR"}), "")
        case_result.error_message = _clean(f"首次尝试失败后重试通过（Flaky）：{first_failure}", secrets, 2000)
    else:
        case_result.status = final_status
        case_result.flaky = False
        case_result.error_message = _clean(final_error, secrets, 2000)

    case_result.attempts_count = len(recorded) or 1
    case_result.attempt = case_result.attempts_count
    case_result.finished_at = utcnow()
    if case_result.started_at:
        case_result.duration_ms = int((case_result.finished_at - case_result.started_at).total_seconds() * 1000)
    db.commit()

    return {
        "bucket": "cancelled" if cancelled else final_bucket,
        "status": case_result.status,
        "case_name": case_result.case_name,
        "duration_ms": case_result.duration_ms,
        "attempts": case_result.attempts_count,
        "error_message": case_result.error_message,
    }


def _persist_attempt(
    db: Session,
    run: ExecutionRun,
    case_result: CaseResult,
    attempt_row: CaseResultAttempt,
    outcome: dict[str, Any],
    secrets: list[str] | None = None,
) -> str:
    """写一次 attempt 的步骤结果与全部证据。返回该 attempt 的状态。"""
    secrets = secrets or []
    step_rows: list[StepResult] = []
    for item in outcome.get("steps", []):
        step_row = StepResult(
            case_result_id=case_result.id,
            attempt_id=attempt_row.id,
            step_no=int(item.get("step_no") or 0),
            step_name=str(item.get("name") or "")[:255],
            action=str(item.get("action") or "")[:64],
            locator=item.get("locator") or {},
            status=str(item.get("status") or "ERROR"),
            # M3：落库的自由文本统一过一遍 redact()。
            # 这些字段会直接展示在页面上，也是失败指纹的输入 —— 页面上的实际值、
            # 断言错误信息里完全可能出现 token / password，不能因为「本来就在数据库里」
            # 就放过。脱敏对正常文本是空操作，所以代价只有一次正则。
            expected=_clean(item.get("expected"), secrets, 2000),
            actual=_clean(item.get("actual"), secrets, 2000),
            duration_ms=int(item.get("duration_ms") or 0),
            error=_clean(item.get("error"), secrets, 4000),
            attempt=int(item.get("attempt") or 1),
            screenshot_path=str(item.get("screenshot_path") or "")[:512],
            page_url=_clean(item.get("page_url"), secrets, 1024),
            page_title=str(item.get("page_title") or "")[:512],
            dom_path=str(item.get("dom_path") or "")[:512],
            log_path=str(item.get("log_path") or "")[:512],
            # M3 归因信号：只有失败步骤才会被填充（执行器侧控制）
            console_summary=_clean(item.get("console_summary"), secrets, 2000),
            network_summary=_clean(item.get("network_summary"), secrets, 2000),
            request_method=str(item.get("request_method") or "")[:16],
            request_url=_clean(item.get("request_url"), secrets, 1024),
            http_status=item.get("http_status") or None,
            js_error=_clean(item.get("js_error"), secrets, 2000),
        )
        db.add(step_row)
        step_rows.append(step_row)
    db.flush()

    for step_row, item in zip(step_rows, outcome.get("steps", [])):
        for key in ("screenshot_path", "before_screenshot_path", "after_screenshot_path", "dom_path", "log_path"):
            _record_artifact(db, run.id, case_result.id, step_row.id, attempt_row.id, str(item.get(key) or ""))

    trace_path = str(outcome.get("trace_path") or "")
    video_path = str(outcome.get("video_path") or "")
    har_path = str(outcome.get("har_path") or "")
    attempt_row.trace_path = trace_path[:512]
    attempt_row.video_path = video_path[:512]
    attempt_row.har_path = har_path[:512]
    for path in (trace_path, video_path, har_path):
        _record_artifact(db, run.id, case_result.id, None, attempt_row.id, path)
    # console / network 日志挂到 attempt（它们不归属任何单个步骤）
    attempt_dir = Path(trace_path).parent.parent if trace_path else None
    if attempt_dir is not None:
        for name, kind in (("logs/console.log", "console"), ("logs/network.log", "network")):
            candidate = attempt_dir / name
            if candidate.exists():
                _record_artifact(db, run.id, case_result.id, None, attempt_row.id, str(candidate))

    outcome_status = str(outcome.get("outcome") or "")
    if outcome_status == OUTCOME_PASSED:
        attempt_row.status = "PASSED"
    elif outcome_status == OUTCOME_FAILED:
        attempt_row.status = "FAILED"
    elif outcome_status == OUTCOME_CANCELLED:
        attempt_row.status = "CANCELLED"
    else:
        attempt_row.status = "ERROR"
    attempt_row.error_message = _clean(outcome.get("error_message"), secrets, 2000)
    attempt_row.finished_at = utcnow()
    if attempt_row.started_at:
        attempt_row.duration_ms = int((attempt_row.finished_at - attempt_row.started_at).total_seconds() * 1000)
    db.commit()
    return attempt_row.status


def _resolve_flow(db: Session, case: TestCase | None) -> AutomationFlow | None:
    if case is None:
        return None
    binding = db.execute(
        select(CaseFlowBinding).where(CaseFlowBinding.case_id == case.id, CaseFlowBinding.enabled.is_(True))
    ).scalars().first()
    if binding is None:
        return None
    return db.get(AutomationFlow, binding.flow_id)


# ---------------------------------------------------------------- Run 创建


def create_run(
    db: Session,
    *,
    project_id: str,
    plan_id: str | None = None,
    environment_id: str | None = None,
    trigger_source: str = "MANUAL",
    commit_sha: str = "",
    branch: str = "",
    pipeline_id: str = "",
    pipeline_url: str = "",
    case_ids: list[str] | None = None,
    source_run_id: str | None = None,
    rerun_mode: str = "",
    base_url_override: str = "",
) -> ExecutionRun:
    """建立一条 QUEUED 状态的执行记录。真正开跑由 workers 层决定 inline 还是 celery。"""
    plan = db.get(RegressionPlan, plan_id) if plan_id else None
    resolved_environment = environment_id or (plan.environment_id if plan else None) or ""
    total = len(case_ids) if case_ids else None
    run = ExecutionRun(
        project_id=project_id,
        plan_id=plan_id,
        environment_id=resolved_environment,
        commit_sha=commit_sha,
        branch=branch,
        trigger_source=trigger_source,
        pipeline_id=pipeline_id,
        pipeline_url=pipeline_url,
        base_url_override=base_url_override,
        status="QUEUED",
        total_cases=total if total is not None else 0,
        case_ids=list(case_ids) if case_ids else None,
        source_run_id=source_run_id,
        rerun_mode=rerun_mode,
        cancel_requested=False,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def failed_case_ids(db: Session, run_id: str) -> list[str]:
    """原 Run 里真正失败的用例（FAILED / ERROR）。FLAKY 最终是通过了，不算失败。"""
    rows = db.execute(
        select(CaseResult.case_id, CaseResult.status)
        .where(CaseResult.run_id == run_id, CaseResult.status.in_(["FAILED", "ERROR"]))
        .order_by(CaseResult.id)
    ).all()
    return [str(row[0]) for row in rows if row[0]]


def encode_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def run_artifact_root(project_id: str, run_id: str) -> Path:
    return settings.runs_dir / project_id / "runs" / run_id


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def load_suite(db: Session, suite_id: str) -> TestSuite | None:
    return db.execute(
        select(TestSuite).options(selectinload(TestSuite.members)).where(TestSuite.id == suite_id)
    ).scalars().first()
