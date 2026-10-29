"""M6-A Artifact / Storage 安全加固测试。

只覆盖「浏览器不能匿名访问 /storage，只能通过鉴权或短时签名访问 Artifact」这条链路。
不触碰 Dashboard / Retention / Production Safety。
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.auth.security import create_access_token  # noqa: E402
from backend.app.config import settings  # noqa: E402
from backend.app.db import SessionLocal, init_db  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Artifact,
    CaseResult,
    CaseResultAttempt,
    Environment,
    ExecutionRun,
    Project,
    StepResult,
    User,
)
from backend.app.seed import bootstrap_admin  # noqa: E402
from backend.app.utils.artifact_signing import build_artifact_url  # noqa: E402

SUFFIX = uuid.uuid4().hex[:8]


def _admin_headers() -> dict[str, str]:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.role == "ADMIN").first()
        assert user is not None, "测试库必须已有 ADMIN 用户"
        return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
    finally:
        db.close()


@pytest.fixture(scope="module")
def artifact_data() -> dict[str, str]:
    init_db()
    bootstrap_admin()

    db = SessionLocal()
    test_dir = settings.storage_dir / "_m6_security" / SUFFIX
    test_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, tuple[str, bytes]] = {
        "screenshot": ("shot.png", b"\x89PNG\r\n\x1a\n" + b"0" * 16),
        "video": ("recording.webm", b"\x1aE\xdf\xa3" + b"1" * 128),
        "trace": ("trace.zip", b"PK\x05\x06" + b"2" * 64),
        "har": ("network.har", b'{"log":{"version":"1.2"}}'),
        "console": ("console.log", b"console: hello\n"),
        "network": ("network.log", b"200 GET /home\n"),
        "dom": ("page.html", b"<!doctype html><html><body>ok</body></html>"),
        "log": ("step.log", b"step log\n"),
    }

    project = Project(name=f"M6安全测试项目-{SUFFIX}", description="test_m6_artifact_security")
    db.add(project)
    db.commit()
    environment = Environment(
        project_id=project.id,
        name=f"M6安全测试环境-{SUFFIX}",
        base_url="http://127.0.0.1:8100",
        environment_type="TEST",
        allowed_hosts=["127.0.0.1"],
    )
    db.add(environment)
    db.commit()
    run = ExecutionRun(
        project_id=project.id,
        environment_id=environment.id,
        status="PASSED",
        trigger_source="MANUAL",
        total_cases=1,
        passed_cases=1,
    )
    db.add(run)
    db.commit()
    case = CaseResult(run_id=run.id, case_code="M6-ART-001", case_name="artifact security", status="PASSED")
    db.add(case)
    db.commit()
    attempt = CaseResultAttempt(case_result_id=case.id, attempt_no=1, status="PASSED")
    db.add(attempt)
    db.commit()
    step = StepResult(
        case_result_id=case.id,
        attempt_id=attempt.id,
        step_no=1,
        step_name="open",
        action="open",
        status="PASSED",
    )
    db.add(step)
    db.commit()

    ids: dict[str, str] = {}
    for kind, (filename, content) in files.items():
        path = test_dir / filename
        path.write_bytes(content)
        artifact = Artifact(
            run_id=run.id,
            case_result_id=case.id,
            step_result_id=step.id,
            attempt_id=attempt.id,
            type=kind,
            path=str(path),
            size=len(content),
        )
        db.add(artifact)
        db.commit()
        ids[kind] = artifact.id

    outside_path = str((test_dir / ".." / ".." / ".." / f"m6-outside-{SUFFIX}.txt").resolve())
    outside = Artifact(
        run_id=run.id,
        case_result_id=case.id,
        step_result_id=step.id,
        attempt_id=attempt.id,
        type="log",
        path=outside_path,
        size=0,
    )
    db.add(outside)
    db.commit()
    ids["outside"] = outside.id

    missing_path = test_dir / "missing.txt"
    missing = Artifact(
        run_id=run.id,
        case_result_id=case.id,
        step_result_id=step.id,
        attempt_id=attempt.id,
        type="log",
        path=str(missing_path),
        size=0,
    )
    db.add(missing)
    db.commit()
    ids["missing"] = missing.id

    db.close()
    yield {
        **ids,
        "project_id": project.id,
        "run_id": run.id,
        "case_result_id": case.id,
        "attempt_id": attempt.id,
        "step_result_id": step.id,
        "test_dir": str(test_dir),
    }

    cleanup_db = SessionLocal()
    try:
        project_row = cleanup_db.get(Project, project.id)
        if project_row is not None:
            cleanup_db.delete(project_row)
            cleanup_db.commit()
    finally:
        cleanup_db.close()
    shutil.rmtree(test_dir, ignore_errors=True)


def test_signed_screenshot_url_is_accessible_without_header(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    with TestClient(app) as client:
        response = client.get(f"/api/artifacts/{shot_id}", headers=_admin_headers())
        assert response.status_code == 200
        url = response.json()["url"]
        assert url.startswith(f"/api/artifacts/{shot_id}/content?")

        content = client.get(url)
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/png")
    assert "inline" in content.headers.get("content-disposition", "")


def test_bearer_content_access_works(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    with TestClient(app) as client:
        response = client.get(f"/api/artifacts/{shot_id}/content", headers=_admin_headers())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


def test_content_without_auth_or_signature_is_401(artifact_data: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.get(f"/api/artifacts/{artifact_data['screenshot']}/content")
    assert response.status_code == 401


def test_nonexistent_artifact_is_404() -> None:
    with TestClient(app) as client:
        response = client.get("/api/artifacts/ffffffffffff/content", headers=_admin_headers())
    assert response.status_code == 404


def test_invalid_artifact_id_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.get("/api/artifacts/not-a-valid-id/content", headers=_admin_headers())
    assert response.status_code == 404


def test_missing_file_returns_410(artifact_data: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.get(f"/api/artifacts/{artifact_data['missing']}/content", headers=_admin_headers())
    assert response.status_code == 410


def test_path_outside_storage_is_rejected(artifact_data: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.get(f"/api/artifacts/{artifact_data['outside']}/content", headers=_admin_headers())
    assert response.status_code == 403


def test_wrong_signature_is_rejected(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    url = build_artifact_url(shot_id, "inline") + "x"
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 403


def test_expired_signature_is_rejected(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    expired = build_artifact_url(shot_id, "inline", now=int(time.time()) - 400)
    with TestClient(app) as client:
        response = client.get(expired)
    assert response.status_code == 403


def test_modified_artifact_id_invalidates_signature(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    other_id = artifact_data["video"]
    url = build_artifact_url(shot_id, "inline").replace(shot_id, other_id)
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 403


def test_modified_expiration_invalidates_signature(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    url = build_artifact_url(shot_id, "inline")
    # 直接改 query 里的 exp：签名内容变化，必须失效。
    prefix, rest = url.split("exp=", 1)
    _, suffix = rest.split("&", 1)
    tampered = f"{prefix}exp=9999999999&{suffix}"
    with TestClient(app) as client:
        response = client.get(tampered)
    assert response.status_code == 403


def test_modified_disposition_invalidates_signature(artifact_data: dict[str, str]) -> None:
    shot_id = artifact_data["screenshot"]
    url = build_artifact_url(shot_id, "inline").replace("disposition=inline", "disposition=attachment")
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 403


def test_video_inline_and_range_request(artifact_data: dict[str, str]) -> None:
    video_id = artifact_data["video"]
    url = build_artifact_url(video_id, "inline")
    with TestClient(app) as client:
        full = client.get(url)
        ranged = client.get(url, headers={"Range": "bytes=0-3"})
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("video/webm")
    assert ranged.status_code == 206
    assert len(ranged.content) == 4


def test_trace_attachment(artifact_data: dict[str, str]) -> None:
    trace_id = artifact_data["trace"]
    url = build_artifact_url(trace_id, "attachment")
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert response.headers["content-type"].startswith("application/zip")


def test_download_endpoint_requires_auth_and_downloads_trace(artifact_data: dict[str, str]) -> None:
    trace_id = artifact_data["trace"]
    with TestClient(app) as client:
        unauthorized = client.get(f"/api/artifacts/{trace_id}/download")
        authorized = client.get(f"/api/artifacts/{trace_id}/download", headers=_admin_headers())
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert "attachment" in authorized.headers.get("content-disposition", "")
    assert authorized.headers["content-type"].startswith("application/zip")


def test_har_attachment(artifact_data: dict[str, str]) -> None:
    har_id = artifact_data["har"]
    url = build_artifact_url(har_id, "attachment")
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert response.headers["content-type"].startswith("application/json")


def test_console_network_log_dom_are_viewable(artifact_data: dict[str, str]) -> None:
    for kind, content_type in (
        ("console", "text/plain"),
        ("network", "text/plain"),
        ("log", "text/plain"),
        ("dom", "text/html"),
    ):
        url = build_artifact_url(artifact_data[kind], "inline")
        with TestClient(app) as client:
            response = client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(content_type)


def test_old_storage_path_is_not_a_file_entry(artifact_data: dict[str, str]) -> None:
    with TestClient(app) as client:
        response = client.get(f"/storage/{SUFFIX}/should-not-exist.png")
    assert response.status_code != 200
