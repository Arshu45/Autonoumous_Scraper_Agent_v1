# agent/test_validation.py
"""
Standalone tests for the Validation Agent (Task 5).

Mocks run_scraper_in_sandbox so no Docker daemon is needed.
Exits 0 if all tests pass, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.models import AgentState, GeneratedArtifacts, SiteAnalysis
from agent.validation_agent import run_validation_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc).isoformat()

def _make_offer(
    *,
    title: str = "Up to 50% off selected styles",
    confidence: str = "high",
    category: str | None = "Womens",
    discount_min: float | None = 20.0,
    discount_max: float | None = 50.0,
    brand: str = "TestBrand",
    source_url: str = "https://example.com",
) -> dict:
    return {
        "source":       "text_scraper",
        "brand":        brand,
        "source_url":   source_url,
        "title":        title,
        "category":     category,
        "discount_min": discount_min,
        "discount_max": discount_max,
        "confidence":   confidence,
        "scraped_at":   NOW,
    }


def _make_state(
    brand: str = "TestBrand",
    estimated_offer_count: int = 5,
    anti_bot_risk: str = "low",
) -> AgentState:
    state = AgentState(
        url="https://example.com",
        brand=brand,
        requirements="Test requirements",
    )
    state.site_analysis = SiteAnalysis(
        url="https://example.com",
        brand=brand,
        screenshot_path="mock.png",
        dom_html="<html></html>",
        extraction_strategy="text",
        anti_bot_risk=anti_bot_risk,
    )
    state.generated_artifacts = GeneratedArtifacts(
        brand=brand,
        config_json={"brand": brand, "source_url": "https://example.com"},
        scraper_code=None,
        estimated_offer_count=estimated_offer_count,
    )
    return state


def _logs_for_offers(offers: list[dict]) -> str:
    return json.dumps({"offers": offers, "count": len(offers)})


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Test 1: clean run with 5 valid offers → auto_approve, score >= 90
# ---------------------------------------------------------------------------

def test_clean_5_offers() -> None:
    print("\n[test_clean_5_offers]")
    offers = [_make_offer() for _ in range(5)]

    mock_result = {
        "exit_code":  0,
        "logs":       _logs_for_offers(offers),
        "violations": [],
    }

    state = _make_state(estimated_offer_count=5, anti_bot_risk="low")

    with patch("agent.validation_agent.run_scraper_in_sandbox", return_value=mock_result):
        out = run_validation_agent(state)

    r = out.validation_report
    assert r is not None, "validation_report must not be None"

    passed_recommendation = r.recommendation == "auto_approve"
    passed_score          = r.confidence_score >= 90
    passed_offers         = r.offers_extracted == 5
    passed_schema         = r.schema_valid is True
    passed_sample         = len(r.sample_offers) <= 3
    passed_violations     = r.sandbox_violations == []
    passed_status         = out.status == "validation"

    _record("recommendation == auto_approve",  passed_recommendation, f"got: {r.recommendation!r}")
    _record("confidence_score >= 90",          passed_score,          f"got: {r.confidence_score}")
    _record("offers_extracted == 5",           passed_offers,         f"got: {r.offers_extracted}")
    _record("schema_valid is True",            passed_schema,         f"got: {r.schema_valid}")
    _record("sample_offers <= 3",             passed_sample,          f"got: {len(r.sample_offers)}")
    _record("no sandbox_violations",           passed_violations,     f"got: {r.sandbox_violations}")
    _record("state.status == validation",      passed_status,         f"got: {out.status!r}")

    overall = all([
        passed_recommendation, passed_score, passed_offers,
        passed_schema, passed_sample, passed_violations, passed_status,
    ])
    _record("OVERALL test_clean_5_offers", overall)


# ---------------------------------------------------------------------------
# Test 2: zero offers, exit 0, no violations → reject, score = 10
# ---------------------------------------------------------------------------

def test_zero_offers() -> None:
    print("\n[test_zero_offers]")

    mock_result = {
        "exit_code":  0,
        "logs":       _logs_for_offers([]),
        "violations": [],
    }

    state = _make_state(estimated_offer_count=5, anti_bot_risk="low")

    with patch("agent.validation_agent.run_scraper_in_sandbox", return_value=mock_result):
        out = run_validation_agent(state)

    r = out.validation_report
    assert r is not None

    passed_recommendation = r.recommendation == "reject"
    passed_score          = r.confidence_score == 10
    passed_offers         = r.offers_extracted == 0
    passed_violations     = r.sandbox_violations == []

    _record("recommendation == reject",   passed_recommendation, f"got: {r.recommendation!r}")
    _record("confidence_score == 10",     passed_score,          f"got: {r.confidence_score}")
    _record("offers_extracted == 0",      passed_offers,         f"got: {r.offers_extracted}")
    _record("no sandbox_violations",      passed_violations,     f"got: {r.sandbox_violations}")

    overall = all([passed_recommendation, passed_score, passed_offers, passed_violations])
    _record("OVERALL test_zero_offers", overall)


# ---------------------------------------------------------------------------
# Test 3: sandbox violations present → reject, score = 0
# ---------------------------------------------------------------------------

def test_sandbox_violation() -> None:
    print("\n[test_sandbox_violation]")

    mock_result = {
        "exit_code":  1,
        "logs":       "read-only file system",
        "violations": ["filesystem_violation"],
    }

    state = _make_state()

    with patch("agent.validation_agent.run_scraper_in_sandbox", return_value=mock_result):
        out = run_validation_agent(state)

    r = out.validation_report
    assert r is not None

    passed_recommendation = r.recommendation == "reject"
    passed_score          = r.confidence_score == 0
    passed_violations     = "filesystem_violation" in r.sandbox_violations

    _record("recommendation == reject",             passed_recommendation, f"got: {r.recommendation!r}")
    _record("confidence_score == 0",               passed_score,          f"got: {r.confidence_score}")
    _record("filesystem_violation in violations",  passed_violations,     f"got: {r.sandbox_violations}")

    overall = all([passed_recommendation, passed_score, passed_violations])
    _record("OVERALL test_sandbox_violation", overall)


# ---------------------------------------------------------------------------
# Test 4: 5 offers + anti_bot_risk="high" → score reduced by 20
# ---------------------------------------------------------------------------

def test_high_antibot_penalty() -> None:
    print("\n[test_high_antibot_penalty]")

    offers = [_make_offer() for _ in range(5)]

    mock_result = {
        "exit_code":  0,
        "logs":       _logs_for_offers(offers),
        "violations": [],
    }

    # With low risk (baseline), score should be ≥ 90 (auto_approve)
    state_low = _make_state(estimated_offer_count=5, anti_bot_risk="low")
    with patch("agent.validation_agent.run_scraper_in_sandbox", return_value=mock_result):
        out_low = run_validation_agent(state_low)

    # With high risk, score should be 20 less
    state_high = _make_state(estimated_offer_count=5, anti_bot_risk="high")
    with patch("agent.validation_agent.run_scraper_in_sandbox", return_value=mock_result):
        out_high = run_validation_agent(state_high)

    score_low  = out_low.validation_report.confidence_score
    score_high = out_high.validation_report.confidence_score
    penalty    = score_low - score_high

    passed_penalty  = penalty == 20
    passed_breakdown = out_high.validation_report.score_breakdown.get("antibot_penalty") == -20

    _record(
        "high-risk score is exactly 20 lower than low-risk score",
        passed_penalty,
        f"low={score_low} high={score_high} diff={penalty}",
    )
    _record(
        "score_breakdown.antibot_penalty == -20",
        passed_breakdown,
        f"got: {out_high.validation_report.score_breakdown.get('antibot_penalty')}",
    )

    overall = passed_penalty and passed_breakdown
    _record("OVERALL test_high_antibot_penalty", overall)


# ---------------------------------------------------------------------------
# Test 5: exit_code=None + violations=["timeout_or_crash"] → reject, score=0
# ---------------------------------------------------------------------------

def test_timeout_crash() -> None:
    print("\n[test_timeout_crash]")

    mock_result = {
        "exit_code":  None,
        "logs":       "[runner error]: container wait timed out",
        "violations": ["timeout_or_crash"],
    }

    state = _make_state()

    with patch("agent.validation_agent.run_scraper_in_sandbox", return_value=mock_result):
        out = run_validation_agent(state)

    r = out.validation_report
    assert r is not None

    passed_recommendation = r.recommendation == "reject"
    passed_score          = r.confidence_score == 0
    passed_violations     = "timeout_or_crash" in r.sandbox_violations
    passed_ran            = r.scraper_ran is False   # exit_code is None → not ran

    _record("recommendation == reject",             passed_recommendation, f"got: {r.recommendation!r}")
    _record("confidence_score == 0",               passed_score,          f"got: {r.confidence_score}")
    _record("timeout_or_crash in violations",       passed_violations,     f"got: {r.sandbox_violations}")
    _record("scraper_ran is False (exit_code=None)",passed_ran,            f"got: {r.scraper_ran}")

    overall = all([passed_recommendation, passed_score, passed_violations, passed_ran])
    _record("OVERALL test_timeout_crash", overall)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_clean_5_offers()
    test_zero_offers()
    test_sandbox_violation()
    test_high_antibot_penalty()
    test_timeout_crash()

    total  = len(results)
    passed = sum(results)
    failed = total - passed

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} checks passed")

    if failed:
        print(f"❌ {failed} check(s) FAILED")
        sys.exit(1)
    else:
        print("🎉 All validation agent tests passed!")
        sys.exit(0)
