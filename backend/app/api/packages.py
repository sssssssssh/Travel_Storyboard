"""Review package listing and download endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReviewPackage
from ..paths import resolve_in_project
from ..schemas import ReviewPackageOut
from .deps import get_db

router = APIRouter(tags=["packages"])


@router.get("/projects/{project_id}/packages", response_model=list[ReviewPackageOut])
def list_packages(project_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(ReviewPackage)
        .where(ReviewPackage.project_id == project_id)
        .order_by(ReviewPackage.created_at.desc())
    ).all()


@router.get("/packages/{package_id}/download")
def download_package(package_id: str, db: Session = Depends(get_db)):
    pkg = db.get(ReviewPackage, package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="Package not found")
    try:
        zip_path = resolve_in_project(pkg.project_id, pkg.package_dir).with_suffix(".zip")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Package path invalid") from exc
    if not zip_path.is_file():
        raise HTTPException(status_code=404, detail="Package zip missing")
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"package_{pkg.version}.zip"
    )
