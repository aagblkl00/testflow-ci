"""M6-B：Artifact Retention 安全治理测试。"""

from __future__ import annotations

import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.auth.security import create_access_token, hash_password  # noqa: E402
from backend.app.config import settings  # noqa: E402
from backend.app.db import SessionLocal, init_db  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Alert,
    Artifact,
    ExecutionRun,
    FailureFingerprint,
    FailureGroup,
    Project,
    User,
)
from backend.app.seed import bootstrap_admin  # noqa: E402
from backend.app.services import retention as retention_service  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


@pytest.fixture(scope="module")
def users() -> dict[str, User]:
    init_db()
    bootstrap_admin()
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "ADMIN").first()
        tester = db.query(User).filter(User.role == "TESTER").first()
        viewer = db.query(User).filter(User.role == "VIEWER").first()
        if tester is None:
            tester = User(username=f"ret_tester_{uuid.uuid4().hex[:8]}", display_name="ret tester", role="TESTER", password_hash=hash_password("test"))
            db.add(tester)
        if viewer is None:
            viewer = User(username=f"ret_viewer_{uuid.uuid4().hex[:8]}", display_name="ret viewer", role="VIEWER", password_hash=hash_password("test"))
            db.add(viewer)
        db.commit()
        db.refresh(tester)
        db.refresh(viewer)
        assert admin is not None
        return {"admin": admin, "tester": tester, "viewer": viewer}
    finally:
        db.close()


class RetentionWorldFactory:
    """每个测试独立的项目/运行/证据小世界，结束统一清理。"""

    def __init__(self) -> None:
        self.suffix = uuid.uuid4().hex[:8]
        self.project_ids: list[str] = []
        self.created_paths: list[Path] = []
        self.base_dir = settings.storage_dir / "_m6_retention" / self.suffix

    def project(self) -> str:
        db = SessionLocal()
        try:
            project = Project(name=f"retention-project-{self.suffix}-{len(self.project_ids)}", description="test_m6_retention")
            db.add(project)
            db.commit()
            self.project_ids.append(project.id)
            return project.id
        finally:
            db.close()

    def run(self, project_id: str, status: str, finished_at: datetime | None = None) -> str:
        db = SessionLocal()
        try:
            run = ExecutionRun(
                project_id=project_id,
                status=status,
                started_at=(finished_at - timedelta(hours=1)) if finished_at else None,
                finished_at=finished_at,
                trigger_source="MANUAL",
                total_cases=1,
            )
            db.add(run)
            db.commit()
            return run.id
        finally:
            db.close()

    def artifact(
        self,
        run_id: str,
        filename: str,
        size: int,
        *,
        content: bytes | None = None,
        make_dir: bool = False,
        create: bool = True,
        absolute_path: str | None = None,
    ) -> tuple[str, Path]:
        db = SessionLocal()
        try:
            if absolute_path is not None:
                path = Path(absolute_path)
            else:
                path = self.base_dir / filename
            if create:
                path.parent.mkdir(parents=True, exist_ok=True)
                if make_dir:
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.write_bytes(content or b"x" * size)
                self.created_paths.append(path)
            artifact = Artifact(run_id=run_id, type="log", path=str(path), size=size)
            db.add(artifact)
            db.commit()
            return artifact.id, path
        finally:
            db.close()

    def alert_group(
        self,
        project_id: str,
        *,
        group_status: str,
        alert_status: str,
        run_id: str,
        latest_run_id: str,
        reverify_run_id: str = "",
    ) -> None:
        db = SessionLocal()
        try:
            fingerprint = FailureFingerprint(
                fingerprint=f"ret-{self.suffix}-{uuid.uuid4().hex[:10]}",
                strategy="UNKNOWN",
                failure_type="unknown",
            )
            db.add(fingerprint)
            db.commit()
            group = FailureGroup(
                project_id=project_id,
                fingerprint_id=fingerprint.id,
                title=f"retention group {uuid.uuid4().hex[:8]}",
                status=group_status,
                latest_run_id=latest_run_id or "",
            )
            db.add(group)
            db.commit()
            alert = Alert(
                project_id=project_id,
                failure_group_id=group.id,
                run_id=run_id,
                latest_run_id=latest_run_id or "",
                reverify_run_id=reverify_run_id or "",
                severity="WARNING",
                status=alert_status,
                title="retention alert",
            )
            db.add(alert)
            db.commit()
        finally:
            db.close()

    def open_group(self, project_id: str, latest_run_id: str) -> None:
        """只建 OPEN FailureGroup，不建 Alert，专门验证最新 Run 保护。"""
        db = SessionLocal()
        try:
            fingerprint = FailureFingerprint(
                fingerprint=f"open-group-{self.suffix}-{uuid.uuid4().hex[:10]}",
                strategy="UNKNOWN",
                failure_type="unknown",
            )
            db.add(fingerprint)
            db.commit()
            group = FailureGroup(
                project_id=project_id,
                fingerprint_id=fingerprint.id,
                title=f"open group {uuid.uuid4().hex[:8]}",
                status="OPEN",
                latest_run_id=latest_run_id,
            )
            db.add(group)
            db.commit()
        finally:
            db.close()

    def cleanup(self) -> None:
        db = SessionLocal()
        try:
            for project_id in self.project_ids:
                project = db.get(Project, project_id)
                if project is not None:
                    db.delete(project)
                    db.commit()
        finally:
            db.close()
        for path in self.created_paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


@pytest.fixture
def world() -> RetentionWorldFactory:
    factory = RetentionWorldFactory()
    yield factory
    factory.cleanup()


def _seed_standard(world: RetentionWorldFactory, now: datetime) -> dict[str, object]:
    project_id = world.project()

    def old_run(status: str) -> str:
        return world.run(project_id, status, now - timedelta(days=60))

    old_pass = old_run("PASSED")
    recent_pass = world.run(project_id, "PASSED", now - timedelta(days=5))
    running = world.run(project_id, "RUNNING")
    queued = world.run(project_id, "QUEUED")
    old_cancelled = old_run("CANCELLED")
    old_error = old_run("ERROR")

    dup_run = old_run("FAILED")
    dup_path = world.artifact(dup_run, "dup.bin", 700, content=b"d" * 700)[1]
    world.artifact(dup_run, "dup.bin", 700, content=b"d" * 700)

    missing_run = old_run("FAILED")
    missing_id, missing_path = world.artifact(missing_run, "missing.log", 800, create=False)

    outside_path = ROOT / f"_m6_retention_outside_{world.suffix}.log"
    outside_run = old_run("FAILED")
    outside_id, _outside = world.artifact(outside_run, "", 900, absolute_path=str(outside_path), content=b"o" * 900)

    dir_run = old_run("FAILED")
    dir_id, dir_path = world.artifact(dir_run, "dir_artifact", 1000, make_dir=True)

    alert_run = old_run("FAILED")
    world.artifact(alert_run, "alert_protected.log", 1100, content=b"a" * 1100)

    group_run = old_run("FAILED")
    world.artifact(group_run, "group_protected.log", 1200, content=b"g" * 1200)

    reverify_source = old_run("FAILED")
    world.artifact(reverify_source, "reverify_source.log", 1300, content=b"r" * 1300)
    reverify_run = old_run("PASSED")
    world.artifact(reverify_run, "reverify_protected.log", 1400, content=b"v" * 1400)

    old_pass_id, old_pass_path = world.artifact(old_pass, "old_pass.log", 100, content=b"p" * 100)
    recent_id, recent_path = world.artifact(recent_pass, "recent.log", 200, content=b"n" * 200)
    running_id, running_path = world.artifact(running, "running.log", 300, content=b"u" * 300)
    queued_id, queued_path = world.artifact(queued, "queued.log", 400, content=b"q" * 400)
    old_cancelled_id, old_cancelled_path = world.artifact(old_cancelled, "old_cancel.log", 500, content=b"c" * 500)
    old_error_id, old_error_path = world.artifact(old_error, "old_error.log", 600, content=b"e" * 600)

    world.alert_group(
        project_id,
        group_status="OPEN",
        alert_status="OPEN",
        run_id=alert_run,
        latest_run_id=alert_run,
    )
    world.alert_group(
        project_id,
        group_status="OPEN",
        alert_status="REVERIFY",
        run_id=reverify_source,
        latest_run_id=reverify_source,
        reverify_run_id=reverify_run,
    )
    world.alert_group(
        project_id,
        group_status="OPEN",
        alert_status="OPEN",
        run_id=group_run,
        latest_run_id=group_run,
    )

    return {
        "project_id": project_id,
        "candidate_runs": [old_pass, old_cancelled, old_error, dup_run, missing_run, outside_run, dir_run],
        "protected_runs": [running, queued, alert_run, group_run, reverify_source, reverify_run],
        "dup_path": dup_path,
        "missing_id": missing_id,
        "missing_path": missing_path,
        "outside_id": outside_id,
        "outside_path": outside_path,
        "dir_id": dir_id,
        "dir_path": dir_path,
        "old_pass_id": old_pass_id,
        "old_pass_path": old_pass_path,
        "recent_id": recent_id,
        "recent_path": recent_path,
        "running_id": running_id,
        "running_path": running_path,
        "queued_id": queued_id,
        "queued_path": queued_path,
        "old_cancelled_id": old_cancelled_id,
        "old_cancelled_path": old_cancelled_path,
        "old_error_id": old_error_id,
        "old_error_path": old_error_path,
    }


