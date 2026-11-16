"""create processing_jobs table

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("video_id", sa.Uuid(), nullable=True),
        sa.Column(
            "job_type",
            sa.Enum(
                "metadata",
                "transcription",
                "clip_detection",
                "render",
                name="processing_job_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="processing_job_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("active_key", sa.String(length=320), nullable=True),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "max_attempts", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_processing_jobs_attempts_non_negative"
        ),
        sa.CheckConstraint(
            "max_attempts >= 1", name="ck_processing_jobs_max_attempts_positive"
        ),
    )
    op.create_index(
        "ix_processing_jobs_user_id", "processing_jobs", ["user_id"], unique=False
    )
    op.create_index(
        "ix_processing_jobs_video_id", "processing_jobs", ["video_id"], unique=False
    )
    op.create_index(
        "ix_processing_jobs_status", "processing_jobs", ["status"], unique=False
    )
    op.create_index(
        "ix_processing_jobs_active_key", "processing_jobs", ["active_key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_active_key", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_video_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_user_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")
