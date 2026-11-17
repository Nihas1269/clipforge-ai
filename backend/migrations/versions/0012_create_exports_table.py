"""create exports table

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "aspect_ratio",
            sa.Enum(
                "9:16",
                "1:1",
                "4:5",
                "16:9",
                name="export_aspect_ratio",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "reframe_mode",
            sa.Enum(
                "center",
                "face",
                name="export_reframe_mode",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="center",
            nullable=False,
        ),
        sa.Column("include_captions", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "rendering",
                "completed",
                "failed",
                name="export_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_exports_size_bytes_non_negative",
        ),
    )
    op.create_index("ix_exports_clip_id", "exports", ["clip_id"], unique=False)
    op.create_index("ix_exports_user_id", "exports", ["user_id"], unique=False)
    op.create_index("ix_exports_job_id", "exports", ["job_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_exports_job_id", table_name="exports")
    op.drop_index("ix_exports_user_id", table_name="exports")
    op.drop_index("ix_exports_clip_id", table_name="exports")
    op.drop_table("exports")
