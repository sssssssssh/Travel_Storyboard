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
    # TSB-371: representative frames for the rating page (a frame id, not a
    # result id -- the image route is keyed by frame).
    ("evaluation_items", "frame_ids", "JSON"),
    ("evaluation_items", "readings_json", "JSON"),
    ("evaluation_ratings", "claim_verdicts", "JSON"),
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
    # TSB-366: relational invariants that a per-column CHECK cannot express.
    _create_storyboard_triggers()
    # TSB-367: one applied operation per produced version.
    _create_plan_operation_indexes()
    if pending:
        applied = _apply_migrations()
        if applied:
            logger.info("Applied schema migrations: %s", ", ".join(applied))
        _legacy_backfill(applied)
    _backfill_motion_verdicts()


#: TSB-366 — cross-row invariants the storyboard tables depend on.
#:
#: Each of these is a rule the task book states as a *guarantee* ("parent 必须属于
#: 同一 board"、"历史快照不可原地覆盖"、"current_version_id 必须属于该 board").
#: SQLAlchemy cannot express them as column constraints, and enforcing them only
#: in the service layer would mean a direct INSERT could bypass them. SQLite
#: triggers make the guarantee true for every writer, including a future one that
#: forgets to call the service.
_STORYBOARD_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "trg_storyboard_version_parent_same_board",
        """
        CREATE TRIGGER IF NOT EXISTS trg_storyboard_version_parent_same_board
        BEFORE INSERT ON storyboard_versions
        WHEN NEW.parent_version_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'parent_version_id must belong to the same board')
            WHERE NOT EXISTS (
                SELECT 1 FROM storyboard_versions p
                WHERE p.id = NEW.parent_version_id AND p.board_id = NEW.board_id
            );
        END
        """,
    ),
    (
        "trg_storyboard_current_version_same_board",
        """
        CREATE TRIGGER IF NOT EXISTS trg_storyboard_current_version_same_board
        BEFORE UPDATE OF current_version_id ON storyboards
        WHEN NEW.current_version_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'current_version_id must belong to this board')
            WHERE NOT EXISTS (
                SELECT 1 FROM storyboard_versions v
                WHERE v.id = NEW.current_version_id AND v.board_id = NEW.id
            );
        END
        """,
    ),
    # Snapshot columns are write-once. A version row may gain nothing but the
    # pointer bookkeeping, so history cannot be rewritten in place (TSB-366 §2.2).
    (
        "trg_storyboard_version_immutable",
        """
        CREATE TRIGGER IF NOT EXISTS trg_storyboard_version_immutable
        BEFORE UPDATE ON storyboard_versions
        WHEN NEW.shots IS NOT OLD.shots
          OR NEW.sections IS NOT OLD.sections
          OR NEW.brief IS NOT OLD.brief
          OR NEW.constraints IS NOT OLD.constraints
          OR NEW.validation_summary IS NOT OLD.validation_summary
          OR NEW.version_no IS NOT OLD.version_no
          OR NEW.parent_version_id IS NOT OLD.parent_version_id
          OR NEW.schema_version IS NOT OLD.schema_version
        BEGIN
            SELECT RAISE(ABORT, 'storyboard version snapshots are immutable');
        END
        """,
    ),
    # --- TSB-367: PlanOperation invariants --------------------------------
    (
        "trg_plan_operation_base_same_board",
        """
        CREATE TRIGGER IF NOT EXISTS trg_plan_operation_base_same_board
        BEFORE INSERT ON plan_operations
        BEGIN
            SELECT RAISE(ABORT, 'base_version_id must belong to this board')
            WHERE NOT EXISTS (
                SELECT 1 FROM storyboard_versions v
                WHERE v.id = NEW.base_version_id AND v.board_id = NEW.board_id
            );
        END
        """,
    ),
    (
        "trg_plan_operation_status_valid",
        """
        CREATE TRIGGER IF NOT EXISTS trg_plan_operation_status_valid
        BEFORE INSERT ON plan_operations
        BEGIN
            SELECT RAISE(ABORT, 'invalid plan operation status')
            WHERE NEW.status NOT IN
                ('proposed','applied','rejected','stale','failed');
        END
        """,
    ),
    #: Every non-``proposed`` state is terminal (R1-02 contract): an operation that
    #: was applied, rejected, found stale or failed must never be rewritten, so the
    #: recorded outcome always matches what the caller was told.
    (
        "trg_plan_operation_terminal_immutable",
        """
        CREATE TRIGGER IF NOT EXISTS trg_plan_operation_terminal_immutable
        BEFORE UPDATE ON plan_operations
        WHEN OLD.status IN ('applied','rejected','stale','failed')
        BEGIN
            SELECT RAISE(ABORT, 'a terminal plan operation cannot change');
        END
        """,
    ),
    #: ``applied`` must name a real version of the same board — enforced on
    #: INSERT as well as UPDATE (R1-02).
    (
        "trg_plan_operation_applied_has_result",
        """
        CREATE TRIGGER IF NOT EXISTS trg_plan_operation_applied_has_result
        BEFORE UPDATE ON plan_operations
        WHEN NEW.status = 'applied'
        BEGIN
            SELECT RAISE(ABORT, 'applied requires a result_version_id in this board')
            WHERE NEW.result_version_id IS NULL
               OR NOT EXISTS (
                    SELECT 1 FROM storyboard_versions v
                    WHERE v.id = NEW.result_version_id
                      AND v.board_id = NEW.board_id
                  );
        END
        """,
    ),
    (
        "trg_plan_operation_applied_has_result_insert",
        """
        CREATE TRIGGER IF NOT EXISTS trg_plan_operation_applied_has_result_insert
        BEFORE INSERT ON plan_operations
        WHEN NEW.status = 'applied'
        BEGIN
            SELECT RAISE(ABORT, 'applied requires a result_version_id in this board')
            WHERE NEW.result_version_id IS NULL
               OR NOT EXISTS (
                    SELECT 1 FROM storyboard_versions v
                    WHERE v.id = NEW.result_version_id
                      AND v.board_id = NEW.board_id
                  );
        END
        """,
    ),
    (
        "trg_storyboard_version_restore_target_same_board",
        """
        CREATE TRIGGER IF NOT EXISTS trg_storyboard_version_restore_target_same_board
        BEFORE INSERT ON storyboard_versions
        WHEN NEW.restore_of_version_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'restore_of_version_id must belong to the same board')
            WHERE NOT EXISTS (
                SELECT 1 FROM storyboard_versions r
                WHERE r.id = NEW.restore_of_version_id AND r.board_id = NEW.board_id
            );
        END
        """,
    ),
)


#: TSB-367: at most one applied operation may claim a given result version.
#:
#: A partial unique index expresses this exactly; a plain UNIQUE would also count
#: the many `NULL` results of non-applied operations, which SQLite ignores for
#: uniqueness anyway but the partial form documents the intent.
_PLAN_OPERATION_INDEXES: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_operation_applied_result
    ON plan_operations(result_version_id)
    WHERE result_version_id IS NOT NULL
    """,
)


def _create_plan_operation_indexes() -> None:
    """Install the PlanOperation indexes (idempotent)."""
    with engine.begin() as conn:
        for ddl in _PLAN_OPERATION_INDEXES:
            conn.execute(text(ddl))


def _create_storyboard_triggers() -> None:
    """Install the storyboard invariants (idempotent)."""
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            ).fetchall()
        }
        for name, ddl in _STORYBOARD_TRIGGERS:
            if name in existing:
                continue
            conn.execute(text(ddl))


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
