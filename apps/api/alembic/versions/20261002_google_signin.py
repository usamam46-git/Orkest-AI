"""Sign in with Google: users.google_sub.

Revision ID: 20261002_google
Revises: 20260830_run_instr

One nullable, unique column. Null for every existing user and for anyone who has
only ever used a password; set the first time an account signs in with Google.

Unique, because one Google account must map to exactly one user — and because the
unique index is what makes `WHERE google_sub = :sub` the identity lookup rather
than a scan.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261002_google"
down_revision = "20260830_run_instr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "google_sub",
            sa.Text(),
            nullable=True,
            comment="Google's stable account id (the `sub` claim).",
        ),
    )
    op.create_unique_constraint("uq_users_google_sub", "users", ["google_sub"])


def downgrade() -> None:
    op.drop_constraint("uq_users_google_sub", "users", type_="unique")
    op.drop_column("users", "google_sub")
