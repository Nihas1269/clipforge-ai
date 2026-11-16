"""create transcripts and transcript_segments tables

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=True),
        sa.Column("requested_language", sa.String(length=35), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_transcripts_video_id", "transcripts", ["video_id"], unique=True)

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transcript_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("words", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["transcript_id"], ["transcripts.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "transcript_id", "index", name="uq_transcript_segments_transcript_id_index"
        ),
        sa.CheckConstraint(
            '"index" >= 0', name="ck_transcript_segments_index_non_negative"
        ),
        sa.CheckConstraint(
            "start_seconds >= 0", name="ck_transcript_segments_start_non_negative"
        ),
        sa.CheckConstraint(
            "end_seconds > start_seconds", name="ck_transcript_segments_end_after_start"
        ),
    )


def downgrade() -> None:
    op.drop_table("transcript_segments")
    op.drop_index("ix_transcripts_video_id", table_name="transcripts")
    op.drop_table("transcripts")
