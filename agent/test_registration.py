# agent/test_registration.py
"""
Standalone tests for the Registration Agent (Task 6).

Tests run against the real DB (reads DATABASE_URL from .env).
Exits 0 only if ALL 5 tests pass.

Tests:
  1. test_registration_writes_db_rows
  2. test_registration_atomic_rollback
  3. test_load_targets_db_first
  4. test_load_targets_filesystem_fallback
  5. test_audit_log_records_user
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agent.models import AgentState, GeneratedArtifacts, ValidationReport

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


def _make_state(brand: str = "TestRegBrand", config_json: dict | None = None) -> AgentState:
    """Build a fully-populated AgentState ready for registration."""
    state = AgentState(
        url="https://example.com",
        brand=brand,
        requirements="Test requirements",
    )
    state.generated_artifacts = GeneratedArtifacts(
        brand=brand,
        config_json=config_json or {
            "brand": brand,
            "source_url": "https://example.com",
            "extraction_strategy": "hybrid",
            "spider": "image_promo",
            "enabled": True,
        },
        estimated_offer_count=5,
        generation_notes="Mock artifacts for testing",
    )
    state.validation_report = ValidationReport(
        brand=brand,
        scraper_ran=True,
        offers_extracted=5,
        schema_valid=True,
        confidence_score=92,
        score_breakdown={"base": 60, "schema_bonus": 20, "offer_count_bonus": 10, "antibot_penalty": 0},
        recommendation="auto_approve",
        sandbox_violations=[],
    )
    return state


# ---------------------------------------------------------------------------
# Test 1: run_registration writes rows to all 4 tables
# ---------------------------------------------------------------------------

def test_registration_writes_db_rows() -> None:
    print("\n[test_registration_writes_db_rows]")
    from database.connection import get_session
    from database.models import (
        Competitor, PrefectTargetRegistry, AgentAuditLog, AgentRunOutcome,
    )
    from agent.registration_agent import run_registration

    brand = "RegTestBrand_T1"
    state = _make_state(brand=brand)

    # Create a real temp config file so the agent can verify it
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", brand.lower()).strip("_")
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets",
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, f"{slug}.json")
    with open(config_path, "w") as f:
        json.dump({"brand": brand, "enabled": True}, f)

    try:
        with patch("auth.approval_rbac.get_current_user", return_value="test_operator"):
            result = run_registration(state)

        passed_status = result.status == "registered"
        _record("state.status == 'registered'", passed_status, f"got: {result.status!r}")

        session = get_session()
        try:
            comp = session.query(Competitor).filter_by(name=brand).first()
            passed_competitor = comp is not None and comp.agent_generated is True
            _record("competitors row written with agent_generated=True", passed_competitor,
                    f"agent_generated={getattr(comp, 'agent_generated', None)}")

            reg = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
            passed_registry = reg is not None and reg.enabled is True
            _record("prefect_target_registry row written", passed_registry,
                    f"enabled={getattr(reg, 'enabled', None)}")

            audit = session.query(AgentAuditLog).filter_by(brand=brand, action="approve").first()
            passed_audit = audit is not None
            _record("agent_audit_log row written with action='approve'", passed_audit,
                    f"found={audit is not None}")

            outcome = session.query(AgentRunOutcome).filter_by(brand=brand, run_type="initial_validation").first()
            passed_outcome = outcome is not None and outcome.was_auto_approved is True
            _record("agent_run_outcomes row written with was_auto_approved=True", passed_outcome,
                    f"was_auto_approved={getattr(outcome, 'was_auto_approved', None)}")
        finally:
            # Cleanup test data
            try:
                session.query(AgentAuditLog).filter_by(brand=brand).delete()
                session.query(AgentRunOutcome).filter_by(brand=brand).delete()
                session.query(PrefectTargetRegistry).filter_by(brand=brand).delete()
                session.query(Competitor).filter_by(name=brand).delete()
                session.commit()
            except Exception:
                session.rollback()
            session.close()

        overall = passed_status and passed_competitor and passed_registry and passed_audit and passed_outcome
        _record("OVERALL test_registration_writes_db_rows", overall)

    finally:
        if os.path.exists(config_path):
            os.remove(config_path)


# ---------------------------------------------------------------------------
# Test 2: atomic rollback — if registry INSERT fails, no rows are committed
# ---------------------------------------------------------------------------

def test_registration_atomic_rollback() -> None:
    print("\n[test_registration_atomic_rollback]")
    from database.connection import get_session
    from database.models import Competitor, PrefectTargetRegistry, AgentAuditLog

    brand = "RegTestBrand_T2_Rollback"

    import re
    slug = re.sub(r"[^a-z0-9]+", "_", brand.lower()).strip("_")
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets",
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, f"{slug}.json")
    with open(config_path, "w") as f:
        json.dump({"brand": brand, "enabled": True}, f)

    try:
        # Patch PrefectTargetRegistry to raise on add
        original_add = None

        def _patched_add(obj):
            if isinstance(obj, PrefectTargetRegistry):
                raise RuntimeError("Simulated registry INSERT failure")
            return original_add(obj)

        from agent import registration_agent  # noqa: F401

        with patch("auth.approval_rbac.get_current_user", return_value="test_operator"):
            # Patch session.add to fail on PrefectTargetRegistry
            from database import connection as conn_module

            original_get_session = conn_module.get_session
            call_count = [0]

            def _mock_get_session():
                session = original_get_session()
                call_count[0] += 1
                if call_count[0] == 1:
                    # This is the main atomic session — wrap add
                    orig_add = session.add
                    def patched_add(obj):
                        if isinstance(obj, PrefectTargetRegistry):
                            raise RuntimeError("Simulated registry INSERT failure")
                        return orig_add(obj)
                    session.add = patched_add
                return session

            with patch("database.connection.get_session", side_effect=_mock_get_session):
                state = _make_state(brand=brand)
                from agent.registration_agent import run_registration
                result = run_registration(state)

        passed_failed = result.status == "failed"
        _record("state.status == 'failed' after rollback", passed_failed, f"got: {result.status!r}")

        # Verify no rows were committed
        session = get_session()
        try:
            comp = session.query(Competitor).filter_by(name=brand).first()
            reg = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
            audit = session.query(AgentAuditLog).filter_by(brand=brand).first()
            passed_no_competitor = comp is None
            passed_no_registry = reg is None
            passed_no_audit = audit is None
            _record("no competitors row committed", passed_no_competitor, f"found={comp}")
            _record("no prefect_target_registry row committed", passed_no_registry, f"found={reg}")
            _record("no agent_audit_log row committed", passed_no_audit, f"found={audit}")
        finally:
            session.close()

        overall = passed_failed and passed_no_competitor and passed_no_registry and passed_no_audit
        _record("OVERALL test_registration_atomic_rollback", overall)

    finally:
        if os.path.exists(config_path):
            os.remove(config_path)


# ---------------------------------------------------------------------------
# Test 3: load_targets() returns DB-registered targets when registry has rows
# ---------------------------------------------------------------------------

def test_load_targets_db_first() -> None:
    print("\n[test_load_targets_db_first]")

    # Create a real temp config file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir="/tmp", delete=False
    ) as f:
        json.dump({"brand": "DB Brand", "enabled": True, "source_url": "https://db.example.com"}, f)
        tmp_path = f.name

    try:
        # Mock a registry row pointing to the temp file
        mock_row = MagicMock()
        mock_row.config_path = tmp_path
        mock_row.enabled = True

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [mock_row]

        with patch("scripts.run_hybrid_promo_scraper.get_session", return_value=mock_session):
            # Also patch PrefectTargetRegistry import inside load_targets
            from database.models import PrefectTargetRegistry
            with patch.dict("sys.modules", {}):
                from scripts.run_hybrid_promo_scraper import load_targets
                targets = load_targets()

        passed_db_first = len(targets) >= 1 and any(t.get("brand") == "DB Brand" for t in targets)
        _record("load_targets() returns DB-registered targets", passed_db_first,
                f"brands found: {[t.get('brand') for t in targets]}")
        _record("OVERALL test_load_targets_db_first", passed_db_first)

    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test 4: load_targets() returns filesystem targets when DB is empty/unavailable
# ---------------------------------------------------------------------------

def test_load_targets_filesystem_fallback() -> None:
    print("\n[test_load_targets_filesystem_fallback]")

    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets",
    )
    os.makedirs(config_dir, exist_ok=True)

    # Write a temp target file into config/targets/
    tmp_brand_slug = "regtest_fs_fallback_brand"
    tmp_config_path = os.path.join(config_dir, f"{tmp_brand_slug}.json")
    with open(tmp_config_path, "w") as f:
        json.dump({"brand": "RegTest FS Fallback Brand", "enabled": True}, f)

    try:
        # Make DB raise an exception to trigger fallback
        with patch("scripts.run_hybrid_promo_scraper.get_session", side_effect=Exception("DB unavailable")):
            from scripts.run_hybrid_promo_scraper import load_targets
            targets = load_targets()

        passed = any(t.get("brand") == "RegTest FS Fallback Brand" for t in targets)
        _record("load_targets() falls back to filesystem when DB unavailable", passed,
                f"brands found: {[t.get('brand') for t in targets]}")

        # Also test with empty DB registry
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = []  # empty

        with patch("scripts.run_hybrid_promo_scraper.get_session", return_value=mock_session):
            targets2 = load_targets()

        passed2 = any(t.get("brand") == "RegTest FS Fallback Brand" for t in targets2)
        _record("load_targets() falls back to filesystem when DB registry is empty", passed2,
                f"brands found: {[t.get('brand') for t in targets2]}")

        overall = passed and passed2
        _record("OVERALL test_load_targets_filesystem_fallback", overall)

    finally:
        if os.path.exists(tmp_config_path):
            os.remove(tmp_config_path)


# ---------------------------------------------------------------------------
# Test 5: agent_audit_log records the correct user_id from get_current_user()
# ---------------------------------------------------------------------------

def test_audit_log_records_user() -> None:
    print("\n[test_audit_log_records_user]")
    from database.connection import get_session
    from database.models import Competitor, PrefectTargetRegistry, AgentAuditLog, AgentRunOutcome
    from agent.registration_agent import run_registration

    brand = "RegTestBrand_T5_User"
    expected_user = "ci_test_user_42"

    import re
    slug = re.sub(r"[^a-z0-9]+", "_", brand.lower()).strip("_")
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets",
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, f"{slug}.json")
    with open(config_path, "w") as f:
        json.dump({"brand": brand, "enabled": True}, f)

    try:
        with patch("auth.approval_rbac.get_current_user", return_value=expected_user):
            state = _make_state(brand=brand)
            result = run_registration(state)

        passed_status = result.status == "registered"
        _record("state.status == 'registered'", passed_status, f"got: {result.status!r}")

        session = get_session()
        try:
            audit = session.query(AgentAuditLog).filter_by(brand=brand, action="approve").first()
            passed_user = audit is not None and audit.user_id == expected_user
            _record(
                f"agent_audit_log.user_id == '{expected_user}'",
                passed_user,
                f"got: {getattr(audit, 'user_id', None)!r}",
            )

            reg = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
            passed_reg_user = reg is not None and reg.registered_by == expected_user
            _record(
                f"prefect_target_registry.registered_by == '{expected_user}'",
                passed_reg_user,
                f"got: {getattr(reg, 'registered_by', None)!r}",
            )
        finally:
            # Cleanup
            try:
                session.query(AgentAuditLog).filter_by(brand=brand).delete()
                session.query(AgentRunOutcome).filter_by(brand=brand).delete()
                session.query(PrefectTargetRegistry).filter_by(brand=brand).delete()
                session.query(Competitor).filter_by(name=brand).delete()
                session.commit()
            except Exception:
                session.rollback()
            session.close()

        overall = passed_status and passed_user and passed_reg_user
        _record("OVERALL test_audit_log_records_user", overall)

    finally:
        if os.path.exists(config_path):
            os.remove(config_path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_registration_writes_db_rows()
    test_registration_atomic_rollback()
    test_load_targets_db_first()
    test_load_targets_filesystem_fallback()
    test_audit_log_records_user()

    total = len(results)
    passed = sum(results)
    failed = total - passed

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} checks passed")

    if failed:
        print(f"❌ {failed} check(s) FAILED")
        sys.exit(1)
    else:
        print("🎉 All registration agent tests passed!")
        sys.exit(0)
