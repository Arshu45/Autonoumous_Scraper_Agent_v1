"""
run_health_check.py
===================
Scraper Health Check Agent — Task 8

Monitors all enabled targets in `prefect_target_registry` and detects
unhealthy scrapers by analysing the last 3 scrape run outcomes stored
in `agent_run_outcomes`.

Health criteria evaluated per target:
  1. Zero-yield collapse   — all 3 recent runs returned 0 offers.
  2. High schema failure   — schema_valid_pct < 50% across recent runs
                            (schema_valid_pct is stored in score_breakdown JSONB).
  3. No runs recorded      — the target has never produced an outcome row
                            (treated as data-collection gap, not unhealthy).

For each target a new row is inserted into `agent_run_outcomes` with:
  run_type             = "health_check"
  still_healthy_at_check = True | False
  score_breakdown      = {
      "checked_targets": N,
      "unhealthy_reason": "zero_yield" | "high_schema_failure_rate" | None,
      "recent_yield_counts": [n1, n2, n3],
      "recent_schema_valid_pcts": [p1, p2, p3],
      "days_since_registration": D,
  }

Detection-only policy (§19a of implementation_plan.md):
  Alerts are written to the log and to `agent_run_outcomes`.
  No automated repair code is executed in this version.
  A clearly marked TODO is left where automated recovery would hook in.

Usage (standalone):
    ../env/bin/python scripts/run_health_check.py

Usage (with Prefect or cron):
    # Call run_health_check() directly from any flow or scheduler.
    from scripts.run_health_check import run_health_check
    run_health_check()

Exit codes:
    0 — all targets healthy (or no targets registered)
    1 — one or more targets flagged as unhealthy
    2 — fatal error (DB connection failure, etc.)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is importable regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("health_check")

# ── Thresholds ─────────────────────────────────────────────────────────────────

# Number of recent `agent_run_outcomes` rows to inspect per target.
RECENT_RUN_LOOKBACK = 3

# If ALL recent runs had zero offers → flag as unhealthy (zero-yield collapse).
ZERO_YIELD_THRESHOLD = 0  # any run with offers_extracted <= this counts as zero

# If average schema_valid_pct across recent runs is below this → high failure rate.
SCHEMA_FAILURE_PCT_THRESHOLD = 50.0  # percent (0–100)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_enabled_targets(session) -> list[Any]:
    """Return all enabled rows from prefect_target_registry."""
    from database.models import PrefectTargetRegistry
    return (
        session.query(PrefectTargetRegistry)
        .filter_by(enabled=True)
        .order_by(PrefectTargetRegistry.brand)
        .all()
    )


def _get_recent_outcomes_batch(session, brands: list[str], n: int = RECENT_RUN_LOOKBACK) -> dict[str, list[Any]]:
    """
    Bulk-fetch recent `initial_validation` or `health_check` outcome rows for all `brands`
    in a single database query, grouped newest-first up to `n` rows per brand.
    """
    from database.models import AgentRunOutcome
    from sqlalchemy import desc

    if not brands:
        return {}

    rows = (
        session.query(AgentRunOutcome)
        .filter(
            AgentRunOutcome.brand.in_(brands),
            AgentRunOutcome.run_type.in_(["initial_validation", "health_check"]),
        )
        .order_by(AgentRunOutcome.brand, desc(AgentRunOutcome.checked_at))
        .all()
    )

    outcomes_by_brand: dict[str, list[Any]] = {b: [] for b in brands}
    for row in rows:
        if row.brand in outcomes_by_brand and len(outcomes_by_brand[row.brand]) < n:
            outcomes_by_brand[row.brand].append(row)

    return outcomes_by_brand


def _get_recent_outcomes(session, brand: str, n: int = RECENT_RUN_LOOKBACK) -> list[Any]:
    """Single brand fallback helper."""
    res = _get_recent_outcomes_batch(session, [brand], n)
    return res.get(brand, [])


def _days_since(dt: datetime | None) -> int | None:
    """Return the number of days between `dt` and now (UTC). None if dt is None."""
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    # Make dt timezone-aware if it isn't (SQLAlchemy may return naive datetimes)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    return max(0, delta.days)


# ── Alert detection ────────────────────────────────────────────────────────────

def _analyse_target_health(
    brand: str,
    recent_outcomes: list[Any],
) -> dict[str, Any]:
    """
    Evaluate the health of a single target based on its recent outcome rows.

    Returns a dict with:
        still_healthy: bool
        unhealthy_reason: str | None   — "zero_yield" | "high_schema_failure_rate"
        recent_yield_counts: list[int]
        recent_schema_valid_pcts: list[float]
        runs_inspected: int
    """
    if not recent_outcomes:
        # No outcome rows at all — target has never been validated.
        # We treat this as healthy (data-collection gap), but log a notice.
        logger.info(
            "Target '%s' has no outcome rows — skipping health evaluation (data gap).",
            brand,
        )
        return {
            "still_healthy": True,
            "unhealthy_reason": None,
            "recent_yield_counts": [],
            "recent_schema_valid_pcts": [],
            "runs_inspected": 0,
            "note": "no_outcome_rows_yet",
        }

    yield_counts: list[int] = []
    schema_pcts: list[float] = []

    for row in recent_outcomes:
        yield_counts.append(row.offers_extracted or 0)

        # schema_valid_pct lives inside score_breakdown JSONB
        pct: float = 0.0
        if row.score_breakdown and isinstance(row.score_breakdown, dict):
            pct = float(row.score_breakdown.get("schema_valid_pct", 0.0))
        schema_pcts.append(pct)

    runs_inspected = len(recent_outcomes)

    # ── Check 1: Zero-yield collapse ──────────────────────────────────────────
    # Only flag if we have the full lookback window — avoids false positives on
    # targets that have only 1–2 runs recorded.
    if runs_inspected >= RECENT_RUN_LOOKBACK:
        if all(y <= ZERO_YIELD_THRESHOLD for y in yield_counts):
            logger.warning(
                "UNHEALTHY (zero-yield collapse): '%s' — last %d runs all returned 0 offers. "
                "Yield counts: %s",
                brand, runs_inspected, yield_counts,
            )
            return {
                "still_healthy": False,
                "unhealthy_reason": "zero_yield",
                "recent_yield_counts": yield_counts,
                "recent_schema_valid_pcts": schema_pcts,
                "runs_inspected": runs_inspected,
            }

    # ── Check 2: High schema failure rate ─────────────────────────────────────
    if runs_inspected >= RECENT_RUN_LOOKBACK and schema_pcts:
        avg_schema_pct = sum(schema_pcts) / len(schema_pcts)
        if avg_schema_pct < SCHEMA_FAILURE_PCT_THRESHOLD:
            logger.warning(
                "UNHEALTHY (high schema failure rate): '%s' — average schema_valid_pct=%.1f%% "
                "across last %d runs (threshold=%.1f%%). Schema pcts: %s",
                brand, avg_schema_pct, runs_inspected,
                SCHEMA_FAILURE_PCT_THRESHOLD, schema_pcts,
            )
            return {
                "still_healthy": False,
                "unhealthy_reason": "high_schema_failure_rate",
                "recent_yield_counts": yield_counts,
                "recent_schema_valid_pcts": schema_pcts,
                "runs_inspected": runs_inspected,
                "avg_schema_valid_pct": round(avg_schema_pct, 2),
            }

    # ── Healthy ───────────────────────────────────────────────────────────────
    logger.info(
        "HEALTHY: '%s' — %d run(s) inspected. Yield counts: %s, Schema pcts: %s",
        brand, runs_inspected, yield_counts, schema_pcts,
    )
    return {
        "still_healthy": True,
        "unhealthy_reason": None,
        "recent_yield_counts": yield_counts,
        "recent_schema_valid_pcts": schema_pcts,
        "runs_inspected": runs_inspected,
    }


# ── DB logging ─────────────────────────────────────────────────────────────────

def _log_health_check_outcome(
    session,
    brand: str,
    health_result: dict[str, Any],
    days_since_registration: int | None,
    commit: bool = True,
) -> None:
    """
    Insert a new row into `agent_run_outcomes` with run_type='health_check'.
    """
    from database.models import AgentRunOutcome

    breakdown = {
        "unhealthy_reason": health_result.get("unhealthy_reason"),
        "recent_yield_counts": health_result.get("recent_yield_counts", []),
        "recent_schema_valid_pcts": health_result.get("recent_schema_valid_pcts", []),
        "runs_inspected": health_result.get("runs_inspected", 0),
        "days_since_registration": days_since_registration,
    }
    for extra_key in ("avg_schema_valid_pct", "note"):
        if extra_key in health_result:
            breakdown[extra_key] = health_result[extra_key]

    row = AgentRunOutcome(
        brand=brand,
        run_type="health_check",
        confidence_score=None,
        score_breakdown=breakdown,
        recommendation=None,
        offers_extracted=None,
        was_auto_approved=None,
        days_since_registration=days_since_registration,
        still_healthy_at_check=health_result["still_healthy"],
    )
    session.add(row)
    if commit:
        session.commit()
    logger.debug(
        "Logged health_check outcome for '%s': still_healthy=%s",
        brand, health_result["still_healthy"],
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def run_health_check() -> dict[str, Any]:
    """
    Run health checks for all enabled targets.
    """
    from database.connection import get_session

    logger.info("=" * 60)
    logger.info("HEALTH CHECK AGENT — starting")
    logger.info("Lookback window: %d runs | Zero-yield threshold: %d | Schema threshold: %.0f%%",
                RECENT_RUN_LOOKBACK, ZERO_YIELD_THRESHOLD, SCHEMA_FAILURE_PCT_THRESHOLD)
    logger.info("=" * 60)

    summary: dict[str, Any] = {
        "targets_checked": 0,
        "healthy": 0,
        "unhealthy": 0,
        "skipped": 0,
        "unhealthy_brands": [],
        "errors": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    session = None
    try:
        session = get_session()
        targets = _get_enabled_targets(session)

        if not targets:
            logger.info("No enabled targets in prefect_target_registry. Nothing to check.")
            return summary

        logger.info("Loaded %d enabled target(s) for health check.", len(targets))

        # Bulk fetch all recent outcomes in 1 query
        brand_list = [t.brand for t in targets if t.brand]
        outcomes_by_brand = _get_recent_outcomes_batch(session, brand_list)

        # Evaluate and log each target within the single session
        for target in targets:
            brand: str = target.brand or "<unknown>"
            registered_at = getattr(target, "registered_at", None)
            days_since_reg = _days_since(registered_at)

            logger.info("-" * 40)
            logger.info("Checking target: '%s' (registered %s day(s) ago)", brand, days_since_reg)

            recent = outcomes_by_brand.get(brand, [])
            health_result = _analyse_target_health(brand, recent)

            try:
                _log_health_check_outcome(session, brand, health_result, days_since_reg, commit=False)
            except Exception as exc:
                logger.error("Error logging health outcome for '%s': %s", brand, exc)
                summary["errors"].append(f"{brand}: outcome log failed — {exc}")

            summary["targets_checked"] += 1
            if health_result["still_healthy"]:
                summary["healthy"] += 1
            else:
                summary["unhealthy"] += 1
                summary["unhealthy_brands"].append(brand)

        # Batch commit all logged outcomes
        session.commit()

    except Exception as exc:
        logger.critical("Health check execution failed: %s", exc)
        summary["errors"].append(f"Fatal: health check failed — {exc}")
        if session:
            session.rollback()
    finally:
        if session:
            session.close()

            # TODO [fast-follow — Repair Agent]: Once `agent/repair_agent.py` is
            # implemented, trigger automated re-exploration and selector patching here:
            #
            #     from agent.repair_agent import trigger_repair_agent
            #     trigger_repair_agent(brand=brand, url=target_url)
            #
            # The repair agent should re-enter the LangGraph graph at the
            # "exploration" node, diff old vs new selectors, produce a patched
            # config, route it through validation, and if confidence >= 70 surface
            # it to the operator workspace for approval — exactly like a new brand.
            #
            # Reference: docs/implementation_plan.md §19a, Appendix A.

    # ── 3. Print summary ──────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("HEALTH CHECK COMPLETE")
    logger.info(
        "  Targets checked : %d", summary["targets_checked"]
    )
    logger.info("  ✓ Healthy        : %d", summary["healthy"])
    logger.info("  ✗ Unhealthy      : %d", summary["unhealthy"])
    logger.info("  ⊘ Skipped (error): %d", summary["skipped"])

    if summary["unhealthy_brands"]:
        logger.warning(
            "  Unhealthy targets requiring attention: %s",
            ", ".join(summary["unhealthy_brands"]),
        )

    if summary["errors"]:
        logger.warning("  Errors encountered: %s", "; ".join(summary["errors"]))

    logger.info("=" * 60)

    return summary


# ── CLI entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = run_health_check()

    if result.get("errors"):
        sys.exit(2)
    elif result.get("unhealthy", 0) > 0:
        sys.exit(1)
    else:
        sys.exit(0)
