"""One current-reference contract for review admission, execution and commit."""
from __future__ import annotations

import math
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Asset, Segment
from .checkpoints import SourceChanged, source_info

EPSILON_SEC = 1e-3
MIN_REVIEW_SEC = 0.2
INVALID_SEGMENT_STATUSES = ('needs_review', 'archived', 'invalid')


class ReviewValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_range(start, end, lower, upper, duration) -> tuple[float, float]:
    try:
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValueError()
        start, end, lower, upper, duration = map(float, (start, end, lower, upper, duration))
    except (TypeError, ValueError, OverflowError):
        raise ReviewValidationError('INVALID_TIME_RANGE', '时间必须是有限数字') from None
    if not all(math.isfinite(x) for x in (start, end, lower, upper, duration)):
        raise ReviewValidationError('INVALID_TIME_RANGE', '时间必须是有限数字')
    if start < 0 or end - start < MIN_REVIEW_SEC - EPSILON_SEC:
        raise ReviewValidationError('INVALID_TIME_RANGE', '入点须非负，出点须晚于入点且区间至少 0.2 秒')
    if end > duration + EPSILON_SEC:
        raise ReviewValidationError('RANGE_OUTSIDE_ASSET', '请求超出素材时长，请重新扫描并选择有效区间')
    if start < lower - EPSILON_SEC or end > upper + EPSILON_SEC or upper <= lower:
        raise ReviewValidationError('RANGE_OUTSIDE_TRIM', '请求超出最新镜头裁剪，请重新选择当前有效区间')
    # Normalize only floating-point tolerance, never extend the requested clip.
    return round(max(start, lower), 3), round(min(end, upper, duration), 3)


def validate_review(s: Session, project_id: str, asset_id: str, segment_id: str,
                    start, end) -> tuple[Asset, Segment, float, float]:
    if not isinstance(asset_id, str) or not isinstance(segment_id, str):
        raise ReviewValidationError('INVALID_REQUEST', '素材和镜头 ID 必须是字符串')
    asset, segment = s.get(Asset, asset_id), s.get(Segment, segment_id)
    if asset is None:
        raise ReviewValidationError('ASSET_NOT_FOUND', '素材不存在，请重新扫描')
    if segment is None:
        raise ReviewValidationError('SEGMENT_NOT_FOUND', '镜头不存在，请选择当前镜头')
    if asset.project_id != project_id or segment.project_id != project_id:
        raise ReviewValidationError('PROJECT_MISMATCH', '素材或镜头不属于当前项目')
    if segment.asset_id != asset.id:
        raise ReviewValidationError('SEGMENT_ASSET_MISMATCH', '镜头不属于所选素材')
    if segment.status in INVALID_SEGMENT_STATUSES:
        raise ReviewValidationError('SEGMENT_NEEDS_REVIEW', '镜头已失效或需要复核，请人工选择当前有效镜头')
    if asset.scan_status in ('missing', 'failed', 'invalid') or asset.error_code == 'source_changed':
        raise ReviewValidationError('SOURCE_CHANGED', '素材不可用或已变化，请重新扫描/检测')
    start, end = validate_range(start, end, segment.start_sec, segment.end_sec, asset.duration_sec)
    return asset, segment, start, end


def review_revision(request, asset, segment) -> tuple:
    return (request.asset_id, request.segment_id, request.start_sec, request.end_sec,
            request.status, request.disposition_at, asset.source_path, asset.duration_sec, asset.quick_fingerprint,
            asset.analyzed_fingerprint, asset.analyzed_source_signature, segment.start_sec, segment.end_sec,
            segment.detected_start_sec, segment.detected_end_sec, segment.is_trimmed, segment.status)


def verified_source(asset: Asset) -> dict:
    try:
        info = source_info(Path(asset.source_path))
    except (OSError, SourceChanged) as exc:
        raise ReviewValidationError('SOURCE_CHANGED', '源文件无法读取或已变化，请重新扫描/检测') from exc
    if ((asset.file_size_bytes and info['size'] != asset.file_size_bytes)
            or (asset.mtime_ns and info['mtime_ns'] != asset.mtime_ns)
            or (asset.quick_fingerprint and info['quick_fingerprint'] != asset.quick_fingerprint)
            or ((asset.analyzed_source_signature or asset.analyzed_fingerprint)
                and (asset.analyzed_source_signature or asset.analyzed_fingerprint) != info['quick_signature'])):
        raise ReviewValidationError('SOURCE_CHANGED', '源文件与扫描/检测记录不一致，请重新扫描/检测')
    return info
