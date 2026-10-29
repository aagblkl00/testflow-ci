from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..models import ENVIRONMENT_TYPES, Environment, Project, User
from ..schemas import EnvironmentIn, EnvironmentOutWithMaskedVariables, ProjectIn, ProjectOut
from ..utils.redact import redact_mapping

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ProjectOut]:
    rows = db.execute(select(Project).order_by(Project.created_at)).scalars().all()
    return [ProjectOut.model_validate(item) for item in rows]


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> ProjectOut:
    exists = db.execute(select(Project).where(Project.name == payload.name.strip())).scalars().first()
    if exists is not None:
        raise HTTPException(status_code=409, detail="项目名称已存在")
    project = Project(name=payload.name.strip(), description=payload.description, repository_url=payload.repository_url)
    db.add(project)
    db.commit()
    db.refresh(project)
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectOut.model_validate(project)


@router.put("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    project.name = payload.name.strip()
    project.description = payload.description
    project.repository_url = payload.repository_url
    db.commit()
    db.refresh(project)
    return ProjectOut.model_validate(project)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    db.delete(project)
    db.commit()


def _masked_environment(environment: Environment) -> EnvironmentOutWithMaskedVariables:
    payload = EnvironmentOutWithMaskedVariables.model_validate(environment)
    payload.variables = redact_mapping(environment.variables or {})  # type: ignore[assignment]
    return payload


@router.get("/environments", response_model=list[EnvironmentOutWithMaskedVariables])
def list_environments(project_id: str | None = None, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(Environment).order_by(Environment.created_at)
    if project_id:
        query = query.where(Environment.project_id == project_id)
    rows = db.execute(query).scalars().all()
    return [_masked_environment(item) for item in rows]


@router.post("/environments", response_model=EnvironmentOutWithMaskedVariables, status_code=201)
def create_environment(payload: EnvironmentIn, _: User = Depends(require_writer), db: Session = Depends(get_db)):
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    env_type = (payload.environment_type or "TEST").upper()
    if env_type not in ENVIRONMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"environment_type 必须是 {ENVIRONMENT_TYPES} 之一")
    environment = Environment(
        project_id=payload.project_id,
        name=payload.name.strip(),
        base_url=payload.base_url.strip(),
        variables=payload.variables or {},
        browser=payload.browser or "chromium",
        environment_type=env_type,
        allowed_hosts=payload.allowed_hosts or [],
        read_only=payload.read_only,
    )
    db.add(environment)
    db.commit()
    db.refresh(environment)
    return _masked_environment(environment)


@router.put("/environments/{environment_id}", response_model=EnvironmentOutWithMaskedVariables)
def update_environment(environment_id: str, payload: EnvironmentIn, _: User = Depends(require_writer), db: Session = Depends(get_db)):
    environment = db.get(Environment, environment_id)
    if environment is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    environment.name = payload.name.strip()
    environment.base_url = payload.base_url.strip()
    environment.variables = payload.variables or {}
    environment.browser = payload.browser or "chromium"
    environment.environment_type = (payload.environment_type or "TEST").upper()
    environment.allowed_hosts = payload.allowed_hosts or []
    environment.read_only = payload.read_only
    db.commit()
    db.refresh(environment)
    return _masked_environment(environment)


@router.delete("/environments/{environment_id}", status_code=204)
def delete_environment(environment_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    environment = db.get(Environment, environment_id)
    if environment is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    db.delete(environment)
    db.commit()
