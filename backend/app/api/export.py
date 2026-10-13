"""Edit plan and timeline export endpoints (M4)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..models import Project
from ..schemas import EditPlanBuild
from ..services.timeline_export import build_edit_plan, export_csv, export_json, export_otio
from .deps import get_db

router = APIRouter(tags=["export"])


def _require_project(project_id: str, db: Session) -> None:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")


@router.post("/projects/{project_id}/edit-plan")
def save_edit_plan(project_id: str, payload: EditPlanBuild, db: Session = Depends(get_db)):
    _require_project(project_id, db)
    return build_edit_plan(project_id, [s.model_dump() for s in payload.shots])


@router.get("/projects/{project_id}/edit-plan")
def get_edit_plan(project_id: str, db: Session = Depends(get_db)):
    _require_project(project_id, db)
    return export_json(project_id)


@router.get("/projects/{project_id}/export/json")
def download_json(project_id: str, db: Session = Depends(get_db)):
    _require_project(project_id, db)
    data = export_json(project_id)
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="edit_plan.json"'},
    )


@router.get("/projects/{project_id}/export/csv")
def download_csv(project_id: str, db: Session = Depends(get_db)):
    _require_project(project_id, db)
    return Response(
        content=export_csv(project_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="timeline.csv"'},
    )


@router.get("/projects/{project_id}/export/otio")
def download_otio(project_id: str, db: Session = Depends(get_db)):
    _require_project(project_id, db)
    return Response(
        content=export_otio(project_id),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="edit.otio"'},
    )
