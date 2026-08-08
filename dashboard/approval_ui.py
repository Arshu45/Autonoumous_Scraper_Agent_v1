# dashboard/approval_ui.py
"""
Premium Streamlit Operator Workspace for Human-in-the-Loop Scraper Approvals.
"""

import streamlit as st
import pandas as pd
import json
import os
import sys
import datetime
from datetime import UTC
from sqlalchemy import text

# Add root folder to python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import get_session
from database.models import Competitor, AgentRunOutcome, AgentAuditLog, PrefectTargetRegistry
from auth.approval_rbac import get_current_user, AgentRole
from agent.registration_agent import run_registration, _brand_slug

# Set page config
st.set_page_config(
    page_title="Agent Operator Workspace",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Apply shared styles
from dashboard.utils.styles import apply_css, page_header, section_label, kpi_card
apply_css()

# Custom Premium Styling
st.markdown("""
<style>
/* Metric badges */
.score-badge {
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 1.1rem;
    font-weight: 600;
    display: inline-block;
    margin-bottom: 1rem;
    text-align: center;
    width: 100%;
}
.score-high {
    color: #1A8A68;
    background-color: rgba(31,175,138,0.12);
    border: 1px solid rgba(31,175,138,0.3);
}
.score-medium {
    color: #9A7020;
    background-color: rgba(180,130,40,0.12);
    border: 1px solid rgba(180,130,40,0.3);
}
.score-low {
    color: #C0354A;
    background-color: rgba(208,74,106,0.12);
    border: 1px solid rgba(208,74,106,0.3);
}

/* Sidebar connection indicator */
.conn-status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.85rem;
    color: #505060;
    margin-bottom: 1.5rem;
}
.dot-green {
    height: 10px;
    width: 10px;
    background-color: #1FAF8A;
    border-radius: 50%;
    display: inline-block;
}
.dot-red {
    height: 10px;
    width: 10px;
    background-color: #D04A6A;
    border-radius: 50%;
    display: inline-block;
}

/* Flex layout helper */
.flex-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
}
</style>
""", unsafe_allow_html=True)

# ── DB Callback Operations ───────────────────────────────────────────────────

def get_pending_approvals(session):
    query = """
    WITH latest_outcomes AS (
        SELECT DISTINCT ON (brand) id, brand, run_type, confidence_score, score_breakdown, recommendation, checked_at
        FROM agent_run_outcomes
        WHERE run_type = 'initial_validation'
        ORDER BY brand, checked_at DESC
    ),
    latest_audit AS (
        SELECT DISTINCT ON (brand) brand, action, created_at
        FROM agent_audit_log
        WHERE action IN ('approve', 'reject')
        ORDER BY brand, created_at DESC
    )
    SELECT o.id, o.brand, o.confidence_score, o.score_breakdown, o.recommendation, o.checked_at
    FROM latest_outcomes o
    LEFT JOIN latest_audit a ON o.brand = a.brand
    WHERE (o.recommendation = 'pending' OR (o.confidence_score >= 70 AND o.confidence_score <= 89))
      AND (a.created_at IS NULL OR a.created_at < o.checked_at)
    ORDER BY o.checked_at DESC
    """
    return session.execute(text(query)).mappings().all()

def approve_pending_target(brand, config_json, confidence_score, score_breakdown, offers_extracted, user_id, outcome_id=None):
    from agent.models import AgentState, GeneratedArtifacts, ValidationReport

    slug = _brand_slug(brand)
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets"
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, f"{slug}.json")

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_json, f, indent=2)

    source_url = config_json.get("source_url", "")
    if not isinstance(source_url, str):
        source_url = (source_url or [""])[0]

    state = AgentState(url=source_url, brand=brand, requirements="")
    state.generated_artifacts = GeneratedArtifacts(
        brand=brand,
        config_json=config_json,
        estimated_offer_count=offers_extracted,
        generation_notes="operator_approved"
    )
    state.validation_report = ValidationReport(
        brand=brand,
        scraper_ran=True,
        offers_extracted=offers_extracted,
        schema_valid=True,
        confidence_score=confidence_score,
        score_breakdown=score_breakdown,
        recommendation="auto_approve",   # route directly to registration block
        sandbox_violations=[]
    )

    old_user_id = os.environ.get("AGENT_USER_ID")
    os.environ["AGENT_USER_ID"] = user_id
    try:
        res = run_registration(state)
        if res.status == "registered":
            # Fix: mark the original pending outcome row as resolved so it
            # no longer appears in the pending queue.
            resolve_session = get_session()
            try:
                with resolve_session.begin():
                    _mark_outcome_resolved(
                        resolve_session, brand,
                        outcome_id or -1, "approved"
                    )
            except Exception:
                pass   # non-critical — queue exclusion already handled by CTE
            finally:
                resolve_session.close()
            return True, "Target registered successfully."
        else:
            return False, f"Registration failed: {res.error}"
    finally:
        if old_user_id is not None:
            os.environ["AGENT_USER_ID"] = old_user_id
        else:
            os.environ.pop("AGENT_USER_ID", None)

