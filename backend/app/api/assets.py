"""Asset listing / detail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Asset, Segment
from ..schemas import AssetListItem, AssetListOut, AssetOut
from .deps import get_db

router = APIRouter(tags=["assets"])


@router.get("/projects/{project_id}/assets", response_model=AssetListOut)
def list_assets(
    project_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    status: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Asset).where(Asset.project_id == project_id)
    if status:
        stmt = stmt.where(Asset.scan_status == status)
    if q:
        stmt = stmt.where(Asset.display_name.ilike(f"%{q}%"))

    total = db.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0
    rows = db.scalars(
        stmt.order_by(Asset.display_name).offset(offset).limit(limit)
    ).all()

    segment_counts: dict[str, int] = {}
    ids = [r.id for r in rows]
    if ids:
        cnt = db.execute(
            select(Segment.asset_id, func.count())
            .where(Segment.asset_id.in_(ids))
            .group_by(Segment.asset_id)
        ).all()
        segment_counts = {asset_id: int(c) for asset_id, c in cnt}

    items = [
        AssetListItem(
            id=r.id,
            display_name=r.display_name,
            source_path=r.source_path,
            file_size_bytes=r.file_size_bytes,
            duration_sec=r.duration_sec,
            width=r.width,
            height=r.height,
            scan_status=r.scan_status,
            error_code=r.error_code,
            segment_count=segment_counts.get(r.id, 0),
        )
        for r in rows
    ]
    return AssetListOut(items=items, total=total, offset=offset, limit=limit)


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset
