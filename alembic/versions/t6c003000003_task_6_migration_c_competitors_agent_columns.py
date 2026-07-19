"""task_6_migration_c_competitors_agent_columns

Revision ID: t6c003000003
Revises: t6b002000002
Create Date: 2026-07-19 14:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 't6c003000003'
down_revision: Union[str, Sequence[str], None] = 't6b002000002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add agent-generated columns to competitors table (idempotent)."""
    op.execute("""
        ALTER TABLE competitors
            ADD COLUMN IF NOT EXISTS extraction_strategy VARCHAR(20) DEFAULT 'hybrid',
            ADD COLUMN IF NOT EXISTS agent_generated     BOOLEAN     DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS agent_confidence    INTEGER,
            ADD COLUMN IF NOT EXISTS agent_notes         TEXT,
            ADD COLUMN IF NOT EXISTS source_url          TEXT
    """)


def downgrade() -> None:
    """Remove agent-generated columns from competitors table."""
    op.drop_column('competitors', 'source_url')
    op.drop_column('competitors', 'agent_notes')
    op.drop_column('competitors', 'agent_confidence')
    op.drop_column('competitors', 'agent_generated')
    op.drop_column('competitors', 'extraction_strategy')
