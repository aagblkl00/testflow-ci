"""M4 CI Integration 测试。

覆盖 CI 参数解析、鉴权、幂等、并发、Exit Code 映射与生产门禁。
**真实浏览器执行不在本文件**（由 `scripts/selftest_m4.py` 覆盖），
所以这里把 `dispatch_run` 换成了空操作 —— 否则每建一条 Run 都会真的拉起 Chromium，
测试会变成又慢又不稳。

跑法：
    .venv/Scripts/python.exe -m pytest tests -q
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import sys
import uuid
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.ci import service as ci_service  # noqa: E402
from backend.app.ci.auth import CI_TOKEN_HEADER  # noqa: E402
from backend.app.config import settings  # noqa: E402
from backend.app.db import SessionLocal, init_db  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import Environment, ExecutionRun, Project, RegressionPlan, utcnow  # noqa: E402
from cli.testflow.__main__ import build_parser  # noqa: E402
from cli.testflow.output import emit_json  # noqa: E402

run_exit_code = ci_service.run_exit_code

CI_TOKEN = settings.ci_token or "test-token"
CI_HEADERS = {CI_TOKEN_HEADER: CI_TOKEN}
SUFFIX = uuid.uuid4().hex[:8]


# ------------------------------------------------------------------ 夹具


@pytest.fixture(scope="module")
def fixtures() -> dict[str, str]:
    """建一个一次性的项目 / 测试环境 / PROD 环境 / 计划，测试结束后清掉。"""
    init_db()
    db = SessionLocal()
    try:
        project = Project(name=f"M4测试项目-{SUFFIX}", description="test_m4_ci")
        db.add(project)
        db.commit()

        test_env = Environment(
            project_id=project.id,
            name=f"M4测试环境-{SUFFIX}",
            base_url="http://127.0.0.1:8100",
            environment_type="TEST",
            allowed_hosts=["127.0.0.1", "localhost"],
        )
        prod_env = Environment(
            project_id=project.id,
            name=f"M4生产环境-{SUFFIX}",
            base_url="https://prod.example.com",
            environment_type="PROD",
            allowed_hosts=["prod.example.com"],
        )
        db.add_all([test_env, prod_env])
        db.commit()

        plan = RegressionPlan(
            project_id=project.id,
            name=f"M4计划-{SUFFIX}",
            environment_id=test_env.id,
            case_ids=[],
            enabled=True,
        )
        disabled = RegressionPlan(
            project_id=project.id,
            name=f"M4禁用计划-{SUFFIX}",
            environment_id=test_env.id,
            case_ids=[],
            enabled=False,
        )
        db.add_all([plan, disabled])
        db.commit()

        data = {
            "project": project.name,
            "project_id": project.id,
            "env": test_env.name,
            "env_id": test_env.id,
            "prod_env": prod_env.name,
            "plan": plan.name,
            "plan_id": plan.id,
            "disabled_plan": disabled.name,
        }
    finally:
        db.close()

    yield data

    cleanup = SessionLocal()
    try:
        project = cleanup.get(Project, data["project_id"])
        if project is not None:
            cleanup.delete(project)
            cleanup.commit()
    finally:
        cleanup.close()


@pytest.fixture(scope="module", autouse=True)
def no_dispatch() -> None:
    """把派发换成空操作：单元测试不该真的去开浏览器。"""
    import backend.app.api.ci as ci_api

    original = ci_api.dispatch_run
    ci_api.dispatch_run = lambda *_args, **_kwargs: None
    yield
    ci_api.dispatch_run = original


def _ci_headers(token: str | None = CI_TOKEN) -> dict[str, str]:
    return {} if token is None else {CI_TOKEN_HEADER: token}


def _payload(fixtures: dict[str, str], **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "project": fixtures["project"],
        "plan": fixtures["plan"],
        "environment": fixtures["env"],
        "source": "ci",
    }
    body.update(overrides)
    return body


# ------------------------------------------------------- 1. CLI 参数解析


def test_cli_parser_run_defaults() -> None:
    args = build_parser().parse_args(["run", "--plan", "smoke", "--env", "test"])
    assert args.plan == "smoke"
    assert args.environment == "test"
    assert args.wait is True, "CI 场景默认必须等待结果"
    assert args.timeout == 3600
    assert args.json is False
    assert args.base_url == ""


def test_cli_parser_no_wait_and_json() -> None:
    args = build_parser().parse_args(["run", "--plan", "smoke", "--env", "test", "--no-wait", "--json"])
    assert args.wait is False
    assert args.json is True


def test_cli_parser_status() -> None:
    args = build_parser().parse_args(["status", "--run-id", "abc123"])
    assert args.run_id == "abc123"


def test_cli_requires_plan_and_env() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--plan", "smoke"])


def test_cli_missing_subcommand_returns_system_error() -> None:
    from cli.testflow.__main__ import main

    with redirect_stdout(io.StringIO()):
        assert main([]) == 2


# ------------------------------------------------------------- 2-3. 解析失败


def test_plan_not_found(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/ci/runs", json=_payload(fixtures, plan="不存在的计划"), headers=CI_HEADERS
        )
    assert response.status_code == 404
    assert "回归计划不存在" in response.json()["detail"]


def test_environment_not_found(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/ci/runs", json=_payload(fixtures, environment="不存在的环境"), headers=CI_HEADERS
        )
    assert response.status_code == 404
    assert "环境不存在" in response.json()["detail"]


def test_project_not_found(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/ci/runs", json=_payload(fixtures, project="不存在的项目"), headers=CI_HEADERS
        )
    assert response.status_code == 404


def test_disabled_plan_rejected(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/ci/runs", json=_payload(fixtures, plan=fixtures["disabled_plan"]), headers=CI_HEADERS
        )
    assert response.status_code == 400
    assert "禁用" in response.json()["detail"]


# --------------------------------------------------------------- 4-6. 鉴权


def test_ci_token_accepted(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post("/api/ci/runs", json=_payload(fixtures), headers=CI_HEADERS)
    assert response.status_code == 200


def test_missing_token_rejected(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post("/api/ci/runs", json=_payload(fixtures))
    assert response.status_code == 401


def test_wrong_token_rejected(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post("/api/ci/runs", json=_payload(fixtures), headers=_ci_headers("wrong-token"))
    assert response.status_code == 401


def test_error_response_does_not_leak_token(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.post("/api/ci/runs", json=_payload(fixtures), headers=_ci_headers("wrong-token"))
    assert CI_TOKEN not in response.text


# ------------------------------------------------------------ 7. Run 创建


def test_run_created_with_ci_metadata(fixtures: dict[str, str]) -> None:
    pipeline_id = f"pytest-create-{SUFFIX}"
    with TestClient(app) as client:
        response = client.post(
            "/api/ci/runs",
            json=_payload(
                fixtures,
                pipeline_id=pipeline_id,
                pipeline_url="https://gitlab.example/group/app/-/pipelines/1",
                commit_sha="abc1234567890",
                branch="feature/m4",
                source="gitlab",
            ),
            headers=CI_HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["status"] == "QUEUED"
    assert body["exit_code"] is None and body["finished"] is False

    db = SessionLocal()
    try:
        run = db.get(ExecutionRun, body["run_id"])
        assert run is not None
        assert run.trigger_source == "GITLAB"
        assert run.pipeline_id == pipeline_id
        assert run.commit_sha == "abc1234567890"
        assert run.branch == "feature/m4"
    finally:
        db.close()


# ------------------------------------------------- 8-10. Exit Code 映射


def test_exit_code_mapping_pure() -> None:
    assert run_exit_code("PASSED") == 0
    assert run_exit_code("FAILED") == 1
    assert run_exit_code("PARTIAL") == 1
    assert run_exit_code("CANCELLED") == 1
    assert run_exit_code("ERROR") == 2
    assert run_exit_code("RUNNING") is None
    assert run_exit_code("QUEUED") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [("PASSED", 0), ("FAILED", 1), ("PARTIAL", 1), ("ERROR", 2)],
)
def test_ci_status_exit_code(fixtures: dict[str, str], status: str, expected: int) -> None:
    """真实落库一条指定状态的 Run，再从 CI 接口读退出码。"""
    db = SessionLocal()
    try:
        run = ExecutionRun(
            project_id=fixtures["project_id"],
            plan_id=fixtures["plan_id"],
            environment_id=fixtures["env_id"],
            status=status,
            trigger_source="CI",
            total_cases=3,
            passed_cases=3 if status == "PASSED" else 1,
            failed_cases=0 if status == "PASSED" else 2,
            finished_at=utcnow(),
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()

    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/ci-status", headers=CI_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == expected
    assert body["finished"] is True


def test_ci_status_requires_token(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        assert client.get("/api/runs/whatever/ci-status").status_code == 401


# --------------------------------------------------------- 11-13. 其他接口


def test_ci_status_unknown_run() -> None:
    with TestClient(app) as client:
        response = client.get("/api/runs/nope/ci-status", headers=CI_HEADERS)
    assert response.status_code == 404


def test_ci_status_payload_shape(fixtures: dict[str, str]) -> None:
    """JSON schema 稳定性：这些键必须一直存在，否则 CI 侧解析会静默失败。"""
    with TestClient(app) as client:
        created = client.post(
            "/api/ci/runs", json=_payload(fixtures, pipeline_id=f"pytest-shape-{SUFFIX}"), headers=CI_HEADERS
        ).json()
        body = client.get(f"/api/runs/{created['run_id']}/ci-status", headers=CI_HEADERS).json()
    for key in (
        "run_id",
        "status",
        "finished",
        "exit_code",
        "exit_code_meaning",
        "project_id",
        "plan_id",
        "plan_name",
        "environment",
        "trigger_source",
        "ci_provider",
        "pipeline_id",
        "pipeline_url",
        "commit_sha",
        "branch",
        "summary",
        "failure_groups",
        "duration_ms",
    ):
        assert key in body, f"CI 视图缺少字段 {key}"
    for key in ("total", "passed", "failed", "skipped", "cancelled", "error", "flaky"):
        assert key in body["summary"]
    assert set(body["failure_groups"]) == {"total", "categories"}


def test_json_output_is_clean_stdout() -> None:
    """JSON 输出必须能被 json.loads 直接解析，混进一行日志就废了。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        emit_json({"run_id": "abc", "exit_code": 1, "summary": {"total": 3}})
    parsed = json.loads(buffer.getvalue())
    assert parsed["run_id"] == "abc"
    assert parsed["exit_code"] == 1


