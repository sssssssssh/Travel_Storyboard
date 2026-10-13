"""Stable per-object checkpoints stored in existing Job JSON columns.

No schema change: old positional tokens are conservatively rechecked once.
A quick signature reads 192 KiB at most, and is not a full-content guarantee.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..database import session_scope
from ..models import Job
from .runner import update_job


def source_signature(path: Path, *, full_hash: bool = False) -> str:
    from .indexer import quick_fingerprint
    path = path.resolve()
    stat = path.stat()
    parts = [str(path), stat.st_size, stat.st_mtime_ns,
             quick_fingerprint(path, stat.st_size)]
    if full_hash:
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        parts.append(h.hexdigest())
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def manifest_entry(key: str, source: str, *, full_hash: bool = False) -> dict:
    try:
        signature = source_signature(Path(source), full_hash=full_hash)
        error = ''
    except OSError as exc:
        signature, error = 'missing', str(exc)
    return {'key': key, 'source': source, 'scan_signature': signature, 'source_error': error,
            'status': 'pending'}


def reconcile(job_id: str, current: list[dict]) -> tuple[list[dict], dict]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        token = dict(job.resume_token or {})
        snapshot = dict(job.config_snapshot or {})
    previous = token.get('items', snapshot.get('items', []))
    by_key = {item['key']: dict(item) for item in previous}
    changed = set(token.get('source_changed_items', []))
    current_keys = {item['key'] for item in current}
    for item in current:
        old = by_key.get(item['key'])
        if old is None or old['scan_signature'] != item['scan_signature']:
            if old is not None or previous:
                changed.add(item['key'])
            by_key[item['key']] = dict(item)
        elif old['status'] == 'failed':
            old['status'] = 'pending'  # retry failure, collector resolves on success
    for key in set(by_key) - current_keys:
        item = by_key[key]
        changed.add(key)
        item.update(status='pending', scan_signature='missing', source_error='源文件已变化或移除')
    items = sorted(by_key.values(), key=lambda item: item['key'])
    token.update(items=items, source_changed_items=sorted(changed))
    if changed:
        token['warning'] = '源文件已变化：新增、移除或内容变化的处理项已重新检查'
    if not snapshot.get('items'):
        snapshot['items'] = current
        update_job(job_id, config_snapshot=snapshot)
    persist(job_id, items, token)
    return items, token


def persist(job_id: str, items: list[dict], token: dict) -> None:
    token = {**token, 'items': [dict(item) for item in items]}
    finished = ('completed', 'failed', 'skipped')
    stats = {'total': len(items), 'done': sum(i['status'] in finished for i in items),
             'failed': sum(i['status'] == 'failed' for i in items),
             'skipped': sum(i['status'] == 'skipped' for i in items),
             'source_changed': len(token.get('source_changed_items', []))}
    update_job(job_id, stats=stats, resume_token=token)
