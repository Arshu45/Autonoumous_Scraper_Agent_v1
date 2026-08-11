# dashboard/test_approval_ui.py
"""
Unit and integration tests for the Streamlit human-in-the-loop approval UI callbacks.
"""

from __future__ import annotations

import json
import os
import sys
import datetime
from datetime import UTC
from sqlalchemy import text

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from database.connection import get_session
from database.models import Competitor, AgentRunOutcome, AgentAuditLog, PrefectTargetRegistry
from dashboard.approval_ui import approve_pending_target, reject_pending_target, toggle_target_status, get_pending_approvals

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

def test_approve_callback():
    print("\n[test_approve_callback]")
    brand = "TestUiApproveBrand"
    user_id = "test_operator_approve"
    
    # 1. Clean previous state
    session = get_session()
    try:
        session.query(AgentAuditLog).filter_by(brand=brand).delete()
        session.query(AgentRunOutcome).filter_by(brand=brand).delete()
        session.query(PrefectTargetRegistry).filter_by(brand=brand).delete()
        session.query(Competitor).filter_by(name=brand).delete()
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()

    # 2. Insert mock pending outcome
    session = get_session()
    saved_outcome_id = None
    try:
        outcome = AgentRunOutcome(
            brand=brand,
            run_type="initial_validation",
            confidence_score=78,
            score_breakdown={"base_score": 70, "schema_bonus": 8},
            recommendation="pending",
            offers_extracted=5,
            was_auto_approved=False,
            checked_at=datetime.datetime.now(UTC)
        )
        session.add(outcome)
        session.commit()
        saved_outcome_id = outcome.id   # save id before session closes
    except Exception as e:
        session.rollback()
        _record("setup outcome", False, f"Failed: {e}")
        return
    finally:
        session.close()

    config_json = {
        "brand": brand,
        "source_url": "https://example.com/ui-test",
        "spider": "image_promo",
        "extraction_strategy": "hybrid",
        "text_selectors": [".ui-test-text"],
        "screenshot_selectors": [],
        "enabled": True
    }

    # Call approval callback — pass outcome_id so the pending row is marked resolved
    success, msg = approve_pending_target(
        brand=brand,
        config_json=config_json,
        confidence_score=78,
        score_breakdown={"base_score": 70, "schema_bonus": 8},
        offers_extracted=5,
        user_id=user_id,
        outcome_id=saved_outcome_id
    )

    _record("approve callback return success", success, msg)

    from agent.registration_agent import _brand_slug as slug_fn
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets", f"{slug_fn(brand)}.json"
    )

    # Assert database state
    session = get_session()
    try:
        competitor = session.query(Competitor).filter_by(name=brand).first()
        _record("competitors row written", competitor is not None and competitor.agent_generated is True)

        registry = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
        _record("prefect_target_registry row written", registry is not None and registry.enabled is True)
        _record("prefect_target_registry records user", registry is not None and registry.registered_by == user_id)

        audit = session.query(AgentAuditLog).filter_by(brand=brand, action="approve").first()
        _record("agent_audit_log row written", audit is not None and audit.user_id == user_id)

        # KEY FIX: verify the original pending outcome row was resolved
        file_exists = os.path.exists(config_path)
        _record("config JSON file written to filesystem", file_exists)

        # The pending outcome row must now have recommendation != 'pending'
        pending_still = session.query(AgentRunOutcome).filter_by(
            brand=brand, recommendation="pending"
        ).count()
        _record("original pending outcome row resolved (no longer pending)", pending_still == 0)
        
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

        if os.path.exists(config_path):
            os.remove(config_path)

