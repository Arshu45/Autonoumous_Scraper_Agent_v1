"""
test_health_check.py
====================
Integration tests for the Health Check Agent (Task 8).

Tests run against the real DB (reads DATABASE_URL from .env).
Uses carefully controlled test data seeded into `agent_run_outcomes`
and `prefect_target_registry` — all test rows are cleaned up after
each test.

Test coverage (15 checks across 5 scenarios):
  1. test_healthy_target                  — target with recent positive yields → healthy
  2. test_zero_yield_collapse             — target with 3× zero-yield runs → unhealthy
  3. test_high_schema_failure_rate        — avg schema_valid_pct < 50% → unhealthy
  4. test_no_outcome_rows                 — registered target, no outcomes yet → treated healthy
  5. test_full_run_health_check           — run_health_check() end-to-end writes
                                           agent_run_outcomes rows for each target

Exit 0 only if ALL checks pass.

Usage:
    ../env/bin/python scripts/test_health_check.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

PASS = "✓"
FAIL = "✗"
results: list[bool] = []


def _record(name: str, passed: bool, detail: str = "") -> None:
    symbol = PASS if passed else FAIL
    msg = f"  {symbol} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    results.append(passed)


# ── Helpers for seeding test data ──────────────────────────────────────────────

def _seed_outcome(session, brand: str, offers_extracted: int, schema_valid_pct: float,
                  run_type: str = "initial_validation", delta_days: int = 0):
    """Insert a synthetic AgentRunOutcome row into the DB."""
    from database.models import AgentRunOutcome
    row = AgentRunOutcome(
        brand=brand,
        run_type=run_type,
        confidence_score=75,
        score_breakdown={"schema_valid_pct": schema_valid_pct, "base_score": 70},
        recommendation="pending",
        offers_extracted=offers_extracted,
        was_auto_approved=False,
        days_since_registration=delta_days,
        still_healthy_at_check=None,
        checked_at=datetime.now(timezone.utc) - timedelta(days=delta_days),
    )
    session.add(row)


def _seed_registry(session, brand: str):
    """Upsert a PrefectTargetRegistry row for a test brand."""
    from database.models import PrefectTargetRegistry
    existing = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
    if existing:
        existing.enabled = True
        existing.registered_at = datetime.now(timezone.utc)
    else:
        session.add(PrefectTargetRegistry(
            brand=brand,
            config_path=f"/fake/config/{brand.replace(' ', '_').lower()}.json",
            enabled=True,
            registered_by="test_operator",
            registered_at=datetime.now(timezone.utc),
        ))


def _cleanup(session, brand: str):
    """Delete all test rows for a brand."""
    from database.models import AgentRunOutcome, PrefectTargetRegistry
    try:
        session.query(AgentRunOutcome).filter_by(brand=brand).delete()
        session.query(PrefectTargetRegistry).filter_by(brand=brand).delete()
        session.commit()
    except Exception:
        session.rollback()


# ── Test 1: Healthy target ─────────────────────────────────────────────────────

def test_healthy_target() -> None:
    """A target with positive offer yields across 3 runs must be flagged as healthy."""
    print("\n[test_healthy_target]")
    from database.connection import get_session
    from scripts.run_health_check import _analyse_target_health, _get_recent_outcomes

    brand = "HealthCheck_TestBrand_Healthy"
    session = get_session()
    try:
        # Seed 3 recent runs with positive yields
        _seed_outcome(session, brand, offers_extracted=12, schema_valid_pct=100.0, delta_days=0)
        _seed_outcome(session, brand, offers_extracted=8,  schema_valid_pct=90.0,  delta_days=1)
        _seed_outcome(session, brand, offers_extracted=15, schema_valid_pct=95.0,  delta_days=2)
        session.commit()

        recent = _get_recent_outcomes(session, brand)
        result = _analyse_target_health(brand, recent)

        passed_healthy = result["still_healthy"] is True
        passed_no_reason = result["unhealthy_reason"] is None
        passed_yields = len(result["recent_yield_counts"]) == 3
        passed_all_positive = all(y > 0 for y in result["recent_yield_counts"])

        _record("still_healthy == True for positive-yield target", passed_healthy,
                f"got: {result['still_healthy']}")
        _record("unhealthy_reason is None", passed_no_reason,
                f"got: {result['unhealthy_reason']!r}")
        _record("3 yield counts returned", passed_yields,
                f"got: {len(result['recent_yield_counts'])}")
        _record("all yield counts > 0", passed_all_positive,
                f"counts: {result['recent_yield_counts']}")

        overall = passed_healthy and passed_no_reason and passed_yields and passed_all_positive
        _record("OVERALL test_healthy_target", overall)

    finally:
        _cleanup(session, brand)
        session.close()


# ── Test 2: Zero-yield collapse ────────────────────────────────────────────────

def test_zero_yield_collapse() -> None:
    """A target with 3 consecutive zero-yield runs must be flagged as unhealthy."""
    print("\n[test_zero_yield_collapse]")
    from database.connection import get_session
    from scripts.run_health_check import _analyse_target_health, _get_recent_outcomes

    brand = "HealthCheck_TestBrand_ZeroYield"
    session = get_session()
    try:
        # Seed 3 zero-yield runs
        _seed_outcome(session, brand, offers_extracted=0, schema_valid_pct=0.0, delta_days=0)
        _seed_outcome(session, brand, offers_extracted=0, schema_valid_pct=0.0, delta_days=1)
        _seed_outcome(session, brand, offers_extracted=0, schema_valid_pct=0.0, delta_days=2)
        session.commit()

        recent = _get_recent_outcomes(session, brand)
        result = _analyse_target_health(brand, recent)

        passed_unhealthy = result["still_healthy"] is False
        passed_reason = result["unhealthy_reason"] == "zero_yield"
        passed_all_zero = all(y == 0 for y in result["recent_yield_counts"])

        _record("still_healthy == False for zero-yield target", passed_unhealthy,
                f"got: {result['still_healthy']}")
        _record("unhealthy_reason == 'zero_yield'", passed_reason,
                f"got: {result['unhealthy_reason']!r}")
        _record("all yield counts == 0", passed_all_zero,
                f"counts: {result['recent_yield_counts']}")

        overall = passed_unhealthy and passed_reason and passed_all_zero
        _record("OVERALL test_zero_yield_collapse", overall)

    finally:
        _cleanup(session, brand)
        session.close()


# ── Test 3: High schema failure rate ──────────────────────────────────────────

def test_high_schema_failure_rate() -> None:
    """A target with avg schema_valid_pct < 50 must be flagged as unhealthy."""
    print("\n[test_high_schema_failure_rate]")
    from database.connection import get_session
    from scripts.run_health_check import _analyse_target_health, _get_recent_outcomes

    brand = "HealthCheck_TestBrand_SchemaFail"
    session = get_session()
    try:
        # Seed 3 runs with high schema failure (avg ~20%)
        # Yields > 0 so zero-yield check does NOT trigger
        _seed_outcome(session, brand, offers_extracted=5, schema_valid_pct=30.0, delta_days=0)
        _seed_outcome(session, brand, offers_extracted=3, schema_valid_pct=10.0, delta_days=1)
        _seed_outcome(session, brand, offers_extracted=7, schema_valid_pct=20.0, delta_days=2)
        session.commit()

        recent = _get_recent_outcomes(session, brand)
        result = _analyse_target_health(brand, recent)

        passed_unhealthy = result["still_healthy"] is False
        passed_reason = result["unhealthy_reason"] == "high_schema_failure_rate"
        # avg should be (30+10+20)/3 = 20%
        avg_pct = result.get("avg_schema_valid_pct", None)
        passed_avg = avg_pct is not None and avg_pct < 50.0

        _record("still_healthy == False for high-schema-failure target", passed_unhealthy,
                f"got: {result['still_healthy']}")
        _record("unhealthy_reason == 'high_schema_failure_rate'", passed_reason,
                f"got: {result['unhealthy_reason']!r}")
        _record("avg_schema_valid_pct < 50%", passed_avg,
                f"avg={avg_pct}")

        overall = passed_unhealthy and passed_reason and passed_avg
        _record("OVERALL test_high_schema_failure_rate", overall)

    finally:
        _cleanup(session, brand)
        session.close()


# ── Test 4: No outcome rows yet ───────────────────────────────────────────────

def test_no_outcome_rows() -> None:
    """A freshly registered target with no outcome rows must be treated as healthy (data gap)."""
    print("\n[test_no_outcome_rows]")
    from scripts.run_health_check import _analyse_target_health

    brand = "HealthCheck_TestBrand_NoOutcomes"

    # Pass empty list — no DB rows
    result = _analyse_target_health(brand, [])

    passed_healthy = result["still_healthy"] is True
    passed_no_reason = result["unhealthy_reason"] is None
    passed_no_yields = result["recent_yield_counts"] == []
    passed_note = result.get("note") == "no_outcome_rows_yet"

    _record("still_healthy == True when no outcome rows", passed_healthy,
            f"got: {result['still_healthy']}")
    _record("unhealthy_reason is None (data gap, not unhealthy)", passed_no_reason,
            f"got: {result['unhealthy_reason']!r}")
    _record("recent_yield_counts is empty list", passed_no_yields,
            f"got: {result['recent_yield_counts']}")
    _record("note == 'no_outcome_rows_yet'", passed_note,
            f"got: {result.get('note')!r}")

    overall = passed_healthy and passed_no_reason and passed_no_yields and passed_note
    _record("OVERALL test_no_outcome_rows", overall)


# ── Test 5: Full run_health_check() end-to-end ────────────────────────────────

def test_full_run_health_check() -> None:
    """
    End-to-end test: seed 2 targets (one healthy, one unhealthy) in the DB,
    call run_health_check(), verify it writes health_check outcome rows and
    returns the correct summary.
    """
    print("\n[test_full_run_health_check]")
    from database.connection import get_session
    from database.models import AgentRunOutcome
    from scripts.run_health_check import run_health_check

    brand_healthy = "HealthCheck_E2E_GoodTarget"
    brand_unhealthy = "HealthCheck_E2E_BadTarget"

    session = get_session()
    try:
        # Seed registry rows
        _seed_registry(session, brand_healthy)
        _seed_registry(session, brand_unhealthy)
        session.commit()

        # Seed outcome rows — healthy brand has positive yields
        _seed_outcome(session, brand_healthy, offers_extracted=10, schema_valid_pct=95.0, delta_days=0)
        _seed_outcome(session, brand_healthy, offers_extracted=8,  schema_valid_pct=90.0, delta_days=1)
        _seed_outcome(session, brand_healthy, offers_extracted=12, schema_valid_pct=100.0, delta_days=2)

        # Unhealthy brand has 3× zero-yield
        _seed_outcome(session, brand_unhealthy, offers_extracted=0, schema_valid_pct=0.0, delta_days=0)
        _seed_outcome(session, brand_unhealthy, offers_extracted=0, schema_valid_pct=0.0, delta_days=1)
        _seed_outcome(session, brand_unhealthy, offers_extracted=0, schema_valid_pct=0.0, delta_days=2)

        session.commit()
        session.close()

        # Run the full health check
        summary = run_health_check()

        # Verify summary structure
        passed_checked = summary["targets_checked"] >= 2
        passed_unhealthy_count = summary["unhealthy"] >= 1
        passed_healthy_count = summary["healthy"] >= 1
        passed_bad_in_list = brand_unhealthy in summary.get("unhealthy_brands", [])

        _record("targets_checked >= 2", passed_checked,
                f"got: {summary['targets_checked']}")
        _record("unhealthy >= 1", passed_unhealthy_count,
                f"got: {summary['unhealthy']}")
        _record("healthy >= 1", passed_healthy_count,
                f"got: {summary['healthy']}")
        _record(f"'{brand_unhealthy}' appears in unhealthy_brands", passed_bad_in_list,
                f"list: {summary.get('unhealthy_brands')}")

        # Verify health_check rows were written to DB
        verify_session = get_session()
        try:
            hc_row_healthy = (
                verify_session.query(AgentRunOutcome)
                .filter_by(brand=brand_healthy, run_type="health_check")
                .order_by(AgentRunOutcome.checked_at.desc())
                .first()
            )
            hc_row_unhealthy = (
                verify_session.query(AgentRunOutcome)
                .filter_by(brand=brand_unhealthy, run_type="health_check")
                .order_by(AgentRunOutcome.checked_at.desc())
                .first()
            )

            passed_wrote_healthy_row = (
                hc_row_healthy is not None and hc_row_healthy.still_healthy_at_check is True
            )
            passed_wrote_unhealthy_row = (
                hc_row_unhealthy is not None and hc_row_unhealthy.still_healthy_at_check is False
            )
            passed_unhealthy_reason = (
                hc_row_unhealthy is not None
                and isinstance(hc_row_unhealthy.score_breakdown, dict)
                and hc_row_unhealthy.score_breakdown.get("unhealthy_reason") == "zero_yield"
            )

            _record("health_check row written for healthy brand (still_healthy=True)",
                    passed_wrote_healthy_row,
                    f"still_healthy={getattr(hc_row_healthy, 'still_healthy_at_check', None)}")
            _record("health_check row written for unhealthy brand (still_healthy=False)",
                    passed_wrote_unhealthy_row,
                    f"still_healthy={getattr(hc_row_unhealthy, 'still_healthy_at_check', None)}")
            _record("unhealthy reason 'zero_yield' persisted in score_breakdown JSONB",
                    passed_unhealthy_reason,
                    f"breakdown: {getattr(hc_row_unhealthy, 'score_breakdown', None)}")

        finally:
            verify_session.close()

        overall = (
            passed_checked and passed_unhealthy_count and passed_healthy_count
            and passed_bad_in_list and passed_wrote_healthy_row
            and passed_wrote_unhealthy_row and passed_unhealthy_reason
        )
        _record("OVERALL test_full_run_health_check", overall)

    except Exception as exc:
        _record("OVERALL test_full_run_health_check", False, f"exception: {exc}")
        import traceback
        traceback.print_exc()

    finally:
        cleanup_session = get_session()
        _cleanup(cleanup_session, brand_healthy)
        _cleanup(cleanup_session, brand_unhealthy)
        cleanup_session.close()


# ── Runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("HEALTH CHECK AGENT — TEST SUITE")
    print("=" * 60)

    test_healthy_target()
    test_zero_yield_collapse()
    test_high_schema_failure_rate()
    test_no_outcome_rows()
    test_full_run_health_check()

    total = len(results)
    passed = sum(results)
    failed = total - passed

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} checks passed")

    if failed:
        print(f"❌ {failed} check(s) FAILED")
        sys.exit(1)
    else:
        print("🎉 All health check agent tests passed!")
        sys.exit(0)
