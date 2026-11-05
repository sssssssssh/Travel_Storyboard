"""TSB-366 — storyboard version tables.

Revision ID: 0001_storyboard_versions
Revises:
Create Date: 2026-11-05

Scope, deliberately narrow: this revision creates **only** the three TSB-366
tables plus the triggers that enforce their cross-row invariants. It does not
touch ``edit_plan_shots`` or any other existing table, so:

* applying it to a populated database is additive and cannot lose user data;
* downgrading it drops only the storyboard tables — the legacy plan table (the
  migration source) is never removed, which is the §3.4 "不得删除迁移前旧计划数据"
  requirement.

Relationship to the runtime path
--------------------------------
The application bootstraps its schema with ``app.database.init_db()``, not with
Alembic. This revision exists for operators who want a reviewable,
upgrade/downgrade-able description of the TSB-366 schema change. Running
``alembic upgrade head`` against a database that was already created by
``init_db`` requires ``alembic stamp`` first (see docs/RELEASE_TSB_366.md); the
revision's ``upgrade`` is therefore written to be idempotent for the tables it
creates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_storyboard_versions"
down_revision = None
branch_labels = None
depends_on = None

#: Mirrors ``app.database._STORYBOARD_TRIGGERS``. Kept in sync by a test
#: (``test_tsb366_schema_ops``) so the two definitions cannot drift.
TRIGGERS: tuple[tuple[str, str], ...] = (
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


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "storyboards" not in existing:
        op.create_table(
            "storyboards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("title", sa.String(200), nullable=False, server_default=""),
            sa.Column("current_version_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("project_id", name="uq_storyboard_one_per_project"),
        )
        op.create_index("ix_storyboards_project_id", "storyboards", ["project_id"])

    if "storyboard_versions" not in existing:
        op.create_table(
            "storyboard_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "board_id",
                sa.String(36),
                sa.ForeignKey("storyboards.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("parent_version_id", sa.String(36), nullable=True),
            sa.Column("schema_version", sa.String(16), nullable=False, server_default="1.0"),
            sa.Column("brief", sa.JSON(), nullable=True),
            sa.Column("sections", sa.JSON(), nullable=True),
            sa.Column("shots", sa.JSON(), nullable=True),
            sa.Column("constraints", sa.JSON(), nullable=True),
            sa.Column("validation_summary", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("actor", sa.String(16), nullable=False, server_default="user"),
            sa.Column("reason", sa.String(32), nullable=False, server_default="manual_edit"),
            sa.Column("job_id", sa.String(36), nullable=True),
            sa.Column("restore_of_version_id", sa.String(36), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=True),
            sa.UniqueConstraint("board_id", "version_no", name="uq_storyboard_version_no"),
            sa.UniqueConstraint(
                "board_id", "idempotency_key", name="uq_storyboard_version_idempotency"
            ),
        )
        op.create_index(
            "ix_storyboard_versions_board_id", "storyboard_versions", ["board_id"]
        )

    # TSB-367 stage 0: init_db has created this table since TSB-366-R1; the
    # operational revision now describes it too, so the two paths agree.
    if "legacy_plan_migrations" not in existing:
        op.create_table(
            "legacy_plan_migrations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("legacy_shot_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("project_id", name="uq_legacy_migration_project"),
        )
        op.create_index("ix_legacy_plan_migrations_project_id",
                        "legacy_plan_migrations", ["project_id"])

    if "storyboard_source_refs" not in existing:
        op.create_table(
            "storyboard_source_refs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "version_id",
                sa.String(36),
                sa.ForeignKey("storyboard_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("shot_id", sa.String(64), nullable=False),
            sa.Column("asset_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("segment_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("source_signature", sa.String(128), nullable=False, server_default=""),
            sa.UniqueConstraint("version_id", "shot_id", name="uq_storyboard_ref_shot"),
        )
        op.create_index(
            "ix_storyboard_source_refs_version_id", "storyboard_source_refs", ["version_id"]
        )
        op.create_index(
            "ix_storyboard_source_refs_asset_id", "storyboard_source_refs", ["asset_id"]
        )
        op.create_index(
            "ix_storyboard_source_refs_segment_id", "storyboard_source_refs", ["segment_id"]
        )

    for _name, ddl in TRIGGERS:
        bind.execute(sa.text(ddl))


def downgrade() -> None:
    """Remove the storyboard tables. The legacy plan table is left untouched.

    Order matters: refs → versions → boards (the FK direction). Triggers go with
    their tables, but are dropped explicitly so a partially-created database
    still ends up clean.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    for name, _ddl in TRIGGERS:
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))

    if "legacy_plan_migrations" in existing:
        op.drop_table("legacy_plan_migrations")
    if "storyboard_source_refs" in existing:
        op.drop_table("storyboard_source_refs")
    if "storyboard_versions" in existing:
        op.drop_table("storyboard_versions")
    if "storyboards" in existing:
        op.drop_table("storyboards")
