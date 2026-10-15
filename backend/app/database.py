"""SQLAlchemy engine, session factory and schema bootstrap.

Schema evolution is handled by a small explicit migration list rather than
`create_all` alone (which never alters existing tables). Each step is
idempotent, so `init_db()` can run on every start. A one-off backup of the
SQLite file is taken before the first schema change of a run.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_settings.data_path.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    _settings.resolved_database_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)

# Additive migrations: (table, column, DDL type/default).
ADDITIVE_MIGRATIONS: list[tuple[str, str, str]] = [
    ("assets", "analyzed_fingerprint", "VARCHAR(128) DEFAULT ''"),
    ("assets", "analyzed_detect_config", "VARCHAR(255) DEFAULT ''"),
    ("assets", "analyzed_at", "DATETIME"),
    ('assets', 'analyzed_source_signature', "VARCHAR(128) DEFAULT ''"),
    ("segments", "detected_start_sec", "FLOAT DEFAULT 0"),
    ("segments", "detected_end_sec", "FLOAT DEFAULT 0"),
    ("segments", "is_trimmed", "BOOLEAN DEFAULT 0"),
    ("jobs", "failures", "JSON DEFAULT '{}'"),
    ('review_requests', 'supersedes_request_id', "VARCHAR(36) DEFAULT ''"),
    ('review_requests', 'superseded_by_request_id', "VARCHAR(36) DEFAULT ''"),
    ('review_requests', 'disposition_at', 'DATETIME'),
    ('segment_motion_reviews', 'input_signature', "VARCHAR(128) DEFAULT ''"),
]


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _backup_sqlite_file() -> str | None:
    """Take a consistent snapshot including committed WAL pages."""
    url = _settings.resolved_database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix) :]
    from pathlib import Path

    db_path = Path(path)
    if not db_path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = db_path.with_name(f"{db_path.stem}.pre-migration-{stamp}.bak")
    try:
        with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(backup)) as dest:
            source.backup(dest)
    except (OSError, sqlite3.Error):
        logger.exception("Could not back up database before migration")
        raise
    return str(backup)


def _apply_migrations() -> list[str]:
    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in ADDITIVE_MIGRATIONS:
            try:
                existing = _existing_columns(conn, table)
            except Exception:  # noqa: BLE001 - table may not exist yet
                continue
            if not existing or column in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            applied.append(f"{table}.{column}")
    return applied


def init_db() -> None:
    # Import models so they register on Base.metadata before create_all.
    from . import models  # noqa: F401

    # create_all also mutates an old schema: back up BEFORE adding any table.
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    pending = bool(set(Base.metadata.tables) - existing_tables)
    for table, column, _ddl in ADDITIVE_MIGRATIONS:
        if not inspector.has_table(table):
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            pending = True
            break
    if pending:
        backup = _backup_sqlite_file() if existing_tables else None
        if backup:
            logger.info("Database backed up to %s before migration", backup)
    Base.metadata.create_all(bind=engine)
    if pending:
        applied = _apply_migrations()
        if applied:
            logger.info("Applied schema migrations: %s", ", ".join(applied))
        _legacy_backfill(applied)
    _backfill_motion_verdicts()


def _backfill_motion_verdicts() -> None:
    """Retain old judgements without inventing missing input provenance."""
    from sqlalchemy import select
    from .models import SegmentMotionReview, SegmentMotionVerdict, new_id

    with Session(engine) as session:
        begin_write(session)
        migrated = select(SegmentMotionVerdict.motion_review_id)
        rows = session.scalars(select(SegmentMotionReview).where(
            SegmentMotionReview.human_verdict != "",
            SegmentMotionReview.id.not_in(migrated),
        )).all()
        for row in rows:
            session.add(SegmentMotionVerdict(
                id=new_id(), project_id=row.project_id, asset_id=row.asset_id,
                segment_id=row.segment_id, motion_review_id=row.id,
                input_signature="", evidence_signature="",
                verdict=row.human_verdict, note=row.human_note,
                created_at=row.human_at or row.updated_at or row.created_at,
                legacy_review_id=row.id,
            ))
        session.commit()


def _legacy_backfill(applied: list[str] | None = None) -> None:
    """Backfill only newly added bounds; an existing detected zero is valid."""
    added = set(applied or [])
    with engine.begin() as conn:
        if 'assets.analyzed_source_signature' in added:
            # Recover only from a checkpoint tied to the exact analyzed digest;
            # never infer that the current scanned source was also the detected one.
            from .models import Asset, Job
            from sqlalchemy import select
            signatures = {}
            for project_id, token in conn.execute(select(Job.project_id, Job.resume_token).where(Job.job_type == 'detect')):
                for item in (token or {}).get('items', []):
                    if item.get('quick_signature'):
                        signatures[(project_id, item.get('key'), item.get('scan_signature'))] = item['quick_signature']
            for aid, pid, digest in conn.execute(select(Asset.id, Asset.project_id, Asset.analyzed_fingerprint)):
                signature = signatures.get((pid, aid, digest))
                if signature:
                    conn.execute(text('UPDATE assets SET analyzed_source_signature=:signature WHERE id=:aid'),
                                 {'signature': signature, 'aid': aid})
        for column, source in (("detected_start_sec", "start_sec"),
                               ("detected_end_sec", "end_sec")):
            if f"segments.{column}" in added:
                conn.execute(text(f"UPDATE segments SET {column} = {source}"))


def begin_write(session: Session) -> None:
    """Acquire SQLite's writer reservation before reading rows to edit.

    BEGIN IMMEDIATE serializes short commit/annotation transactions in WAL mode.
    No row-level FOR UPDATE assumptions; extraction/hashing never hold this lock.
    """
    session.execute(text("BEGIN IMMEDIATE"))


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
