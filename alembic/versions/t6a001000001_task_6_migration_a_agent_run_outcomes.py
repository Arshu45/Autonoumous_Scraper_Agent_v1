"""task_6_migration_a_agent_run_outcomes

Revision ID: t6a001000001
Revises: a1b2c3d4e5f6
Create Date: 2026-07-19 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 't6a001000001'
down_revision: Union[str, Sequence[str], None] = '56dc637c376b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agent_run_outcomes table (idempotent)."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_run_outcomes (
            id                      SERIAL PRIMARY KEY,
            brand                   VARCHAR(255),
            run_type                VARCHAR(20),
            confidence_score        INTEGER,
            score_breakdown         JSONB,
            recommendation          VARCHAR(20),
            offers_extracted        INTEGER,
            was_auto_approved       BOOLEAN,
            days_since_registration INTEGER,
            still_healthy_at_check  BOOLEAN,
            checked_at              TIMESTAMP DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    """Drop agent_run_outcomes table."""
    op.drop_table('agent_run_outcomes')