# --------------------------------------------------------------- 14-15. 幂等


def test_idempotent_repeated_requests(fixtures: dict[str, str]) -> None:
    pipeline_id = f"pytest-idem-{SUFFIX}"
    run_ids = []
    created_flags = []
    with TestClient(app) as client:
        for _ in range(3):
            body = client.post(
                "/api/ci/runs", json=_payload(fixtures, pipeline_id=pipeline_id), headers=CI_HEADERS
            ).json()
            run_ids.append(body["run_id"])
            created_flags.append(body["created"])
    assert created_flags == [True, False, False]
    assert len(set(run_ids)) == 1


def test_different_pipeline_creates_new_run(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        first = client.post(
            "/api/ci/runs", json=_payload(fixtures, pipeline_id=f"pytest-a-{SUFFIX}"), headers=CI_HEADERS
        ).json()
        second = client.post(
            "/api/ci/runs", json=_payload(fixtures, pipeline_id=f"pytest-b-{SUFFIX}"), headers=CI_HEADERS
        ).json()
    assert first["run_id"] != second["run_id"]


def test_manual_run_not_blocked_by_idempotency(fixtures: dict[str, str]) -> None:
    """手动执行不带 pipeline_id，不能被 CI 幂等约束挡住。"""
    with TestClient(app) as client:
        first = client.post("/api/ci/runs", json=_payload(fixtures, pipeline_id=""), headers=CI_HEADERS).json()
        second = client.post("/api/ci/runs", json=_payload(fixtures, pipeline_id=""), headers=CI_HEADERS).json()
    assert first["run_id"] != second["run_id"]


def test_concurrent_identical_requests_create_one_run(fixtures: dict[str, str]) -> None:
    """10 个线程同时建同一条：数据库唯一约束必须兜住，只允许 1 条。

    刻意不用 TestClient（它本身不是为多线程设计的），而是像 10 个并发 HTTP 请求
    那样各开一个 Session —— 每个 Session 一条独立连接，这才是真正的并发路径。
    """
    pipeline_id = f"pytest-concurrent-{SUFFIX}"

    def worker(_: int) -> tuple[str, bool]:
        session = SessionLocal()
        try:
            project = session.get(Project, fixtures["project_id"])
            plan = session.get(RegressionPlan, fixtures["plan_id"])
            environment = session.get(Environment, fixtures["env_id"])
            run, created = ci_service.create_ci_run(
                session,
                project=project,
                plan=plan,
                environment=environment,
                trigger_source="CI",
                pipeline_id=pipeline_id,
            )
            return run.id, created
        finally:
            session.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker, range(10)))

    run_ids = {item[0] for item in results}
    assert len(run_ids) == 1, f"并发下建出了 {len(run_ids)} 条 Run"
    assert sum(1 for item in results if item[1]) == 1, "只应有一次 created=True"


