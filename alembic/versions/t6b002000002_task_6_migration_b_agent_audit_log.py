"""task_6_migration_b_agent_audit_log

Revision ID: t6b002000002
Revises: t6a001000001
Create Date: 2026-07-19 14:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 't6b002000002'
down_revision: Union[str, Sequence[str], None] = 't6a001000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agent_audit_log table (idempotent)."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_audit_log (
            id         SERIAL PRIMARY KEY,
            brand      VARCHAR(255) NOT NULL,
            user_id    VARCHAR(255) NOT NULL,
            action     VARCHAR(50)  NOT NULL,
            details    JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    """Drop agent_audit_log table."""
    op.drop_table('agent_audit_log')
