# agent/validation_agent.py
"""
Validation Agent — Task 5

Runs the generated scraper config inside the Docker sandbox, validates the
returned offer data against the schema, computes a confidence score, and
populates state.validation_report.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.models import AgentState, ValidationReport
from agent.sandbox_runner import run_scraper_in_sandbox

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Offer schema validation
# ---------------------------------------------------------------------------

VALID_CONFIDENCE_VALUES = {"high", "medium", "low"}


def _validate_offer(offer: dict[str, Any], index: int) -> list[str]:
    """Return a list of schema error strings for a single offer dict."""
    errors: list[str] = []

    # title — required, non-empty
    title = offer.get("title")
    if not title or not str(title).strip():
        errors.append(f"offer[{index}]: 'title' is missing or empty")

    # confidence — required, must be one of the valid values
    confidence = offer.get("confidence")
    if confidence not in VALID_CONFIDENCE_VALUES:
        errors.append(
            f"offer[{index}]: 'confidence' is invalid ({confidence!r});"
            f" must be one of {sorted(VALID_CONFIDENCE_VALUES)}"
        )

    # Optional fields (no errors, just noted for scoring)
    # category, discount_min, discount_max may all be None — no validation needed

    return errors


def _validate_offers(offer_items: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Validate every offer. Returns (schema_valid, [error_strings])."""
    all_errors: list[str] = []
    for i, offer in enumerate(offer_items):
        all_errors.extend(_validate_offer(offer, i))
    return (len(all_errors) == 0, all_errors)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _compute_confidence_score(
    offer_items: list[dict[str, Any]],
    valid_offer_count: int,
    estimated_offer_count: int,
    anti_bot_risk: str,
    violations: list[str],
) -> tuple[int, dict[str, Any]]:
    """
    Compute the confidence score and a detailed breakdown dict.

    Returns (final_score: int, breakdown: dict).
    """
    breakdown: dict[str, Any] = {}
    total_offers = len(offer_items)

    # ---- Base score via yield-rate -------------------------------------------
    if estimated_offer_count > 0:
        yield_rate = total_offers / estimated_offer_count
        breakdown["yield_rate"] = round(yield_rate, 4)
        if total_offers >= 5 or yield_rate >= 0.8:
            base = 70
            breakdown["yield_bonus_applied"] = True
        elif total_offers >= 1:
            base = 50
            breakdown["yield_bonus_applied"] = False
        else:
            base = 10
            breakdown["yield_bonus_applied"] = False
    else:
        breakdown["yield_rate"] = None
        if total_offers >= 5:
            base = 70
            breakdown["yield_bonus_applied"] = True
        elif total_offers >= 1:
            base = 50
            breakdown["yield_bonus_applied"] = False
        else:
            base = 10
            breakdown["yield_bonus_applied"] = False

    breakdown["base_score"] = base
    score = base

    # ---- Schema quality adjustments ------------------------------------------
    if total_offers > 0:
        schema_valid_pct = (valid_offer_count / total_offers) * 100
    else:
        schema_valid_pct = 0.0

    breakdown["schema_valid_pct"] = round(schema_valid_pct, 2)

    if schema_valid_pct == 100:
        schema_bonus = 20
    elif schema_valid_pct >= 80:
        schema_bonus = 10
    else:
        schema_bonus = 0

    breakdown["schema_bonus"] = schema_bonus
    score += schema_bonus

    # ---- Field-population bonuses --------------------------------------------
    # title populated on all offers
    if total_offers > 0:
        title_count = sum(
            1 for o in offer_items if o.get("title") and str(o["title"]).strip()
        )
        title_pct = title_count / total_offers
    else:
        title_count = 0
        title_pct = 0.0

    title_bonus = 5 if title_pct == 1.0 else 0
    breakdown["title_fully_populated"] = title_pct == 1.0
    breakdown["title_bonus"] = title_bonus
    score += title_bonus

    # category populated on >= 80% of offers
    if total_offers > 0:
        category_count = sum(
            1 for o in offer_items if o.get("category") is not None
        )
        category_pct = category_count / total_offers
    else:
        category_count = 0
        category_pct = 0.0

    category_bonus = 5 if category_pct >= 0.8 else 0
    breakdown["category_pct"] = round(category_pct, 4)
    breakdown["category_bonus"] = category_bonus
    score += category_bonus

    # discount_min populated on >= 50% of offers
    if total_offers > 0:
        discount_count = sum(
            1 for o in offer_items if o.get("discount_min") is not None
        )
        discount_pct = discount_count / total_offers
    else:
        discount_count = 0
        discount_pct = 0.0

    discount_bonus = 5 if discount_pct >= 0.5 else 0
    breakdown["discount_min_pct"] = round(discount_pct, 4)
    breakdown["discount_bonus"] = discount_bonus
    score += discount_bonus

    # ---- Anti-bot risk penalties --------------------------------------------
    if anti_bot_risk == "high":
        antibot_penalty = -20
    elif anti_bot_risk == "medium":
        antibot_penalty = -5
    else:
        antibot_penalty = 0

    breakdown["anti_bot_risk"] = anti_bot_risk
    breakdown["antibot_penalty"] = antibot_penalty
    score += antibot_penalty

    breakdown["score_before_violation_check"] = score

    # ---- Sandbox violation override -----------------------------------------
    if violations:
        breakdown["violation_override"] = True
        score = 0
    else:
        breakdown["violation_override"] = False

    breakdown["final_score"] = score
    return max(0, score), breakdown