# ------------------------------------------------------ 16. 生产环境门禁


def test_production_environment_rejected(fixtures: dict[str, str]) -> None:
    with TestClient(app) as client:
        before = client.get("/api/projects").status_code  # 触发 app 启动，保证 DB 就绪
        response = client.post(
            "/api/ci/runs", json=_payload(fixtures, environment=fixtures["prod_env"]), headers=CI_HEADERS
        )
    assert before == 401  # 网页接口仍需 JWT，CI Token 不能拿来当登录态
    assert response.status_code == 400
    assert "安全门禁" in response.json()["detail"]


def test_production_rejection_creates_no_run(fixtures: dict[str, str]) -> None:
    pipeline_id = f"pytest-prod-{SUFFIX}"
    with TestClient(app) as client:
        client.post(
            "/api/ci/runs",
            json=_payload(fixtures, environment=fixtures["prod_env"], pipeline_id=pipeline_id),
            headers=CI_HEADERS,
        )
    db = SessionLocal()
    try:
        count = db.query(ExecutionRun).filter(ExecutionRun.pipeline_id == pipeline_id).count()
    finally:
        db.close()
    assert count == 0, "被门禁拒绝的请求不应留下 Run 记录"


def test_ci_token_not_usable_for_web_api(fixtures: dict[str, str]) -> None:
    """CI Token 只作用于 /api/ci/*，不能顺带把网页接口也开了。"""
    with TestClient(app) as client:
        assert client.get("/api/projects", headers=CI_HEADERS).status_code == 401
        assert client.get("/api/failures", headers=CI_HEADERS).status_code == 401
