"""create pending_storage_deletions table

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_storage_deletions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_type", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_pending_storage_deletions_attempts_non_negative"
        ),
    )
    op.create_index(
        "ix_pending_storage_deletions_created_at",
        "pending_storage_deletions",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_storage_deletions_created_at", table_name="pending_storage_deletions"
    )
    op.drop_table("pending_storage_deletions")
