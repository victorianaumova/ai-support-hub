"""`runs.steps`: per-step durations of a run (P9-01, ADR-0021).

The console's Run tab shows each node of the run with its duration and outcome. They are stored
on the run, not read from Langfuse, so the tab works with tracing switched off.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "steps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(op.f("ck_runs_steps"), "runs", "jsonb_typeof(steps) = 'array'")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_steps"), "runs", type_="check")
    op.drop_column("runs", "steps")
