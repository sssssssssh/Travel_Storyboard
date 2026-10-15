"""Short SQLite writer boundary shared by both vision publishers (TSB-316)."""
from ...models import Asset, Job, Segment
from ..runner import raise_if_cancelled_in_session
from .errors import ModelError, SOURCE_CHANGED
from .verdicts import source_identity


def validate_publication(session, job_id, project_id, snapshot, job_type):
    raise_if_cancelled_in_session(session, job_id)
    job = session.get(Job, job_id)
    if (job is None or job.project_id != project_id or job.job_type != job_type
            or job.status != 'running'):
        raise ModelError('JOB_INVALID', 'job is not allowed to publish in this project')
    sid = snapshot['segment_id']
    segment = session.get(Segment, sid)
    asset = session.get(Asset, snapshot['asset_id'])
    if (segment is None or asset is None or segment.project_id != project_id
            or asset.project_id != project_id or segment.asset_id != asset.id
            or segment.status in ('needs_review', 'archived', 'invalid')
            or segment.user_rating == 'exclude'
            or asset.scan_status in ('failed', 'missing', 'invalid')):
        raise ModelError('SEGMENT_INVALID', 'segment or source is no longer eligible')
    if (segment.start_sec != snapshot['start_sec'] or segment.end_sec != snapshot['end_sec']
            or not snapshot.get('source_signature')
            or source_identity(asset) != snapshot['source_signature']):
        raise ModelError(SOURCE_CHANGED, 'source or interval changed before publication')
