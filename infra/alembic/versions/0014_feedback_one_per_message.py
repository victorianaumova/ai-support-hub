"""One feedback per message, enforced by the database (P10-02, deferred from the P2-06 review).

`POST /tickets/{id}/feedback` already keeps one rating per AI message with a row lock on the
message; the unique constraint makes the database enforce it too, so a second writer that does
not take the lock (or a future code path) cannot add another row.

Before the constraint is created, any duplicate rows from before are removed, keeping the first
rating of each message (the one the API reports), as the API contract says only the first
rating is kept.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM feedback f
        USING feedback first
        WHERE f.message_id = first.message_id
          AND (first.created_at, first.id) < (f.created_at, f.id)
        """
    )
    op.create_unique_constraint(op.f("uq_feedback_message_id"), "feedback", ["message_id"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_feedback_message_id"), "feedback", type_="unique")
