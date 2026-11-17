"""create clip caption and caption style tables

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clip_captions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("clip_id", "index", name="uq_clip_captions_clip_id_index"),
        sa.CheckConstraint('"index" >= 0', name="ck_clip_captions_index_non_negative"),
        sa.CheckConstraint(
            "start_seconds >= 0", name="ck_clip_captions_start_non_negative"
        ),
        sa.CheckConstraint(
            "end_seconds > start_seconds", name="ck_clip_captions_end_after_start"
        ),
    )
    op.create_table(
        "caption_styles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clip_id", sa.Uuid(), nullable=False),
        sa.Column(
            "preset",
            sa.Enum(
                "classic",
                "bold",
                "karaoke",
                "minimal",
                name="caption_preset",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("font_family", sa.String(length=64), nullable=False),
        sa.Column("font_size", sa.Integer(), nullable=False),
        sa.Column("primary_color", sa.String(length=9), nullable=False),
        sa.Column("outline_color", sa.String(length=9), nullable=False),
        sa.Column(
            "position",
            sa.Enum(
                "bottom",
                "middle",
                "top",
                name="caption_position",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("uppercase", sa.Boolean(), nullable=False),
        sa.Column("max_chars_per_line", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "font_size >= 16 AND font_size <= 160",
            name="ck_caption_styles_font_size_range",
        ),
        sa.CheckConstraint(
            "max_chars_per_line >= 10 AND max_chars_per_line <= 80",
            name="ck_caption_styles_max_chars_range",
        ),
    )
    op.create_index(
        "ix_caption_styles_clip_id", "caption_styles", ["clip_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_caption_styles_clip_id", table_name="caption_styles")
    op.drop_table("caption_styles")
    op.drop_table("clip_captions")
