"""Project CRUD and stats endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Asset, Project, Segment
from ..schemas import ProjectCreate, ProjectDetail, ProjectStats, ProjectUpdate
from .deps import get_db

router = APIRouter(prefix="/projects", tags=["projects"])


def _stats(db: Session, project_id: str) -> ProjectStats:
    asset_count = db.scalar(
        select(func.count()).select_from(Asset).where(Asset.project_id == project_id)
    ) or 0
    succeeded = db.scalar(
        select(func.count())
        .select_from(Asset)
        .where(Asset.project_id == project_id, Asset.scan_status == "succeeded")
    ) or 0
    failed = db.scalar(
        select(func.count())
        .select_from(Asset)
        .where(Asset.project_id == project_id, Asset.scan_status == "failed")
    ) or 0
    segment_count = db.scalar(
        select(func.count()).select_from(Segment).where(Segment.project_id == project_id)
    ) or 0
    duration = db.scalar(
        select(func.coalesce(func.sum(Asset.duration_sec), 0.0)).where(
            Asset.project_id == project_id
        )
    ) or 0.0
    size = db.scalar(
        select(func.coalesce(func.sum(Asset.file_size_bytes), 0)).where(
            Asset.project_id == project_id
        )
    ) or 0
    return ProjectStats(
        asset_count=asset_count,
        asset_succeeded=succeeded,
        asset_failed=failed,
        segment_count=segment_count,
        total_duration_sec=float(duration),
        total_size_bytes=int(size),
    )


def _detail(db: Session, project: Project) -> ProjectDetail:
    return ProjectDetail(
        id=project.id,
        name=project.name,
        source_roots=project.source_roots,
        output_dir=project.output_dir,
        settings=project.settings,
        created_at=project.created_at,
        updated_at=project.updated_at,
        stats=_stats(db, project.id),
    )


@router.get("", response_model=list[ProjectDetail])
def list_projects(db: Session = Depends(get_db)):
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return [_detail(db, p) for p in projects]


@router.post("", response_model=ProjectDetail, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(
        name=payload.name,
        source_roots=payload.source_roots,
        output_dir=payload.output_dir,
        settings=payload.settings,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _detail(db, project)


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _detail(db, project)


@router.patch("/{project_id}", response_model=ProjectDetail)
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    for field in ("name", "source_roots", "output_dir", "settings"):
        value = getattr(payload, field)
        if value is not None:
            setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return _detail(db, project)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    db.delete(project)
    db.commit()