# ---------------------------------------------------------------------------
# Recommendation threshold
# ---------------------------------------------------------------------------

def _determine_recommendation(score: int, violations: list[str]) -> str:
    if violations:
        return "reject"
    if score >= 90:
        return "auto_approve"
    if score >= 70:
        return "pending"
    return "reject"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_validation_agent(state: AgentState) -> AgentState:
    """
    Execute the validation pipeline for the generated scraper config.

    1. Run sandbox with the generated config/code.
    2. Check for sandbox violations — instant reject if any found.
    3. Parse offer data from container logs.
    4. Validate each offer against the schema.
    5. Compute confidence score.
    6. Populate state.validation_report and set state.status = "validation".
    """
    logger.info("Validation agent starting for brand=%s", state.brand)

    brand = state.brand

    # ---- Early return: report already populated (e.g. pre-set in tests or repair runs) ----
    # If the caller has already built a ValidationReport (e.g. for direct routing tests),
    # honour it and skip the sandbox run entirely.
    if state.validation_report is not None:
        logger.info(
            "ValidationReport already present for brand=%s "
            "(recommendation=%s score=%d) — skipping sandbox run.",
            brand,
            state.validation_report.recommendation,
            state.validation_report.confidence_score,
        )
        state.status = "validation"
        return state

    artifacts = state.generated_artifacts

    # Defensive: if no artifacts, we cannot validate
    if artifacts is None:
        logger.error("No generated_artifacts on state — cannot validate brand=%s", brand)
        state.validation_report = ValidationReport(
            brand=brand,
            scraper_ran=False,
            issues=["No generated_artifacts found; generation step may have failed"],
            recommendation="reject",
        )
        state.status = "validation"
        return state

    config_json = artifacts.config_json
    scraper_code = artifacts.scraper_code
    estimated_offer_count = artifacts.estimated_offer_count

    # Derive anti-bot risk from site_analysis (may be absent in tests)
    anti_bot_risk = "low"
    if state.site_analysis is not None:
        anti_bot_risk = state.site_analysis.anti_bot_risk

    # ---- 1. Run sandbox -------------------------------------------------------
    logger.info("Launching sandbox for brand=%s (estimated offers: %d)", brand, estimated_offer_count)
    try:
        sandbox_result = run_scraper_in_sandbox(
            scraper_code=scraper_code,
            config=config_json,
            timeout_seconds=60,
        )
    except Exception as exc:
        logger.exception("Unexpected error calling run_scraper_in_sandbox for brand=%s", brand)
        state.validation_report = ValidationReport(
            brand=brand,
            scraper_ran=False,
            issues=[f"Sandbox runner raised an exception: {exc}"],
            sandbox_violations=["timeout_or_crash"],
            recommendation="reject",
        )
        state.status = "validation"
        return state

    exit_code: int | None = sandbox_result.get("exit_code")
    logs: str = sandbox_result.get("logs", "")
    violations: list[str] = sandbox_result.get("violations", [])

    scraper_ran = exit_code is not None  # None = timeout/crash
    logger.info(
        "Sandbox completed: exit_code=%s violations=%s scraper_ran=%s",
        exit_code, violations, scraper_ran,
    )

    issues: list[str] = []

    # ---- 2. Violation fast-path ----------------------------------------------
    if violations:
        logger.warning("Sandbox violations detected for brand=%s: %s", brand, violations)
        state.validation_report = ValidationReport(
            brand=brand,
            scraper_ran=scraper_ran,
            offers_extracted=0,
            schema_valid=False,
            confidence_score=0,
            score_breakdown={
                "base_score": 0,
                "violation_override": True,
                "final_score": 0,
            },
            issues=[f"Sandbox violation: {v}" for v in violations],
            sandbox_violations=violations,
            recommendation="reject",
        )
        state.status = "validation"
        return state

    # ---- 3. Parse offer data from logs ---------------------------------------
    offer_items: list[dict[str, Any]] = []
    try:
        parsed = json.loads(logs.strip())
        offer_items = parsed.get("offers", [])
        logger.info("Parsed %d offers from sandbox logs for brand=%s", len(offer_items), brand)
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Could not parse JSON from sandbox logs for brand=%s: %s", brand, exc)
        issues.append(f"Sandbox log is not valid JSON: {exc}")

    # ---- 4. Schema validation ------------------------------------------------
    schema_valid, schema_errors = _validate_offers(offer_items)
    valid_offer_count = sum(
        1 for i, o in enumerate(offer_items)
        if not _validate_offer(o, i)
    )
    logger.info(
        "Schema validation for brand=%s: valid=%s errors=%d",
        brand, schema_valid, len(schema_errors),
    )

    # ---- 5. Confidence score -------------------------------------------------
    confidence_score, score_breakdown = _compute_confidence_score(
        offer_items=offer_items,
        valid_offer_count=valid_offer_count,
        estimated_offer_count=estimated_offer_count,
        anti_bot_risk=anti_bot_risk,
        violations=violations,
    )

    # ---- 6. Recommendation ---------------------------------------------------
    recommendation = _determine_recommendation(confidence_score, violations)

    # ---- 7. Sample offers (up to 3) ------------------------------------------
    sample_offers = offer_items[:3]

    # ---- 8. Build ValidationReport ------------------------------------------
    report = ValidationReport(
        brand=brand,
        scraper_ran=scraper_ran,
        offers_extracted=len(offer_items),
        schema_valid=schema_valid,
        schema_errors=schema_errors,
        confidence_score=confidence_score,
        score_breakdown=score_breakdown,
        issues=issues,
        sample_offers=sample_offers,
        recommendation=recommendation,
        sandbox_violations=violations,
    )

    state.validation_report = report
    state.status = "validation"

    # Save to agent_run_outcomes table so it can be queried by the dashboard
    from database.connection import get_session
    from database.models import AgentRunOutcome
    
    # Pack sample offers and schema errors into score_breakdown for storage
    db_breakdown = dict(score_breakdown or {})
    db_breakdown["sample_offers"] = sample_offers
    db_breakdown["schema_errors"] = schema_errors
    db_breakdown["issues"] = issues
    db_breakdown["sandbox_violations"] = violations
    
    try:
        db_session = get_session()
        try:
            db_session.add(AgentRunOutcome(
                brand=brand,
                run_type="initial_validation",
                confidence_score=confidence_score,
                score_breakdown=db_breakdown,
                recommendation=recommendation,
                offers_extracted=len(offer_items),
                was_auto_approved=False,
                days_since_registration=None,
                still_healthy_at_check=None,
            ))
            db_session.commit()
            logger.info("Inserted agent_run_outcomes in validation agent for brand=%s", brand)
        except Exception as exc:
            db_session.rollback()
            logger.warning("Failed to write agent_run_outcomes in validation agent: %s", exc)
        finally:
            db_session.close()
    except Exception as exc:
        logger.warning("Could not open session for agent_run_outcomes in validation agent: %s", exc)

    logger.info(
        "Validation complete for brand=%s: score=%d recommendation=%s",
        brand, confidence_score, recommendation,
    )
    return state
