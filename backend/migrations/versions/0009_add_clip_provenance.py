"""add clip provenance columns

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clips", sa.Column("detector_version", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "clips", sa.Column("detector_model", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "clips", sa.Column("prompt_version", sa.String(length=50), nullable=True)
    )


def downgrade() -> None:
    # Plain DROP COLUMN (PostgreSQL, SQLite >= 3.35) keeps the table and its
    # CHECK constraints instead of recreating it.
    op.drop_column("clips", "prompt_version")
    op.drop_column("clips", "detector_model")
    op.drop_column("clips", "detector_version")