def _mark_outcome_resolved(session, brand: str, outcome_id: int, new_recommendation: str) -> None:
    """Update the original pending outcome row so it no longer appears in the queue."""
    # First try to find by id (most precise)
    outcome = session.query(AgentRunOutcome).filter_by(id=outcome_id).first()
    if outcome:
        outcome.recommendation = new_recommendation
    else:
        # Fallback: update the latest pending row for this brand
        outcome = (
            session.query(AgentRunOutcome)
            .filter_by(brand=brand, recommendation="pending")
            .order_by(AgentRunOutcome.checked_at.desc())
            .first()
        )
        if outcome:
            outcome.recommendation = new_recommendation


def reject_pending_target(brand, comment, confidence_score, user_id, outcome_id):
    session = get_session()
    try:
        with session.begin():
            session.add(AgentAuditLog(
                brand=brand,
                user_id=user_id,
                action="reject",
                details={
                    "comment": comment,
                    "confidence_score": confidence_score
                }
            ))
            _mark_outcome_resolved(session, brand, outcome_id, "reject")

        return True, "Target rejected successfully."
    except Exception as e:
        session.rollback()
        return False, f"Rejection failed: {e}"
    finally:
        session.close()

def toggle_target_status(brand, enabled, user_id):
    session = get_session()
    try:
        with session.begin():
            target = session.query(PrefectTargetRegistry).filter_by(brand=brand).first()
            if target:
                target.enabled = enabled
                
            competitor = session.query(Competitor).filter_by(name=brand).first()
            if competitor:
                competitor.enabled = enabled
                
            action = "enable_target" if enabled else "disable_target"
            audit = AgentAuditLog(
                brand=brand,
                user_id=user_id,
                action=action,
                details={"enabled": enabled}
            )
            session.add(audit)
            
        return True, f"Target status updated to {'enabled' if enabled else 'disabled'}."
    except Exception as e:
        session.rollback()
        return False, f"Status update failed: {e}"
    finally:
        session.close()

