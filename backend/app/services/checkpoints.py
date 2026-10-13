"""Background source verification and stable checkpoints in existing Job JSON.

Quick signatures sample at most 192 KiB. Full signatures stream once per item
per job and persist the result; strict recheck explicitly pays a second pass.
A resumed incomplete hash restarts that file, never completed unchanged files.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ..database import session_scope
from ..models import Job
from .runner import raise_if_cancelled, update_job


class SourceChanged(RuntimeError):
    code = 'source_changed'


class SourceUnavailable(OSError):
    code = 'source_unavailable'


def file_identity(stat) -> dict:
    return {name: int(getattr(stat, name)) for name in
            ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_dev', 'st_ino')}


def _signature(info: dict, full_sha256: str | None = None) -> str:
    parts = [info['source'], info['size'], info['mtime_ns'], info['quick_fingerprint']]
    if full_sha256 is not None:
        parts.append(full_sha256)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def source_info(path: Path) -> dict:
    from .indexer import quick_fingerprint
    path = path.resolve()
    before = path.stat()
    info = {'source': str(path), 'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
            'identity': file_identity(before),
            'quick_fingerprint': quick_fingerprint(path, before.st_size)}
    if file_identity(path.stat()) != info['identity']:
        raise SourceChanged('源文件已变化：快速校验期间被修改，请再次扫描/检测')
    info['quick_signature'] = _signature(info)
    return info


def read_hash_block(stream, size: int) -> bytes:
    return stream.read(size)


def full_content_hash(path: Path, expected: dict, *, job_id=None, on_progress=None) -> str:
    h = hashlib.sha256()
    read = 0
    last_bytes, last_time = 0, time.monotonic()
    try:
        with path.open('rb') as stream:
            if file_identity(os.fstat(stream.fileno())) != expected['identity']:
                raise SourceChanged('源文件已变化：开始完整校验前被替换')
            while True:
                if job_id:
                    raise_if_cancelled(job_id)
                block = read_hash_block(stream, 1024 * 1024)
                if not block:
                    break
                h.update(block)
                read += len(block)
                if on_progress and (read - last_bytes >= 8 * 1024 * 1024 or time.monotonic() - last_time >= .25):
                    on_progress(read)
                    last_bytes, last_time = read, time.monotonic()
        after = source_info(path)
        if read != expected['size'] or after['identity'] != expected['identity'] or after['quick_signature'] != expected['quick_signature']:
            raise SourceChanged('源文件已变化：完整校验期间被修改，请再次扫描/检测')
        return h.hexdigest()
    finally:
        if on_progress:
            on_progress(read)


def source_signature(path: Path, *, full_hash: bool = False) -> str:
    """Standalone compatibility helper; background tasks use prepared items."""
    info = source_info(path)
    digest = full_content_hash(path, info) if full_hash else None
    return _signature(info, digest)


def manifest_entry(key: str, source: str, *, full_hash: bool = False) -> dict:
    # Full hashing is intentionally deferred even when the manifest mode is full.
    try:
        info = source_info(Path(source))
        error, code = '', ''
    except (OSError, SourceChanged) as exc:
        info = {'source': source, 'size': 0, 'identity': {}, 'quick_signature': 'missing'}
        error, code = str(exc), getattr(exc, 'code', 'source_unavailable')
    return {**info, 'key': key, 'scan_signature': info['quick_signature'],
            'signature_mode': 'full' if full_hash else 'quick',
            'source_error': error, 'error_code': code, 'status': 'pending'}


def reconcile(job_id: str, current: list[dict]) -> tuple[list[dict], dict]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        token, snapshot = dict(job.resume_token or {}), dict(job.config_snapshot or {})
    previous = token.get('items', snapshot.get('items', []))
    by_key = {item['key']: dict(item) for item in previous}
    changed = set(token.get('source_changed_items', []))
    current_keys = {item['key'] for item in current}
    for item in current:
        old = by_key.get(item['key'])
        unchanged = old is not None and old.get('quick_signature', old['scan_signature']) == item['quick_signature']
        # Full results are reused only within this job and with the same quick
        # signature AND filesystem identity (ctime included). New jobs read anew.
        if unchanged and old.get('identity'):
            unchanged = old['identity'] == item['identity']
        if not unchanged:
            if old is not None or previous:
                changed.add(item['key'])
            by_key[item['key']] = dict(item)
        elif old['status'] == 'failed':
            old.update(status='pending', source_error=item['source_error'], error_code=item['error_code'])
    for key in set(by_key) - current_keys:
        changed.add(key)
        by_key[key].update(status='pending', scan_signature='missing', quick_signature='missing',
                           source_error='源文件已变化或移除，请再次扫描/检测', error_code='source_changed')
    items = sorted(by_key.values(), key=lambda item: item['key'])
    token.update(items=items, source_changed_items=sorted(changed))
    if changed:
        token['warning'] = '源文件已变化：新增、移除或内容变化的处理项已重新检查，请复核分析结果'
    token['signature_mode'] = 'full' if snapshot.get('full_hash') else 'quick'
    io = dict(token.get('io', {}))
    io.setdefault('bytes_read', 0)
    io.setdefault('strict_extra_bytes', 0)
    io.setdefault('hash_passes', 0)
    io.setdefault('hash_restarts', 0)
    io['total_bytes'] = sum(i.get('size', 0) for i in items) + io.get('strict_planned_bytes', 0) if snapshot.get('full_hash') else 0
    token['io'] = io
    if not snapshot.get('items'):
        # Frozen first background manifest; mutable per-item state stays in token.
        snapshot['items'] = [dict(i) for i in current]
        update_job(job_id, config_snapshot=snapshot)
    persist(job_id, items, token)
    return items, token


def _hash_item(job_id: str, item: dict, items: list[dict], token: dict, *, strict=False) -> str:
    io = token['io']
    base, extra = io['bytes_read'], io['strict_extra_bytes']
    io['hash_passes'] += 1
    if not strict and item.get('hash_bytes_read') and not item.get('full_sha256'):
        io['hash_restarts'] += 1
    if strict:
        io['strict_planned_bytes'] = io.get('strict_planned_bytes', 0) + item['size']
        io['total_bytes'] += item['size']
    token.update(phase='hash_recheck' if strict else 'hashing', current_file=Path(item['source']).name)
    persist(job_id, items, token)
    def progress(read):
        io['bytes_read'] = base + read
        if strict:
            io['strict_extra_bytes'] = extra + read
        item['hash_bytes_read'] = read
        persist(job_id, items, token)
    return full_content_hash(Path(item['source']), item, job_id=job_id, on_progress=progress)


def prepare_item(job_id: str, item: dict, items: list[dict], token: dict, config: dict) -> None:
    raise_if_cancelled(job_id)
    if item.get('source_error'):
        raise SourceChanged(item['source_error']) if item.get('error_code') == 'source_changed' else SourceUnavailable(item['source_error'])
    verify_source(item)
    if config.get('full_hash') and not item.get('full_sha256'):
        digest = _hash_item(job_id, item, items, token)
        item['full_sha256'] = digest
        item['scan_signature'] = _signature(item, digest)
        persist(job_id, items, token)
    token.update(phase='processing', current_file=Path(item['source']).name)
    persist(job_id, items, token)


def verify_source(item: dict) -> None:
    now = source_info(Path(item['source']))
    if now['quick_signature'] != item.get('quick_signature') or now['identity'] != item.get('identity'):
        raise SourceChanged('源文件已变化：处理期间被修改，请再次扫描/检测；旧分析版本已保留')


def verify_after_processing(job_id: str, item: dict, items: list[dict], token: dict, config: dict) -> None:
    verify_source(item)
    if config.get('strict_full_hash'):
        digest = _hash_item(job_id, item, items, token, strict=True)
        if digest != item['full_sha256']:
            raise SourceChanged('源文件已变化：严格复核不一致，旧分析版本已保留')
    raise_if_cancelled(job_id)


def fail_item(item: dict, exc: Exception, token: dict) -> None:
    source_io = isinstance(exc, OSError) and getattr(exc, 'filename', None) == item['source']
    item.update(status='failed', error_code=getattr(exc, 'code', 'source_unavailable' if source_io else 'io_error' if isinstance(exc, OSError) else 'processing_failed'),
                error_message=str(exc))
    if item['error_code'] in ('source_changed', 'source_unavailable'):
        item.pop('full_sha256', None)
        token['warning'] = '源文件已变化或无法读取，旧分析保留；请处理原文件后再次扫描/检测'


def persist(job_id: str, items: list[dict], token: dict) -> None:
    saved = {**token, 'items': [dict(item) for item in items]}
    finished = ('completed', 'failed', 'skipped')
    stats = {'total': len(items), 'done': sum(i['status'] in finished for i in items),
             'failed': sum(i['status'] == 'failed' for i in items),
             'skipped': sum(i['status'] == 'skipped' for i in items),
             'source_changed': len(token.get('source_changed_items', [])),
             'forced_reprobe': sum(bool(i.get('forced_reprobe')) for i in items),
             'skipped_unchanged': sum(i['status'] == 'skipped' for i in items),
             'signature_mode': token.get('signature_mode', 'quick'),
             'phase': 'complete' if items and all(i['status'] in finished for i in items) else token.get('phase', 'enumerating'), 'current_file': token.get('current_file', ''),
             'processed_files': sum(bool(i.get('full_sha256')) for i in items) if token.get('signature_mode') == 'full' else sum(i['status'] in finished for i in items),
             'total_files': len(items), **token.get('io', {})}
    update_job(job_id, stats=stats, resume_token=saved)
