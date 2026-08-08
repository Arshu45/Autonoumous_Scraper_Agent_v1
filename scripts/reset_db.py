"""
Reset Script: Clears all data tables.

This does NOT drop the schema (tables stay). It only deletes all rows
so you can run a fresh scrape from scratch.

Usage:
    python scripts/reset_db.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_session
from database.models import Promotion, Competitor, PromotionTeamAssignment


def reset_all_tables(session):
    print("🗑️  Clearing all tables...")

    # Order matters — delete child tables before parents (foreign key constraints)
    counts = {
        'promotion_team_assignments': session.query(PromotionTeamAssignment).delete(),
        'promotions':                 session.query(Promotion).delete(),
        'competitors':                session.query(Competitor).delete(),
    }
    session.commit()

    for table, count in counts.items():
        print(f"   ✓ {table}: {count} rows deleted")


if __name__ == "__main__":
    confirm = input(
        "\n⚠️  This will DELETE ALL rows from promotions and competitors.\n"
        "   Type 'yes' to continue: "
    ).strip().lower()

    if confirm != 'yes':
        print("Aborted. No changes made.")
        sys.exit(0)

    session = get_session()
    try:
        reset_all_tables(session)
        print("\n✅ All tables cleared.")
        print("\n👉 Next steps:")
        print("   1. python flows/master_pipeline.py  ← run all scrapers")
    except Exception as e:
        session.rollback()
        print(f"❌ Error: {e}")
        raise
    finally:
        session.close()
