"""task_6_migration_d_prefect_target_registry

Revision ID: t6d004000004
Revises: t6c003000003
Create Date: 2026-07-19 14:03:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 't6d004000004'
down_revision: Union[str, Sequence[str], None] = 't6c003000003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create prefect_target_registry table (idempotent)."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS prefect_target_registry (
            id            SERIAL PRIMARY KEY,
            brand         VARCHAR(255) UNIQUE,
            config_path   VARCHAR(500),
            enabled       BOOLEAN   DEFAULT TRUE,
            registered_at TIMESTAMP DEFAULT NOW(),
            registered_by VARCHAR(255)
        )
    """)


def downgrade() -> None:
    """Drop prefect_target_registry table."""
    op.drop_table('prefect_target_registry')
