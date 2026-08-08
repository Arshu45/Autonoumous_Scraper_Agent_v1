"""
run_hybrid_promo_scraper.py
===========================
Entry point for the hybrid promotional scraper.

Each brand/target is processed as a CONCURRENT Prefect task.
All tasks are submitted in parallel and awaited together before
the enrichment stage runs.

Usage (standalone):
    python scripts/run_hybrid_promo_scraper.py

Usage (via Prefect flow):
    from scripts.run_hybrid_promo_scraper import scrape_single_target, load_targets
"""

import argparse
import hashlib
import json
import glob
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_session
from database.models import Competitor, Promotion
from promo_scraper.hybrid_promo_extractor import HybridPromoExtractor
from services.team_policy_engine import TeamPolicyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hybrid_promo_scraper")
policy_engine = TeamPolicyEngine()

from scripts.logging_setup import attach_file_logger, detach_file_logger


# ── Load Target registry from config ──────────────────────────────────────────

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "targets",
)


def _load_target_file(config_file: str) -> dict | None:
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("enabled", True) is False:
            logger.info("Target brand '%s' is disabled in config (%s) — skipping", cfg.get("brand"), os.path.basename(config_file))
            return None
        return cfg
    except Exception as e:
        logger.error("Failed to load target config from %s: %s", config_file, e)
        return None


def load_targets(target_path: str | None = None) -> list[dict]:
    """Load targets from DB registry, falling back to filesystem glob."""
    if target_path:
        cfg = _load_target_file(target_path)
        return [cfg] if cfg else []

    try:
        from database.models import PrefectTargetRegistry
        session = get_session()
        rows = session.query(PrefectTargetRegistry).filter_by(enabled=True).all()
        if rows:
            targets = []
            for row in rows:
                cfg = _load_target_file(row.config_path)
                if cfg:
                    targets.append(cfg)
            session.close()
            if targets:
                # Also include filesystem-only configs not in the registry
                fs_targets = _load_filesystem_targets()
                registered_brands = {t["brand"] for t in targets}
                for t in fs_targets:
                    if t["brand"] not in registered_brands:
                        targets.append(t)
                return targets
        session.close()
    except Exception:
        pass  # fall back to filesystem

    return _load_filesystem_targets()


def _load_filesystem_targets() -> list[dict]:
    """Original glob-based loading — backward compatible."""
    targets = []
    for config_file in sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json"))):
        cfg = _load_target_file(config_file)
        if cfg:
            targets.append(cfg)
    return targets



# ── DB helpers ─────────────────────────────────────────────────────────────────

def _make_offer_hash(source: str, brand: str, title: str, source_url: str | None = None, date_str: str = "") -> str:
    source_part = source_url or ""
    raw = f"{source}|{brand}|{source_part}|{title}|{date_str}".lower().strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()



def _make_legacy_offer_hash(source: str, brand: str, title: str) -> str:
    raw = f"{source}|{brand}|{title}".lower().strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _save_offer_items(offer_dicts: list[dict], session) -> tuple[int, int]:
    """
    Upserts a batch of offer dicts into the promotions table.
    Returns (inserted, updated).

    All staging (session.add / attribute updates) is done first, then a
    SINGLE commit is issued for the entire batch.  This is ~10-50× faster
    than committing per-row and ensures all offers for a brand are written
    atomically — either all succeed or all roll back together.
    """
    if not offer_dicts:
        return 0, 0

    inserted = updated = 0

    # All items in a batch share the same brand — look up / create once.
    brand_name = offer_dicts[0]["brand"]
    competitor = session.query(Competitor).filter_by(name=brand_name).first()
    if not competitor:
        competitor = Competitor(name=brand_name, enabled=True)
        session.add(competitor)
        session.flush()   # get competitor.id without a full commit
        logger.info("Auto-created competitor '%s' in DB", brand_name)

    scraped_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Pre-compute all hashes so we can batch-fetch existing promotions in
    # a single query instead of 2 SELECTs per item (N+1 → 1).
    item_hashes = []
    for item in offer_dicts:
        source_url = item.get("source_url")
        new_hash = _make_offer_hash(item["source"], brand_name, item["title"], source_url, scraped_date_str)
        legacy_hash = _make_legacy_offer_hash(item["source"], brand_name, item["title"])
        item_hashes.append((new_hash, legacy_hash, source_url))

    all_hashes = set()
    for new_h, legacy_h, _ in item_hashes:
        all_hashes.add(new_h)
        all_hashes.add(legacy_h)

    # Single query: fetch all promotions whose offer_hash matches any of our hashes.
    existing_promos = (
        session.query(Promotion)
        .filter(Promotion.offer_hash.in_(all_hashes))
        .all()
    )
    promo_by_hash = {p.offer_hash: p for p in existing_promos}

    for item, (offer_hash, legacy_hash, source_url) in zip(offer_dicts, item_hashes):
        existing = promo_by_hash.get(offer_hash)
        if not existing:
            legacy = promo_by_hash.get(legacy_hash)
            if legacy and legacy.source_url == source_url:
                existing = legacy
                existing.offer_hash = offer_hash

        cat = item.get("category") or "Others"
        if existing:
            existing.offer_title           = item["title"]
            existing.scraped_at            = datetime.now(timezone.utc)
            existing.extraction_confidence = item.get("confidence")
            existing.category              = cat
            updated += 1
            promo_obj = existing
        else:
            promo_obj = Promotion(
                competitor_id         = competitor.id,
                brand                 = brand_name,
                offer_title           = item["title"],
                category              = cat,
                source_name           = item["source"],
                source_url            = item.get("source_url"),
                extraction_confidence = item.get("confidence"),
                offer_hash            = offer_hash,
                scraped_at            = datetime.now(timezone.utc),
                created_at            = datetime.now(timezone.utc),
            )
            session.add(promo_obj)
            session.flush()
            inserted += 1
        
        # Sync team assignment rules
        policy_engine.sync_promotion_assignments(session, promo_obj)

    # Single commit for the entire brand batch — re-raise on failure
    # so the caller knows no data was persisted.
    try:
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(
            "Batch DB commit failed for brand '%s' (%d offers): %s",
            brand_name, len(offer_dicts), e,
        )
        raise

    return inserted, updated


