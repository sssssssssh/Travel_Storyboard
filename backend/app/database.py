"""SQLAlchemy engine, session factory and schema bootstrap.

Schema evolution is handled by a small explicit migration list rather than
`create_all` alone (which never alters existing tables). Each step is
idempotent, so `init_db()` can run on every start. A one-off backup of the
SQLite file is taken before the first schema change of a run.
"""

from __future__ import annotations

import logging
import shutil
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
    connect_args={"check_same_thread": False},
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
    ("segments", "detected_start_sec", "FLOAT DEFAULT 0"),
    ("segments", "detected_end_sec", "FLOAT DEFAULT 0"),
    ("segments", "is_trimmed", "BOOLEAN DEFAULT 0"),
    ("jobs", "failures", "JSON DEFAULT '{}'"),
]


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _backup_sqlite_file() -> str | None:
    """Copy the SQLite file once before applying pending migrations."""
    url = _settings.resolved_database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix) :]
    from pathlib import Path

    db_path = Path(path)
    if not db_path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.stem}.pre-migration-{stamp}.bak")
    try:
        shutil.copy2(db_path, backup)
    except OSError:
        logger.warning("Could not back up database before migration", exc_info=True)
        return None
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

    Base.metadata.create_all(bind=engine)

    # Detect pending work before touching anything so we only back up when needed.
    inspector = inspect(engine)
    pending = False
    for table, column, _ddl in ADDITIVE_MIGRATIONS:
        if not inspector.has_table(table):
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            pending = True
            break
    if pending:
        backup = _backup_sqlite_file()
        if backup:
            logger.info("Database backed up to %s before migration", backup)
        applied = _apply_migrations()
        if applied:
            logger.info("Applied schema migrations: %s", ", ".join(applied))
        _legacy_backfill()


def _legacy_backfill() -> None:
    """Populate the new immutable bounds for rows created before the split."""
    with engine.begin() as conn:
        if not _existing_columns(conn, "segments"):
            return
        conn.execute(
            text(
                "UPDATE segments SET detected_start_sec = start_sec "
                "WHERE detected_start_sec IS NULL OR detected_start_sec = 0"
            )
        )
        conn.execute(
            text(
                "UPDATE segments SET detected_end_sec = end_sec "
                "WHERE detected_end_sec IS NULL OR detected_end_sec = 0"
            )
        )


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