def seed_mock_pending_run(brand_name, score=78):
    slug = _brand_slug(brand_name)
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "targets"
    )
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, f"{slug}.json")
    
    mock_config = {
        "brand": brand_name,
        "source_url": f"https://www.{slug}.com/sale",
        "spider": "image_promo",
        "extraction_strategy": "hybrid",
        "text_selectors": [".sale-title", "[class*='discount']"],
        "screenshot_selectors": [".hero-banner", ".promo-card"],
        "min_image_width": 400,
        "min_image_height": 150,
        "min_aspect_ratio": 1.2,
        "request_delay_seconds": 4,
        "scroll_depth": 2,
        "enabled": True
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(mock_config, f, indent=2)
        
    session = get_session()
    try:
        outcome = session.query(AgentRunOutcome).filter_by(brand=brand_name, run_type="initial_validation").first()
        if outcome:
            outcome.confidence_score = score
            outcome.recommendation = "pending"
            outcome.checked_at = datetime.datetime.now(UTC)
        else:
            outcome = AgentRunOutcome(
                brand=brand_name,
                run_type="initial_validation",
                confidence_score=score,
                score_breakdown={
                    "base_score": 70,
                    "schema_valid_pct": 100.0,
                    "schema_bonus": 20,
                    "title_bonus": 5,
                    "category_bonus": 5,
                    "discount_bonus": 0,
                    "antibot_penalty": -12,
                    "final_score": score,
                    "notes": "Validation yielded average results. Some selectors might need manual tuning.",
                    "sample_offers": [
                        {"title": "Special Price - 40% Off", "confidence": "high", "category": "Womens", "discount_min": 40.0},
                        {"title": "Selected Jackets at Half Price", "confidence": "medium", "category": "Menswear", "discount_min": 50.0},
                        {"title": "Summer dresses from $29", "confidence": "high", "category": "Kids"}
                    ],
                    "schema_errors": [
                        "offer[2]: 'discount_min' is missing (warning)"
                    ],
                    "issues": [],
                    "sandbox_violations": []
                },
                recommendation="pending",
                offers_extracted=4,
                was_auto_approved=False,
                checked_at=datetime.datetime.utcnow()
            )
            session.add(outcome)
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()

# ── Sidebar Setup ────────────────────────────────────────────────────────────

st.sidebar.markdown("""
<div style="padding: 0.5rem 0 1rem 0;">
    <div style="font-size: 0.7rem; font-weight: 600; color: #A0A0B0; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.5rem;">Operator Panel</div>
    <div style="font-size: 1rem; font-weight: 600; color: #1A1A2E; letter-spacing: -0.01em;">Scraper Management</div>
</div>
<hr style="border-color: #E0E0EA; margin: 0 0 1.5rem 0;">
""", unsafe_allow_html=True)

# DB Connectivity check
try:
    db_test_session = get_session()
    db_test_session.execute(text("SELECT 1"))
    db_test_session.close()
    st.sidebar.markdown('<div class="conn-status"><span class="dot-green"></span> Database Online</div>', unsafe_allow_html=True)
    db_connected = True
except Exception as e:
    st.sidebar.markdown(f'<div class="conn-status"><span class="dot-red"></span> Database Offline: {e}</div>', unsafe_allow_html=True)
    db_connected = False

# Active user profile
sim_user = st.sidebar.text_input("Operator User ID", value=get_current_user())

# Sidebar metrics
if db_connected:
    session = get_session()
    try:
        pending_count = len(get_pending_approvals(session))
        active_count = session.query(PrefectTargetRegistry).filter_by(enabled=True).count()
        audit_count = session.query(AgentAuditLog).count()
    except Exception:
        pending_count = active_count = audit_count = 0
    finally:
        session.close()
    
    st.sidebar.markdown("### Operational Summary")
    st.sidebar.markdown(f"⏳ **Pending Reviews:** {pending_count}")
    st.sidebar.markdown(f"🚀 **Active Targets:** {active_count}")
    st.sidebar.markdown(f"📋 **Audit Events:** {audit_count}")

# Developer tools
if db_connected:
    with st.sidebar.expander("🛠️ Developer Tools"):
        st.markdown("Seed a mock pending run to test the workspace:")
        dev_brand = st.text_input("Mock Brand", "Mock Zara")
        dev_score = st.slider("Mock Score", 70, 89, 78)
        if st.button("Seed Mock Run"):
            try:
                seed_mock_pending_run(dev_brand, dev_score)
                st.success(f"Seeded mock run for {dev_brand}!")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to seed: {e}")

# ── Main Content ─────────────────────────────────────────────────────────────

page_header("Agent Operator Workspace", "Human-in-the-loop approvals, target registry management, and immutable audit logs.")

if not db_connected:
    st.error("Cannot connect to the database. Please check your database settings and make sure the PostgreSQL service is running.")
    st.stop()

# Tabs
tab_pending, tab_active, tab_audit, tab_add_new = st.tabs([
    "⏳ Pending Approvals Queue",
    "🚀 Active Registry",
    "📋 Audit Trail Log",
    "🔍 Trigger Exploration Agent"
])

# ── Tab 1: Pending Approvals Queue ───────────────────────────────────────────
with tab_pending:
    st.markdown('<div class="section-label" style="margin-top:0.5rem;">Review Queue</div>', unsafe_allow_html=True)
    
    session = get_session()
    try:
        pending_runs = get_pending_approvals(session)
    finally:
        session.close()
        
    if not pending_runs:
        st.info("🎉 **All clear!** There are currently no pending scraper configurations requiring human approval.")
        st.markdown("""
        To test the queue:
        1. Run the scraper agent on a new target site using `scripts/run_scraper_agent.py`.
        2. Or use the **Developer Tools** in the sidebar to seed a mock pending configuration.
        """)
    else:
        brand_options = [r["brand"] for r in pending_runs]
        selected_brand = st.selectbox("Select brand to review", brand_options)
        
        selected_run = next(r for r in pending_runs if r["brand"] == selected_brand)
        brand = selected_run["brand"]
        slug = _brand_slug(brand)
        config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "targets")
        config_path = os.path.join(config_dir, f"{slug}.json")
        
        config_json = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_json = json.load(f)
            except Exception as e:
                st.warning(f"Could not read config file: {e}")
        else:
            config_json = {
                "brand": brand,
                "source_url": f"https://www.{slug}.com/sale",
                "spider": "image_promo",
                "extraction_strategy": "hybrid",
                "text_selectors": [],
                "screenshot_selectors": [],
                "enabled": True
            }
            
        config_str = json.dumps(config_json, indent=2)
        
        col_left, col_right = st.columns([1, 1])
        
        with col_left:
            st.markdown("**Configuration Editor**")
            edited_config_str = st.text_area(
                "Config JSON Source",
                value=config_str,
                height=450,
                key=f"editor_{brand}"
            )
            
            is_valid_json = True
            parsed_config = {}
            try:
                parsed_config = json.loads(edited_config_str)
            except Exception as e:
                is_valid_json = False
                st.error(f"❌ Invalid JSON format: {e}")
                
        with col_right:
            st.markdown("**Validation Outcome Details**")
            
            score = selected_run["confidence_score"]
            if score >= 90:
                color_class = "score-high"
                rec_text = "Auto-Approve"
            elif score >= 70:
                color_class = "score-medium"
                rec_text = "Human Review"
            else:
                color_class = "score-low"
                rec_text = "Reject"
                
            st.markdown(f'<div class="score-badge {color_class}">{rec_text} Recommendation (Confidence Score: {score}/100)</div>', unsafe_allow_html=True)
            
            breakdown = selected_run["score_breakdown"] or {}
            
            with st.expander("📊 Score Metrics", expanded=True):
                st.markdown(f"**Base Score:** {breakdown.get('base_score', 'N/A')}")
                st.markdown(f"**Schema Match:** {breakdown.get('schema_valid_pct', 'N/A')}%")
                
                bonuses = []
                if breakdown.get("schema_bonus"):
                    bonuses.append(f"🟢 Schema Bonus: +{breakdown['schema_bonus']}")
                if breakdown.get("title_bonus"):
                    bonuses.append(f"🟢 Title Bonus: +{breakdown['title_bonus']}")
                if breakdown.get("category_bonus"):
                    bonuses.append(f"🟢 Category Bonus: +{breakdown['category_bonus']}")
                if breakdown.get("discount_bonus"):
                    bonuses.append(f"🟢 Discount Bonus: +{breakdown['discount_bonus']}")
                if breakdown.get("antibot_penalty"):
                    bonuses.append(f"🔴 Anti-Bot Penalty: {breakdown['antibot_penalty']}")
                if breakdown.get("violation_override"):
                    bonuses.append("❌ Sandbox Violation Override: Score reset to 0")
                    
                if bonuses:
                    st.markdown("\n".join(f"- {b}" for b in bonuses))
                else:
                    st.markdown("*No score adjustments applied.*")
                    
                if breakdown.get("notes"):
                    st.info(f"**Validation Notes:** {breakdown['notes']}")
                    
            warnings = breakdown.get("schema_errors", []) + breakdown.get("issues", []) + [f"Sandbox Violation: {v}" for v in breakdown.get("sandbox_violations", [])]
            with st.expander(f"⚠️ Failures / Warnings ({len(warnings)})", expanded=len(warnings) > 0):
                if warnings:
                    for w in warnings:
                        st.markdown(f"- {w}")
                else:
                    st.success("No validation errors or warnings reported.")
                    
            samples = breakdown.get("sample_offers", [])
            with st.expander(f"🛍️ Sample Offers Extracted ({len(samples)})", expanded=True):
                if samples:
                    st.dataframe(pd.DataFrame(samples), width="stretch")
                else:
                    st.info("No sample offers captured.")
                    
        st.markdown("<hr>", unsafe_allow_html=True)
        act_col1, act_col2 = st.columns(2)
        
        with act_col1:
            approve_clicked = st.button("✅ Approve & Register", disabled=not is_valid_json, width="stretch")
            if approve_clicked:
                success, msg = approve_pending_target(
                    brand=brand,
                    config_json=parsed_config,
                    confidence_score=score,
                    score_breakdown=breakdown,
                    offers_extracted=len(samples),
                    user_id=sim_user,
                    outcome_id=selected_run["id"]
                )
                if success:
                    st.success(f"Registered brand '{brand}' successfully!")
                    st.rerun()
                else:
                    st.error(msg)

        with act_col2:
            reject_clicked = st.button("❌ Reject Target", width="stretch")
            if reject_clicked or st.session_state.get(f"show_reject_{brand}", False):
                st.session_state[f"show_reject_{brand}"] = True
                reject_comment = st.text_area("Rejection Reason", placeholder="Explain why this configuration was rejected...", key=f"cmt_{brand}")
                if st.button("Confirm Rejection", key=f"conf_rej_{brand}"):
                    success, msg = reject_pending_target(
                        brand=brand,
                        comment=reject_comment,
                        confidence_score=score,
                        user_id=sim_user,
                        outcome_id=selected_run["id"]
                    )
                    if success:
                        st.success(f"Rejected brand '{brand}' successfully.")
                        st.session_state[f"show_reject_{brand}"] = False
                        st.rerun()
                    else:
                        st.error(msg)