def _artifact_count(run_ids: list[str]) -> int:
    db = SessionLocal()
    try:
        return int(db.execute(select(func.count(Artifact.id)).where(Artifact.run_id.in_(run_ids))).scalar_one() or 0)
    finally:
        db.close()


def _artifact_exists(artifact_id: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(Artifact, artifact_id) is not None
    finally:
        db.close()


def test_preview_counts_and_is_readonly(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    data = _seed_standard(world, _now())
    candidate_runs = data["candidate_runs"]
    protected_runs = data["protected_runs"]
    before_candidates = _artifact_count(candidate_runs)
    before_protected = _artifact_count(protected_runs)

    with TestClient(app) as client:
        response = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["candidate_artifact_count"] == 8
    assert payload["candidate_path_count"] == 7
    assert payload["estimated_bytes"] == 1900
    assert payload["protected_artifact_count"] >= 6
    assert payload["protected_run_count"] >= 6
    assert payload["duplicate_path_count"] == 1
    assert len(payload["sample_artifacts"]) <= 5
    assert len(payload["sample_runs"]) <= 5

    assert _artifact_count(candidate_runs) == before_candidates
    assert _artifact_count(protected_runs) == before_protected
    for path in (
        data["old_pass_path"],
        data["old_cancelled_path"],
        data["old_error_path"],
        data["dup_path"],
    ):
        assert Path(path).exists()


def test_preview_excludes_recent_and_active_runs(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    now = _now()
    project_id = world.project()
    old_pass = world.run(project_id, "PASSED", now - timedelta(days=60))
    recent_pass = world.run(project_id, "PASSED", now - timedelta(days=5))
    running = world.run(project_id, "RUNNING")
    queued = world.run(project_id, "QUEUED")
    old_cancelled = world.run(project_id, "CANCELLED", now - timedelta(days=60))
    old_error = world.run(project_id, "ERROR", now - timedelta(days=60))
    for run_id in (old_pass, recent_pass, running, queued, old_cancelled, old_error):
        world.artifact(run_id, f"{run_id}.log", 10, content=b"x" * 10)

    with TestClient(app) as client:
        response = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["candidate_artifact_count"] == 3
    assert payload["candidate_path_count"] == 3
    assert payload["protected_artifact_count"] >= 2
    assert payload["protected_run_count"] >= 2


def test_preview_apply_require_admin(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "PASSED", _now() - timedelta(days=60))
    world.artifact(run_id, "perm.log", 10, content=b"x" * 10)

    with TestClient(app) as client:
        assert client.post("/api/admin/retention/preview").status_code == 401
        assert client.post("/api/admin/retention/apply").status_code == 401
        for role in ("tester", "viewer"):
            preview = client.post("/api/admin/retention/preview", headers=_headers(users[role]), json={"retention_days": 30})
            apply_resp = client.post("/api/admin/retention/apply", headers=_headers(users[role]), json={"retention_days": 30})
            assert preview.status_code == 403
            assert apply_resp.status_code == 403

        admin_preview = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
        assert admin_preview.status_code == 200


def test_apply_duplicate_path_reference_count(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    first_id, path = world.artifact(run_id, "same.bin", 321, content=b"z" * 321)
    second_id, _same_path = world.artifact(run_id, "same.bin", 321, content=b"z" * 321)

    with TestClient(app) as client:
        preview = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
        assert preview.status_code == 200, preview.text
        assert preview.json()["candidate_artifact_count"] == 2
        assert preview.json()["candidate_path_count"] == 1
        assert preview.json()["duplicate_path_count"] == 1
        assert preview.json()["estimated_bytes"] == 321

        apply_resp = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert apply_resp.status_code == 200, apply_resp.text
    payload = apply_resp.json()
    assert payload["deleted_artifact_count"] == 2
    assert payload["deleted_file_count"] == 1
    assert payload["freed_bytes"] == 321
    assert payload["failed_count"] == 0
    assert not _artifact_exists(first_id)
    assert not _artifact_exists(second_id)
    assert not path.exists()


def test_apply_missing_file_deletes_db(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "missing.log", 40, create=False)

    with TestClient(app) as client:
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deleted_artifact_count"] == 1
    assert payload["missing_file_count"] == 1
    assert payload["deleted_file_count"] == 0
    assert payload["failed_count"] == 0
    assert not _artifact_exists(artifact_id)
    assert not path.exists()


def test_apply_invalid_path_and_directory_fail(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    outside_run = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    outside_path = ROOT / f"_m6_retention_apply_outside_{world.suffix}.log"
    outside_id, _ = world.artifact(outside_run, "", 90, absolute_path=str(outside_path), content=b"o" * 90)

    dir_run = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    dir_id, dir_path = world.artifact(dir_run, "dir_artifact", 100, make_dir=True)

    with TestClient(app) as client:
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["failed_count"] == 2
    assert payload["deleted_artifact_count"] == 0
    assert _artifact_exists(outside_id)
    assert _artifact_exists(dir_id)
    assert outside_path.exists()
    assert dir_path.is_dir()


def test_apply_permission_failure_keeps_db(world: RetentionWorldFactory, users: dict[str, User], monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "denied.log", 50, content=b"x" * 50)

    original = retention_service._remove_file

    def _fail_for(path_arg: Path) -> tuple[str, str]:
        if path_arg == path:
            return "failed", "permission_denied:test"
        return original(path_arg)

    monkeypatch.setattr(retention_service, "_remove_file", _fail_for)
    with TestClient(app) as client:
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["failed_count"] == 1
    assert payload["deleted_artifact_count"] == 0
    assert _artifact_exists(artifact_id)
    assert path.exists()


def test_apply_db_delete_failure_records_and_keeps_db(world: RetentionWorldFactory, users: dict[str, User], monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "db_fail.log", 50, content=b"x" * 50)

    original = retention_service._delete_artifact_record

    def _raise_for(db_arg, artifact_arg: Artifact) -> None:
        if artifact_arg.id == artifact_id:
            raise SQLAlchemyError("db_delete_failed")
        return original(db_arg, artifact_arg)

    monkeypatch.setattr(retention_service, "_delete_artifact_record", _raise_for)
    with TestClient(app) as client:
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["failed_count"] == 1
    assert payload["deleted_artifact_count"] == 0
    assert _artifact_exists(artifact_id)
    assert not path.exists()


def test_apply_protects_active_alert_reverify_open_group_and_running_run(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    candidate_run = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    candidate_id, candidate_path = world.artifact(candidate_run, "candidate.log", 50, content=b"c" * 50)

    running_run = world.run(project_id, "RUNNING")
    running_id, running_path = world.artifact(running_run, "running.log", 50, content=b"r" * 50)

    alert_run = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    alert_id, alert_path = world.artifact(alert_run, "alert.log", 50, content=b"a" * 50)
    world.alert_group(
        project_id,
        group_status="OPEN",
        alert_status="OPEN",
        run_id=alert_run,
        latest_run_id=alert_run,
    )

    group_run = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    group_id, group_path = world.artifact(group_run, "group.log", 50, content=b"g" * 50)
    world.open_group(project_id, group_run)

    reverify_source = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    source_id, source_path = world.artifact(reverify_source, "source.log", 50, content=b"s" * 50)
    reverify_run = world.run(project_id, "PASSED", _now() - timedelta(days=60))
    reverify_id, reverify_path = world.artifact(reverify_run, "reverify.log", 50, content=b"v" * 50)
    world.alert_group(
        project_id,
        group_status="OPEN",
        alert_status="REVERIFY",
        run_id=reverify_source,
        latest_run_id=reverify_source,
        reverify_run_id=reverify_run,
    )

    with TestClient(app) as client:
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deleted_artifact_count"] == 1
    assert payload["failed_count"] == 0
    assert not _artifact_exists(candidate_id)
    assert not candidate_path.exists()
    for artifact_id, path in (
        (running_id, running_path),
        (alert_id, alert_path),
        (group_id, group_path),
        (source_id, source_path),
        (reverify_id, reverify_path),
    ):
        assert _artifact_exists(artifact_id)
        assert path.exists()


def test_apply_rechecks_run_status_changed_after_preview(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "race-run.log", 50, content=b"x" * 50)

    with TestClient(app) as client:
        preview = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
        assert preview.status_code == 200
        assert preview.json()["candidate_artifact_count"] == 1

        db = SessionLocal()
        try:
            run = db.get(ExecutionRun, run_id)
            run.status = "RUNNING"
            run.finished_at = None
            db.commit()
        finally:
            db.close()

        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_artifact_count"] == 0
    assert _artifact_exists(artifact_id)
    assert path.exists()


def test_apply_rechecks_alert_status_changed_after_preview(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "race-alert.log", 50, content=b"x" * 50)

    with TestClient(app) as client:
        preview = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
        assert preview.json()["candidate_artifact_count"] == 1
        world.alert_group(
            project_id,
            group_status="OPEN",
            alert_status="OPEN",
            run_id=run_id,
            latest_run_id=run_id,
        )
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_artifact_count"] == 0
    assert _artifact_exists(artifact_id)
    assert path.exists()


def test_apply_rechecks_open_group_changed_after_preview(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "race-group.log", 50, content=b"x" * 50)

    with TestClient(app) as client:
        preview = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
        assert preview.json()["candidate_artifact_count"] == 1
        world.open_group(project_id, run_id)
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_artifact_count"] == 0
    assert _artifact_exists(artifact_id)
    assert path.exists()


def test_apply_rechecks_reverify_created_after_preview(world: RetentionWorldFactory, users: dict[str, User]) -> None:
    project_id = world.project()
    run_id = world.run(project_id, "FAILED", _now() - timedelta(days=60))
    artifact_id, path = world.artifact(run_id, "race-reverify.log", 50, content=b"x" * 50)

    with TestClient(app) as client:
        preview = client.post("/api/admin/retention/preview", headers=_headers(users["admin"]), json={"retention_days": 30})
        assert preview.json()["candidate_artifact_count"] == 1
        world.alert_group(
            project_id,
            group_status="OPEN",
            alert_status="REVERIFY",
            run_id=run_id,
            latest_run_id=run_id,
            reverify_run_id=run_id,
        )
        response = client.post("/api/admin/retention/apply", headers=_headers(users["admin"]), json={"retention_days": 30})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_artifact_count"] == 0
    assert _artifact_exists(artifact_id)
    assert path.exists()
