"""
scripts/reassign_teams.py
=========================
One-off script to re-apply team routing rules from config/teams.json
to ALL existing promotions in the database.

Run this whenever teams.json is updated (e.g. new categories added,
brands moved between teams) — no need to re-scrape.

Usage:
    python scripts/reassign_teams.py
"""
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_session
from database.models import Promotion
from services.team_policy_engine import TeamPolicyEngine


def reassign_all():
    engine = TeamPolicyEngine()
    logger.info("Loaded %d team configurations.", len(engine.teams))

    BATCH_SIZE = 200

    session = get_session()
    try:
        total = session.query(Promotion).count()
        logger.info("Found %d promotions to reassign.", total)

        reassigned = 0
        # Load promotions list so session.commit() doesn't invalidate an active server-side cursor
        promotions = session.query(Promotion).all()
        for i, promo in enumerate(promotions, start=1):
            team_ids = engine.sync_promotion_assignments(session, promo)
            reassigned += 1

            # Periodic commit to flush assignments and keep memory lean
            if i % BATCH_SIZE == 0:
                session.commit()
                logger.info("  Progress: %d / %d (last: %s → teams: %s)", i, total, promo.brand, team_ids or "unassigned")

        # Final commit for the remaining rows
        session.commit()
        logger.info("✅ Done. Reassigned %d promotions.", reassigned)

    except Exception as e:
        session.rollback()
        logger.error("❌ Reassignment failed: %s", e)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    reassign_all()
