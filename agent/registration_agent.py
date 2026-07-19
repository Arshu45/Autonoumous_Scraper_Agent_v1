# agent/registration_agent.py
"""
Registration Agent — Task 6

Writes DB rows atomically when a brand passes validation (confidence >= 90,
no sandbox violations). The node is wired into the LangGraph graph as the
final step of the "auto_approve" route.

Atomicity contract:
  - Steps 3a–3c (UPSERT competitors, INSERT prefect_target_registry,
    INSERT agent_audit_log) run inside a single session.begin() block.
  - If ANY of these fail, all three are rolled back together.
  - agent_run_outcomes is written outside the critical block (it is a
    diagnostic record, not a control record).
"""

from __future__ import annotations

import logging
import os
import re

from agent.models import AgentState

logger = logging.getLogger(__name__)


def _brand_slug(brand: str) -> str:
    """Convert 'David Jones' → 'david_jones' for filename lookups."""
    return re.sub(r"[^a-z0-9]+", "_", brand.lower()).strip("_")


def run_registration(state: AgentState) -> AgentState:
    """
    Registration node for the LangGraph pipeline.

    Steps:
      1. Validate inputs (generated_artifacts, validation_report).
      2. Verify the config file written by the generation agent exists.
      3. Atomic block: UPSERT competitors, INSERT prefect_target_registry,
         INSERT agent_audit_log. Rolls back all three on any failure.
      4. Write agent_run_outcomes (non-critical; failure does not fail the node).
      5. Set state.status = "registered" and return.
    """
    from auth.approval_rbac import get_current_user
    from database.connection import get_session
    from database.models import (
        Competitor,
        AgentRunOutcome,
        AgentAuditLog,
        PrefectTargetRegistry,
    )

    brand = state.brand
    logger.info("Registration agent starting for brand=%s", brand)

    # ── 1. Input validation ────────────────────────────────────────────────────
    if not state.generated_artifacts:
        logger.error("No generated_artifacts on state — cannot register brand=%s", brand)
        state.status = "failed"
        state.error = "Missing generated_artifacts"
        return state

    if not state.validation_report:
        logger.error("No validation_report on state — cannot register brand=%s", brand)
        state.status = "failed"
        state.error = "Missing validation_report"
        return state

    config_json = state.generated_artifacts.config_json
    validation_report = state.validation_report

    # Derive key values
    user_id = get_current_user()
    slug = _brand_slug(brand)
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets", f"{slug}.json",
    )
    extraction_strategy = config_json.get("extraction_strategy", "hybrid")
    agent_confidence = validation_report.confidence_score
    source_url = config_json.get("source_url") or state.url
    agent_notes = validation_report.score_breakdown.get("notes", "") if validation_report.score_breakdown else ""

    # ── 2. Verify config file exists ───────────────────────────────────────────
    if not os.path.exists(config_path):
        logger.error(
            "Config file missing at %s — rolling back registration for brand=%s",
            config_path, brand,
        )
        state.status = "failed"
        state.error = f"Config file not found: {config_path}"
        return state

    logger.info("Config file verified at %s", config_path)

    # ── 3. Atomic DB writes ────────────────────────────────────────────────────
    session = get_session()
    try:
        with session.begin():
            # 3a. UPSERT competitors row
            competitor = session.query(Competitor).filter_by(name=brand).first()
            if competitor:
                competitor.extraction_strategy = extraction_strategy
                competitor.agent_generated = True
                competitor.agent_confidence = agent_confidence
                competitor.agent_notes = agent_notes or None
                competitor.source_url = source_url
                logger.info("Updated competitor row for brand=%s", brand)
            else:
                competitor = Competitor(
                    name=brand,
                    enabled=True,
                    extraction_strategy=extraction_strategy,
                    agent_generated=True,
                    agent_confidence=agent_confidence,
                    agent_notes=agent_notes or None,
                    source_url=source_url,
                )
                session.add(competitor)
                logger.info("Created new competitor row for brand=%s", brand)

            # 3b. INSERT INTO prefect_target_registry (UPSERT by brand)
            registry_row = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
            if registry_row:
                registry_row.config_path = config_path
                registry_row.enabled = True
                registry_row.registered_by = user_id
                logger.info("Updated prefect_target_registry for brand=%s", brand)
            else:
                session.add(PrefectTargetRegistry(
                    brand=brand,
                    config_path=config_path,
                    enabled=True,
                    registered_by=user_id,
                ))
                logger.info("Inserted prefect_target_registry for brand=%s", brand)

            # 3c. INSERT INTO agent_audit_log
            session.add(AgentAuditLog(
                brand=brand,
                user_id=user_id,
                action="approve",
                details={
                    "confidence_score": validation_report.confidence_score,
                    "recommendation": validation_report.recommendation,
                    "config_path": config_path,
                },
            ))
            logger.info("Inserted agent_audit_log for brand=%s user=%s", brand, user_id)

        logger.info("Atomic DB transaction committed for brand=%s", brand)

    except Exception as exc:
        logger.exception(
            "Atomic DB transaction failed for brand=%s — all changes rolled back: %s",
            brand, exc,
        )
        state.status = "failed"
        state.error = f"DB registration failed: {exc}"
        return state
    finally:
        # Session is already closed by context manager on commit/rollback
        pass

    # ── 4. Write agent_run_outcomes (non-critical) ─────────────────────────────
    try:
        outcome_session = get_session()
        try:
            outcome_session.add(AgentRunOutcome(
                brand=brand,
                run_type="initial_validation",
                confidence_score=validation_report.confidence_score,
                score_breakdown=validation_report.score_breakdown or {},
                recommendation=validation_report.recommendation,
                offers_extracted=validation_report.offers_extracted,
                was_auto_approved=True,
                days_since_registration=None,
                still_healthy_at_check=None,
            ))
            outcome_session.commit()
            logger.info("Inserted agent_run_outcomes for brand=%s", brand)
        except Exception as exc:
            outcome_session.rollback()
            logger.warning(
                "Failed to write agent_run_outcomes for brand=%s (non-critical): %s",
                brand, exc,
            )
        finally:
            outcome_session.close()
    except Exception as exc:
        logger.warning("Could not open session for agent_run_outcomes: %s", exc)

    # ── 5. Mark as registered ─────────────────────────────────────────────────
    state.status = "registered"
    logger.info("Registration complete for brand=%s", brand)
    return state
