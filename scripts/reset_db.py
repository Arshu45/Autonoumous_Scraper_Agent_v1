"""
Reset Script: Clears all data tables.

This does NOT drop the database schema (tables remain intact). It deletes all rows
so you can run a fresh scrape or re-register agent targets from scratch.

Usage:
    python scripts/reset_db.py
    python scripts/reset_db.py --keep-registry  # Keep target registry & audit logs
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_session
from database.models import (
    PromotionTeamAssignment,
    Promotion,
    Competitor,
    AgentRunOutcome,
    AgentAuditLog,
    PrefectTargetRegistry,
)


def reset_all_tables(session, keep_registry: bool = False):
    print("🗑️  Clearing database tables...")

    # Foreign key constraint safe deletion order (child tables first)
    counts = {
        'promotion_team_assignments': session.query(PromotionTeamAssignment).delete(),
        'promotions':                 session.query(Promotion).delete(),
        'agent_run_outcomes':         session.query(AgentRunOutcome).delete(),
    }

    if not keep_registry:
        counts['agent_audit_log']         = session.query(AgentAuditLog).delete()
        counts['prefect_target_registry'] = session.query(PrefectTargetRegistry).delete()

    counts['competitors'] = session.query(Competitor).delete()

    session.commit()

    for table, count in counts.items():
        print(f"   ✓ {table}: {count} rows deleted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset database tables.")
    parser.add_argument(
        "--keep-registry",
        action="store_true",
        help="Keep target registry and audit logs intact while deleting promotions and competitors.",
    )
    args = parser.parse_args()

    confirm = input(
        "\n⚠️  WARNING: This will DELETE rows from database tables.\n"
        f"   Target registry will be: {'KEPT' if args.keep_registry else 'CLEARED'}\n"
        "   Type 'yes' to continue: "
    ).strip().lower()

    if confirm != 'yes':
        print("Aborted. No changes made.")
        sys.exit(0)

    session = get_session()
    try:
        reset_all_tables(session, keep_registry=args.keep_registry)
        print("\n✅ Database reset complete.")
        print("\n👉 Next steps:")
        print("   1. python scripts/run_scraper_agent.py --url <URL> --brand <BRAND>  ← Register a new target")
        print("   2. python flows/master_pipeline.py                                 ← Run full scraper pipeline")
    except Exception as e:
        session.rollback()
        print(f"❌ Error during database reset: {e}")
        raise
    finally:
        session.close()
