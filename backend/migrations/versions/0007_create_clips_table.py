"""create clips table

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clips",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "suggested",
                "accepted",
                "rejected",
                name="clip_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="suggested",
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "heuristic",
                "llm",
                "manual",
                name="clip_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.CheckConstraint("start_seconds >= 0", name="ck_clips_start_non_negative"),
        sa.CheckConstraint(
            "end_seconds > start_seconds", name="ck_clips_end_after_start"
        ),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_clips_score_range"),
    )
    op.create_index(
        "ix_clips_video_id_start_seconds",
        "clips",
        ["video_id", "start_seconds"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_clips_video_id_start_seconds", table_name="clips")
    op.drop_table("clips")