def test_reject_callback():
    print("\n[test_reject_callback]")
    brand = "TestUiRejectBrand"
    user_id = "test_operator_reject"
    comment = "Selectors are not robust enough"

    # 1. Clean previous state
    session = get_session()
    try:
        session.query(AgentAuditLog).filter_by(brand=brand).delete()
        session.query(AgentRunOutcome).filter_by(brand=brand).delete()
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()

    # 2. Insert mock pending outcome
    session = get_session()
    outcome_id = None
    try:
        outcome = AgentRunOutcome(
            brand=brand,
            run_type="initial_validation",
            confidence_score=72,
            score_breakdown={"base_score": 70, "schema_bonus": 2},
            recommendation="pending",
            offers_extracted=2,
            was_auto_approved=False,
            checked_at=datetime.datetime.now(UTC)
        )
        session.add(outcome)
        session.commit()
        outcome_id = outcome.id
    except Exception as e:
        session.rollback()
        _record("setup outcome", False, f"Failed: {e}")
        return
    finally:
        session.close()

    # Call rejection callback
    success, msg = reject_pending_target(
        brand=brand,
        comment=comment,
        confidence_score=72,
        user_id=user_id,
        outcome_id=outcome_id
    )

    _record("reject callback return success", success, msg)

    # Assert database state
    session = get_session()
    try:
        audit = session.query(AgentAuditLog).filter_by(brand=brand, action="reject").first()
        _record("agent_audit_log reject row written", audit is not None)
        _record("audit log records commenter and user_id", audit is not None and audit.user_id == user_id and audit.details.get("comment") == comment)

        outcome_after = session.query(AgentRunOutcome).filter_by(id=outcome_id).first()
        _record("outcome recommendation updated to reject", outcome_after is not None and outcome_after.recommendation == "reject")
    finally:
        # Cleanup
        try:
            session.query(AgentAuditLog).filter_by(brand=brand).delete()
            session.query(AgentRunOutcome).filter_by(brand=brand).delete()
            session.commit()
        except Exception:
            session.rollback()
        session.close()

def test_toggle_status_callback():
    print("\n[test_toggle_status_callback]")
    brand = "TestUiToggleBrand"
    user_id = "test_operator_toggle"

    # 1. Clean previous state
    session = get_session()
    try:
        session.query(AgentAuditLog).filter_by(brand=brand).delete()
        session.query(PrefectTargetRegistry).filter_by(brand=brand).delete()
        session.query(Competitor).filter_by(name=brand).delete()
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()

    # 2. Insert mock active target and competitor
    session = get_session()
    try:
        competitor = Competitor(name=brand, enabled=True)
        session.add(competitor)
        session.flush()
        
        target = PrefectTargetRegistry(
            brand=brand,
            config_path=f"config/targets/{brand.lower()}.json",
            enabled=True,
            registered_by=user_id,
            registered_at=datetime.datetime.now(UTC)
        )
        session.add(target)
        session.commit()
    except Exception as e:
        session.rollback()
        _record("setup target", False, f"Failed: {e}")
        return
    finally:
        session.close()

    # Call toggle status callback -> Disable
    success_disable, msg_disable = toggle_target_status(brand, False, user_id)
    _record("toggle disable return success", success_disable, msg_disable)

    # Assert disabled state
    session = get_session()
    try:
        registry = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
        _record("prefect_target_registry disabled", registry is not None and registry.enabled is False)

        competitor = session.query(Competitor).filter_by(name=brand).first()
        _record("competitors row disabled", competitor is not None and competitor.enabled is False)

        audit = session.query(AgentAuditLog).filter_by(brand=brand, action="disable_target").first()
        _record("disable audit log written", audit is not None and audit.user_id == user_id)
    finally:
        session.close()

    # Call toggle status callback -> Enable
    success_enable, msg_enable = toggle_target_status(brand, True, user_id)
    _record("toggle enable return success", success_enable, msg_enable)

    # Assert enabled state
    session = get_session()
    try:
        registry = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
        _record("prefect_target_registry enabled", registry is not None and registry.enabled is True)

        competitor = session.query(Competitor).filter_by(name=brand).first()
        _record("competitors row enabled", competitor is not None and competitor.enabled is True)

        audit = session.query(AgentAuditLog).filter_by(brand=brand, action="enable_target").first()
        _record("enable audit log written", audit is not None and audit.user_id == user_id)
    finally:
        # Cleanup
        try:
            session.query(AgentAuditLog).filter_by(brand=brand).delete()
            session.query(PrefectTargetRegistry).filter_by(brand=brand).delete()
            session.query(Competitor).filter_by(name=brand).delete()
            session.commit()
        except Exception:
            session.rollback()
        session.close()

if __name__ == "__main__":
    test_approve_callback()
    test_reject_callback()
    test_toggle_status_callback()

    total = len(results)
    passed = sum(results)
    failed = total - passed

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} checks passed")

    if failed:
        print(f"❌ {failed} check(s) FAILED")
        sys.exit(1)
    else:
        print("🎉 All approval UI callback tests passed!")
        sys.exit(0)