# ── Tab 2: Active Registry ───────────────────────────────────────────────────
with tab_active:
    st.markdown('<div class="section-label" style="margin-top:0.5rem;">Target Registry</div>', unsafe_allow_html=True)
    
    session = get_session()
    try:
        registry_items = session.query(PrefectTargetRegistry).order_by(PrefectTargetRegistry.brand).all()
    finally:
        session.close()
        
    if not registry_items:
        st.info("No scraper targets registered in the Prefect Target Registry yet.")
    else:
        for item in registry_items:
            c1, c2, c3, c4 = st.columns([2, 3, 2, 1], vertical_alignment="center")
            
            with c1:
                st.markdown(f"### **{item.brand}**")
                st.markdown(f"Status: **{'🟢 Enabled' if item.enabled else '🔴 Disabled'}**")
                
            with c2:
                st.markdown(f"Config: `{item.config_path}`")
                
            with c3:
                st.markdown(f"Registered By: **{item.registered_by}**")
                reg_date = item.registered_at.strftime("%Y-%m-%d %H:%M:%S") if item.registered_at else "N/A"
                st.markdown(f"Registered At: *{reg_date}*")
                
            with c4:
                toggle_txt = "Disable" if item.enabled else "Enable"
                if st.button(toggle_txt, key=f"tgl_{item.brand}"):
                    success, msg = toggle_target_status(item.brand, not item.enabled, sim_user)
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
                        
            st.markdown("<hr style='margin: 0.5rem 0 !important;'>", unsafe_allow_html=True)

