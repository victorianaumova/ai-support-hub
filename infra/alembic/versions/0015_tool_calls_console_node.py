"""The `console` node of `tool_calls` (P10-03 review, owner decision on PR #66).

The console's Customer tab reads the customer's limits, cards and recent transactions through
the core read tools, with the tool permissions like any other call (allow-list `console`). Those
calls are recorded under the ticket's latest run with node `console`, so the CHECK constraint
gets the new value. The value list is copied from `hub.domain.enums.ToolNode`;
tests/unit/test_db_models.py compares them.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tool_calls_node"), "tool_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_tool_calls_node"),
        "tool_calls",
        "node IN ('intake', 'research', 'handoff_prep', 'act', 'console')",
    )


def downgrade() -> None:
    # The older constraint cannot hold the console reads; their audit records stay.
    op.execute("DELETE FROM tool_calls WHERE node = 'console'")
    op.drop_constraint(op.f("ck_tool_calls_node"), "tool_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_tool_calls_node"),
        "tool_calls",
        "node IN ('intake', 'research', 'handoff_prep', 'act')",
    )