# ── Per-brand task (runs concurrently) ─────────────────────────────────────────

def scrape_single_target(target: dict) -> dict:
    """
    Scrapes ONE brand target.
    Each call opens its own DB session → safe to run in parallel threads.
    Returns a summary dict for the Prefect report.
    """
    brand = target["brand"]
    logger.info("=" * 60)
    logger.info("Processing: %s", brand)
    logger.info("=" * 60)

    session = get_session()
    try:
        extractor = HybridPromoExtractor(target)
        summary   = extractor.run()
        items     = summary.pop("offer_items", [])

        if items:
            try:
                inserted, updated = _save_offer_items(items, session)
            except Exception as db_err:
                logger.error("%s — DB save failed, 0 offers stored: %s", brand, db_err)
                inserted = updated = 0
            summary["offers_stored"] = inserted + updated
            logger.info(
                "%s — Inserted: %d  |  Updated: %d  |  Cost: $%.6f",
                brand, inserted, updated, summary.get("estimated_cost_usd", 0.0),
            )
        else:
            logger.warning("%s — No offers extracted", brand)
            summary.setdefault("offers_stored", 0)

        return summary

    except EnvironmentError as e:
        session.rollback()
        logger.critical(str(e))
        raise
    except Exception as e:
        session.rollback()
        logger.error("Failed to process '%s': %s", brand, e, exc_info=True)
        return {"brand": brand, "error": str(e)}
    finally:
        session.close()


# ── Standalone runner (sequential, for direct CLI use) ─────────────────────────

def run_hybrid_promo_scraper(target_path: str | None = None) -> list[dict]:
    """
    Runs all targets SEQUENTIALLY when called directly (python scripts/run_...).
    For concurrent execution, use the Prefect flow in flows/master_pipeline.py.
    Each run writes a timestamped log file to logs/.
    """
    file_handler = attach_file_logger(source="cli")
    try:
        targets = load_targets(target_path)
        all_summaries = [scrape_single_target(t) for t in targets]
        _print_summary(all_summaries)
    finally:
        log_path = detach_file_logger(file_handler)
        logger.info("📄 Full log saved → %s", log_path)
    return all_summaries


def _print_summary(all_summaries: list[dict]) -> None:
    logger.info("")
    logger.info("━" * 60)
    logger.info("HYBRID PROMO SCRAPER — COMPLETE")
    logger.info("━" * 60)
    total_offers = sum(s.get("offers_extracted", 0) for s in all_summaries)
    total_stored = sum(s.get("offers_stored", 0)    for s in all_summaries)
    total_cost   = sum(s.get("estimated_cost_usd", 0) for s in all_summaries)
    total_calls  = sum(s.get("gemini_api_calls", 0) for s in all_summaries)

    for s in all_summaries:
        if "error" in s:
            logger.info("  ✗ %-20s  ERROR: %s", s["brand"], s["error"])
        else:
            logger.info(
                "  ✓ %-20s  images=%d  offers=%d  stored=%d  api_calls=%d  cost=$%.6f",
                s["brand"],
                s.get("images_processed", 0),
                s.get("offers_extracted", 0),
                s.get("offers_stored", 0),
                s.get("gemini_api_calls", 0),
                s.get("estimated_cost_usd", 0.0),
            )

    logger.info(
        "  TOTAL  offers=%d  stored=%d  api_calls=%d  est_cost=$%.6f",
        total_offers, total_stored, total_calls, total_cost,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the hybrid promo scraper.")
    parser.add_argument("--target", help="Path to one target config JSON, e.g. config/targets/the_iconic.json")
    args = parser.parse_args()
    run_hybrid_promo_scraper(args.target)
