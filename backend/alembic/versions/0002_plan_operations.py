"""TSB-367 — plan_operations table, its invariants and the applied-result index.

Revision ID: 0002_plan_operations
Revises: 0001_storyboard_versions
Create Date: 2026-11-05

Scope: adds **only** what TSB-367 introduces. It does not touch any existing
table, so applying it to a populated database is additive.

Relationship to the runtime path
--------------------------------
As with 0001, ``app.database.init_db()`` is the runtime bootstrap. This revision
exists so an operator can review, upgrade and downgrade the TSB-367 schema. The
trigger set is asserted equal to ``app.database._STORYBOARD_TRIGGERS`` by
``tests/test_tsb366_schema_ops.py``, so the two definitions cannot drift.

Downgrade drops only ``plan_operations`` — never the storyboard tables, and never
the legacy ``edit_plan_shots`` data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_plan_operations"
down_revision = "0001_storyboard_versions"
branch_labels = None
depends_on = None

#: Mirrors the TSB-367 entries of ``app.database._STORYBOARD_TRIGGERS``.
PLAN_OPERATION_TRIGGERS: tuple[tuple[str, str], ...] = (
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
    (
        "trg_plan_operation_terminal_immutable",
        """
        CREATE TRIGGER IF NOT EXISTS trg_plan_operation_terminal_immutable
        BEFORE UPDATE ON plan_operations
        WHEN OLD.status IN ('applied','rejected')
        BEGIN
            SELECT RAISE(ABORT, 'a terminal plan operation cannot change');
        END
        """,
    ),
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
)

#: At most one applied operation may claim a produced version.
APPLIED_RESULT_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_operation_applied_result
ON plan_operations(result_version_id)
WHERE result_version_id IS NOT NULL
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "plan_operations" not in existing:
        op.create_table(
            "plan_operations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column(
                "board_id",
                sa.String(36),
                sa.ForeignKey("storyboards.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("base_version_id", sa.String(36), nullable=False),
            sa.Column("message_id", sa.String(36), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("operations", sa.JSON(), nullable=True),
            sa.Column("diff", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("result_version_id", sa.String(36), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
            sa.Column("error_detail", sa.JSON(), nullable=True),
            sa.Column("actor", sa.String(16), nullable=False, server_default="user"),
            sa.Column("payload_digest", sa.String(64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("board_id", "idempotency_key",
                                name="uq_plan_operation_idempotency"),
        )
        op.create_index("ix_plan_operations_project_id", "plan_operations", ["project_id"])
        op.create_index("ix_plan_operations_board_id", "plan_operations", ["board_id"])
        op.create_index("ix_plan_operations_status", "plan_operations", ["status"])

    for _name, ddl in PLAN_OPERATION_TRIGGERS:
        bind.execute(sa.text(ddl))
    bind.execute(sa.text(APPLIED_RESULT_INDEX))


def downgrade() -> None:
    """Remove TSB-367 only. Storyboard and legacy plan tables are untouched."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    bind.execute(sa.text("DROP INDEX IF EXISTS uq_plan_operation_applied_result"))
    for name, _ddl in PLAN_OPERATION_TRIGGERS:
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))

    if "plan_operations" in existing:
        op.drop_table("plan_operations")
