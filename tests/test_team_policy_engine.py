"""
test_team_policy_engine.py
===========================
Unit tests for the Team Policy Engine and team visibility routing.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.team_policy_engine import TeamPolicyEngine
from database.connection import get_session, init_db
from database.models import Competitor, Promotion, PromotionTeamAssignment


class TestTeamPolicyEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.engine = TeamPolicyEngine()

    def setUp(self):
        self.session = get_session()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_big_w_beauty_promo_excluded_from_beauty_team(self):
        """
        Verify Business Rule: Big W Beauty promotions are categorized as Beauty,
        but MUST NOT be assigned to Beauty Team because Big W is not in the Beauty allowlist.
        """
        big_w_beauty_promo = {
            "brand": "BIG W",
            "category": "Beauty",
            "offer_title": "Up to 50% off cosmetics, skincare and personal care"
        }

        assigned_teams = self.engine.evaluate_promotion(big_w_beauty_promo)

        self.assertNotIn("beauty_team", assigned_teams, "BIG W beauty promo must NOT be assigned to Beauty Team!")

    def test_david_jones_beauty_promo_assigned_to_beauty_team(self):
        """
        Verify David Jones Beauty promotions ARE assigned to Beauty Team (in allowlist).
        """
        dj_beauty_promo = {
            "brand": "David Jones",
            "category": "Beauty",
            "offer_title": "20% off selected luxury skincare & fragrances"
        }

        assigned_teams = self.engine.evaluate_promotion(dj_beauty_promo)

        self.assertIn("beauty_team", assigned_teams, "David Jones beauty promo should be assigned to Beauty Team!")

    def test_big_w_home_promo_assigned_to_home_team(self):
        """
        Verify Big W Home promotions ARE assigned to Home Team (denylist mode).
        """
        big_w_home_promo = {
            "brand": "BIG W",
            "category": "Home",
            "offer_title": "40% off Corelle Dinnerware"
        }

        assigned_teams = self.engine.evaluate_promotion(big_w_home_promo)

        self.assertIn("home_team", assigned_teams, "BIG W home promo should be assigned to Home Team!")

    def test_forever_new_womens_promo_assigned_to_womens_team(self):
        """
        Verify Forever New Womens promotions ARE assigned to Womens (WIFA) Team.
        """
        fn_womens_promo = {
            "brand": "Forever New",
            "category": "Womens",
            "offer_title": "20% off selected dresses & outerwear"
        }

        assigned_teams = self.engine.evaluate_promotion(fn_womens_promo)

        self.assertIn("womens_wifa", assigned_teams, "Forever New womens promo should be assigned to Womens Team!")

    def test_db_sync_promotion_assignments(self):
        """
        Test end-to-end database synchronization of promotion team assignments.
        """
        # 1. Ensure competitor exists
        competitor = self.session.query(Competitor).filter_by(name="BIG W").first()
        if not competitor:
            competitor = Competitor(name="BIG W", enabled=True)
            self.session.add(competitor)
            self.session.flush()

        # 2. Create test promotion
        import uuid
        test_hash = f"test_hash_{uuid.uuid4().hex[:12]}"
        promo = Promotion(
            competitor_id=competitor.id,
            brand="BIG W",
            offer_title="Test Beauty Offer for DB Sync",
            category="Beauty",
            source_name="hybrid",
            offer_hash=test_hash,
            scraped_at=os.sys.modules['datetime'].datetime.utcnow()
        )
        self.session.add(promo)
        self.session.flush()

        # 3. Sync team assignments
        assigned = self.engine.sync_promotion_assignments(self.session, promo)
        self.session.commit()

        # 4. Verify junction table entries
        db_assignments = self.session.query(PromotionTeamAssignment).filter_by(promotion_id=promo.id).all()
        assigned_ids = [a.team_id for a in db_assignments]

        self.assertNotIn("beauty_team", assigned_ids, "Database junction table should NOT include beauty_team for Big W beauty promo!")


if __name__ == "__main__":
    unittest.main()