# ── Tab 3: Audit Trail Log ───────────────────────────────────────────────────
with tab_audit:
    st.markdown('<div class="section-label" style="margin-top:0.5rem;">System Log</div>', unsafe_allow_html=True)

    PAGE_SIZE = 25
    audit_filter_col, audit_page_col = st.columns([3, 1])
    with audit_filter_col:
        audit_action_filter = st.multiselect(
            "Filter by action",
            options=["approve", "reject", "enable_target", "disable_target"],
            default=[],
            placeholder="All actions"
        )
    with audit_page_col:
        audit_page = st.number_input("Page", min_value=1, value=1, step=1)

    session = get_session()
    try:
        q = session.query(AgentAuditLog).order_by(AgentAuditLog.created_at.desc())
        if audit_action_filter:
            q = q.filter(AgentAuditLog.action.in_(audit_action_filter))
        total_logs = q.count()
        audit_trail = q.offset((audit_page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    finally:
        session.close()

    total_pages = max(1, (total_logs + PAGE_SIZE - 1) // PAGE_SIZE)
    st.caption(f"Showing page {audit_page} of {total_pages} ({total_logs} total events)")

    if not audit_trail:
        st.info("No audit logs found.")
    else:
        for log in audit_trail:
            timestamp = log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "N/A"
            action_badge = log.action.upper()
            exp_label = f"{timestamp} | Brand: {log.brand} | User: {log.user_id} | Action: {action_badge}"
            with st.expander(exp_label):
                st.write(f"**Action:** {log.action}")
                st.write(f"**User ID:** {log.user_id}")
                st.write(f"**Brand:** {log.brand}")
                st.write(f"**Timestamp:** {timestamp}")
                if log.details:
                    st.markdown("**Details:**")
                    st.json(log.details)

# ── Tab 4: Trigger Exploration Agent ─────────────────────────────────────────
with tab_add_new:
    st.markdown('<div class="section-label" style="margin-top:0.5rem;">Launch Agent Pipeline</div>', unsafe_allow_html=True)
    st.write("Trigger the autonomous scraper agent to explore a website, generate a configuration, run containerized validation, and calculate scoring.")

    with st.form("trigger_agent_form"):
        new_brand_name = st.text_input("Brand Name *", placeholder="e.g., Zara, Nike")
        new_target_url = st.text_input("Target URL *", placeholder="e.g., https://www.zara.com/au/en/sale-lp.html")
        new_requirements = st.text_area("Extraction Requirements / Notes (Optional)", placeholder="e.g., Focus on shoes and jackets promotions, ignore sitewide banners.")
        
        submit_btn = st.form_submit_button("🚀 Run Scraper Agent", width="stretch")

    if submit_btn:
        if not new_brand_name.strip() or not new_target_url.strip():
            st.error("Please provide both Brand Name and Target URL.")
        elif not (new_target_url.startswith("http://") or new_target_url.startswith("https://")):
            st.error("Please provide a valid URL starting with http:// or https://")
        else:
            from agent.orchestrator import build_agent_graph
            from agent.models import AgentState
            
            status_placeholder = st.empty()
            status_placeholder.info("Initializing Agent State Graph...")
            
            try:
                # Trigger the graph execution
                with st.spinner("Agent exploring target, building layout analysis, generating configuration, and running sandbox validations..."):
                    state = AgentState(
                        url=new_target_url.strip(),
                        brand=new_brand_name.strip(),
                        requirements=new_requirements.strip()
                    )
                    graph = build_agent_graph()
                    result = graph.invoke(state)
                
                status_placeholder.empty()
                status_type = result.get("status")
                report = result.get("validation_report")
                
                if status_type == "registered":
                    st.success(f"🎉 **Success!** Scraper registered and auto-approved for brand '{new_brand_name}'.")
                    if report:
                        st.write(f"- **Confidence Score:** {report.confidence_score}")
                        st.write(f"- **Recommendation:** {report.recommendation}")
                elif status_type == "validation" and report:
                    if report.recommendation == "pending":
                        st.warning(f"⚠️ **Pending Review:** Config generated with confidence score **{report.confidence_score}**. Review it in the **Pending Approvals Queue** tab.")
                    else:
                        st.error(f"❌ **Rejected:** Config score was too low (**{report.confidence_score}**) or sandbox violations occurred. Recommendation: {report.recommendation}")
                elif result.get("error"):
                    st.error(f"❌ **Error occurred:** {result.get('error')}")
                else:
                    st.info(f"Pipeline complete. Status: {status_type}")
                    
                st.button("🔄 Refresh Workspace")
                
            except Exception as e:
                status_placeholder.empty()
                st.error(f"Failed to execute agent: {e}")

