import hashlib
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from database.connection import get_session
from database.models import Competitor, Promotion
from services.team_policy_engine import TeamPolicyEngine


def make_offer_hash(source: str, brand: str, title: str, source_url: str | None = None, date_str: str = "") -> str:
    """Generate a stable SHA-256 fingerprint for deduplication."""
    source_part = source_url or ""
    raw = f"{source}|{brand}|{source_part}|{title}|{date_str}".lower().strip()
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()



def make_legacy_offer_hash(source: str, brand: str, title: str) -> str:
    raw = f"{source}|{brand}|{title}".lower().strip()
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class PostgresPipeline:
    """
    Receives OfferItems from any spider and
    upserts them into the PostgreSQL `promotions` table using offer_hash
    for deduplication.
    """

    def open_spider(self, spider):
        self.session = get_session()
        self.policy_engine = TeamPolicyEngine()
        self.items_scraped = 0
        self.items_inserted = 0
        self.items_updated = 0
        spider.logger.info("PostgresPipeline: DB session opened.")

    def process_item(self, item, spider):
        from promo_scraper.items import OfferItem
        if not isinstance(item, OfferItem):
            return item

        self.items_scraped += 1

        brand_name  = item.get('brand', 'unknown')
        source_name = item.get('source', 'unknown')
        title       = item.get('title', '')
        source_url  = item.get('source_url')

        # 1. Look up competitor_id from DB
        competitor = self.session.query(Competitor).filter_by(name=brand_name).first()
        if not competitor:
            spider.logger.warning(f"Competitor '{brand_name}' not found in DB. Skipping item.")
            return item

        # 2. Generate deduplication hash
        scraped_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        offer_hash = make_offer_hash(source_name, brand_name, title, source_url, scraped_date_str)


        # 3. Upsert: check if offer already exists
        existing = self.session.query(Promotion).filter_by(offer_hash=offer_hash).first()
        if not existing:
            legacy_hash = make_legacy_offer_hash(source_name, brand_name, title)
            legacy = self.session.query(Promotion).filter_by(offer_hash=legacy_hash).first()
            if legacy and legacy.source_url == source_url:
                existing = legacy
                existing.offer_hash = offer_hash

        cat = item.get('category') or 'Others'
        if existing:
            existing.offer_title = title
            existing.scraped_at = datetime.now(timezone.utc)
            existing.category = cat
            self.items_updated += 1
            promotion = existing
        else:
            # Insert new promotion
            promotion = Promotion(
                competitor_id = competitor.id,
                brand         = brand_name,
                offer_title   = title,
                category      = cat,
                source_name   = source_name,
                source_url    = source_url,
                offer_hash    = offer_hash,
                scraped_at    = datetime.now(timezone.utc),
                created_at    = datetime.now(timezone.utc),
            )
            self.session.add(promotion)
            self.session.flush()
            self.items_inserted += 1

        # Sync team assignment rules
        self.policy_engine.sync_promotion_assignments(self.session, promotion)

        return item

    def close_spider(self, spider):
        # Single batch commit for all items — 10-50× faster than per-row commits.
        try:
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            spider.logger.error(
                "Batch DB commit failed (%d items): %s",
                self.items_scraped, e,
            )
            # Reset counts since nothing was persisted
            self.items_inserted = 0
            self.items_updated = 0
        finally:
            self.session.close()

        spider.logger.info(
            f"PostgresPipeline closed. "
            f"Scraped={self.items_scraped}, "
            f"Inserted={self.items_inserted}, "
            f"Updated={self.items_updated}"
        )
        spider.pipeline_stats = {
            'items_scraped':  self.items_scraped,
            'items_inserted': self.items_inserted,
            'items_updated':  self.items_updated,
        }
