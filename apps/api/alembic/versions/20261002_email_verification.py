"""Email verification: users.email_verified_at.

Revision ID: 20261002_emailver
Revises: 20261002_google

Nullable timestamp. Null means "this address has not proved it can receive mail".

**Every existing user is backfilled to now().** Without that, switching verification
on would lock out every account that registered before it existed — including the
owner's own. The honest reading of the backfill is "grandfathered", not "verified":
those addresses were never checked. The only existing accounts at the time of this
migration were created by the platform's own operator.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261002_emailver"
down_revision = "20261002_google"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When this address proved it can receive mail. Null = unverified.",
        ),
    )
    op.execute("UPDATE users SET email_verified_at = now() WHERE email_verified_at IS NULL")


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
