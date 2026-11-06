"""TSB-367-R1 — add legacy_plan_migrations as a FORWARD revision (R1-04).

Revision ID: 0003_legacy_plan_migrations
Revises: 0002_plan_operations
Create Date: 2026-11-06

Why this revision exists
------------------------
``legacy_plan_migrations`` was originally introduced by editing the already
published ``0001``. That works for a fresh database and for ``init_db()``, but a
database that had **already run** the old ``0001`` never re-runs it — so it stayed
permanently without the table while ``init_db()`` had it. The two bootstrap paths
silently diverged.

``0001`` has now been restored to the structure it actually shipped. This revision
is the forward-only fix: it runs for every database that upgrades to head,
regardless of what it was stamped at before.

Safety
------
Everything here is ``IF NOT EXISTS``-guarded, so it is a no-op for a database
that already has the table (the common case after ``init_db()``). It never alters
an existing table, never drops data, and its downgrade removes only this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_legacy_plan_migrations"
down_revision = "0002_plan_operations"
branch_labels = None
depends_on = None

TABLE = "legacy_plan_migrations"
INDEX = "ix_legacy_plan_migrations_project_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if TABLE in existing:
        # Already present (created by init_db or a previous upgrade). Nothing to do
        # beyond making sure the index exists, since old databases may lack it.
        indexes = {ix["name"] for ix in inspector.get_indexes(TABLE)}
        if INDEX not in indexes:
            op.create_index(INDEX, TABLE, ["project_id"])
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("legacy_shot_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", name="uq_legacy_migration_project"),
    )
    op.create_index(INDEX, TABLE, ["project_id"])


def downgrade() -> None:
    """Remove only this table. Storyboard and legacy plan data are untouched."""
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if TABLE in existing:
        op.drop_table(TABLE)
