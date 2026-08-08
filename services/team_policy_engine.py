"""
team_policy_engine.py
=====================
Policy routing engine that evaluates promotions against centralized business team rules
defined in config/teams.json. Decouples objective product categorization from subjective
team visibility routing.
"""

import json
import logging
import os
import re
from typing import Any, List, Dict
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "teams.json"
)


class TeamPolicyEngine:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.teams: List[Dict[str, Any]] = []
        self.reload_config()

    def reload_config(self) -> None:
        """Reload team configuration from teams.json."""
        if not os.path.exists(self.config_path):
            logger.warning("Teams config file not found at %s. Using empty teams list.", self.config_path)
            self.teams = []
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.teams = []
            if isinstance(data, dict):
                if "teams" in data and isinstance(data["teams"], list):
                    # Legacy array format fallback
                    for item in data["teams"]:
                        if isinstance(item, dict):
                            self.teams.append(item)
                else:
                    # Ultra-clean dictionary format:
                    # { "Beauty": { "categories": ["Beauty"], "allowed_brands": [...] } }
                    for team_name, cfg in data.items():
                        if isinstance(cfg, dict):
                            team_id = cfg.get("team_id") or team_name.lower().replace(" ", "_").replace("&", "and")
                            self.teams.append({
                                "team_id": team_id,
                                "team_name": team_name,
                                "categories": cfg.get("categories", [team_name]),
                                "allowed_brands": cfg.get("allowed_brands"),
                                "excluded_brands": cfg.get("excluded_brands", []),
                                "enabled": cfg.get("enabled", True),
                            })
            logger.info("Loaded %d team configurations from %s", len(self.teams), self.config_path)
        except Exception as e:
            logger.error("Failed to load teams config from %s: %s", self.config_path, e)
            self.teams = []

    @staticmethod
    def _brand_matches(promo_brand: str, target_list_or_dict: Any) -> Any:
        """
        Check if promo_brand matches any brand in target_list or target_dict.
        Returns the matched brand key/item (str) if found, else None.
        Handles regional suffixes (e.g. 'Tommy Hilfiger Australia' matches 'Tommy Hilfiger').
        """
        if not target_list_or_dict:
            return None

        promo_b = (promo_brand or "").lower().strip()
        promo_b_normalized = re.sub(r"\s+(australia|au|official|online|store)$", "", promo_b)

        target_keys = (
            list(target_list_or_dict.keys())
            if isinstance(target_list_or_dict, dict)
            else target_list_or_dict
        )

        for item in target_keys:
            a = (item or "").lower().strip()
            a_normalized = re.sub(r"\s+(australia|au|official|online|store)$", "", a)
            if promo_b == a or promo_b_normalized == a_normalized:
                return item
        return None

    def evaluate_promotion(self, promo: Dict[str, Any]) -> List[str]:
        """
        Evaluate a promotion dictionary against all active team rules.

        promo keys expected:
          - brand (str)
          - category (str)
          - offer_title (str)

        Returns:
          List of team_id strings for teams that should receive this promotion.
        """
        assigned_team_ids: List[str] = []

        promo_category = (promo.get("category") or "").strip()
        promo_brand = (promo.get("brand") or "").strip()

        if not promo_category:
            return assigned_team_ids

        for team in self.teams:
            if not team.get("enabled", True):
                continue

            team_id = team.get("team_id")
            team_name = team.get("team_name", team_id)
            team_default_categories = team.get("categories", [])
            effective_categories = team_default_categories

            # 1. Allowlist Check & Brand-Specific Category Determination
            allowed_brands = team.get("allowed_brands")
            if allowed_brands is not None:
                matched_brand_key = self._brand_matches(promo_brand, allowed_brands)
                if not matched_brand_key:
                    logger.debug(
                        "Promo '%s' (brand: %s) skipped for team '%s': brand not in allowed_brands",
                        promo.get("offer_title"), promo_brand, team_name
                    )
                    continue

                # Support Option A: dict-based allowed_brands with brand-specific categories
                if isinstance(allowed_brands, dict):
                    brand_cfg = allowed_brands.get(matched_brand_key)
                    if isinstance(brand_cfg, dict) and "categories" in brand_cfg:
                        effective_categories = brand_cfg["categories"]
                    elif isinstance(brand_cfg, list):
                        effective_categories = brand_cfg

            # 2. Category Matching against effective_categories
            if not any(c.lower() == promo_category.lower() for c in effective_categories):
                continue

            # 3. Denylist Check (if specified)
            excluded_brands = team.get("excluded_brands", [])
            if excluded_brands:
                if self._brand_matches(promo_brand, excluded_brands):
                    logger.debug(
                        "Promo '%s' (brand: %s) skipped for team '%s': brand in excluded_brands",
                        promo.get("offer_title"), promo_brand, team_name
                    )
                    continue

            # 4. Legacy brand_filter support if present
            brand_filter = team.get("brand_filter", {})
            if brand_filter:
                mode = brand_filter.get("mode", "denylist").lower()
                configured_brands = brand_filter.get("brands", [])
                if mode == "allowlist" and not self._brand_matches(promo_brand, configured_brands):
                    continue
                elif mode == "denylist" and self._brand_matches(promo_brand, configured_brands):
                    continue

            assigned_team_ids.append(team_id)

        return assigned_team_ids

    def sync_promotion_assignments(self, session: Session, promotion_obj: Any) -> List[str]:
        """
        Synchronize `promotion_team_assignments` table in PostgreSQL for a given Promotion object.
        """
        from database.models import PromotionTeamAssignment

        if promotion_obj.id is None:
            session.flush()

        promo_dict = {
            "brand": promotion_obj.brand,
            "category": promotion_obj.category,
            "offer_title": promotion_obj.offer_title,
        }

        assigned_team_ids = self.evaluate_promotion(promo_dict)

        # Clear existing assignments for this promotion to avoid stale mappings
        session.query(PromotionTeamAssignment).filter_by(promotion_id=promotion_obj.id).delete()

        # Insert new team assignments
        for team_id in assigned_team_ids:
            assignment = PromotionTeamAssignment(
                promotion_id=promotion_obj.id,
                team_id=team_id
            )
            session.add(assignment)

        return assigned_team_ids
