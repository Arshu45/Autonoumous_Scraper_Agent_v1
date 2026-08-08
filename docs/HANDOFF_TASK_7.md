# HANDOFF — Task 7: Streamlit Human-in-the-Loop Approval UI

**Branch:** `task_7`  
**Date:** 2026-07-19  
**Depends on:** Task 6 (Auth Hooks + Data-Driven Registration, `task_6` branch)

---

## Summary of What Was Built

Task 7 adds a full **operator workspace** — a Streamlit dashboard that gives the scraping team a human-in-the-loop interface for reviewing, approving, and rejecting agent-generated scraper configurations before they are registered for Prefect execution.

### Files Created / Modified

| File | Status | Purpose |
|---|---|---|
| `dashboard/approval_ui.py` | **new** | Streamlit operator workspace (3 tabs) |
| `dashboard/test_approval_ui.py` | **new** | 19 integration checks for all DB callbacks |
| `agent/validation_agent.py` | **modified** (lines 372–411) | Now persists every validation run to `agent_run_outcomes` so the queue is populated automatically |

---

## How to Run the Dashboard

```bash
cd /home/arsha/Desktop/Autonomous_scraping_agent/myers_competitive_analysis

# Set your operator identity (written to audit log)
export AGENT_USER_ID=your.name@company.com

# Launch
../env/bin/streamlit run dashboard/approval_ui.py
```

The app opens at `http://localhost:8501` by default.

---

## Architecture: Four-Tab Layout

### Tab 1 — Pending Approvals Queue

**What populates it:** Every call to `run_validation_agent()` now writes a row to `agent_run_outcomes`. Rows where `recommendation = 'pending'` OR `confidence_score BETWEEN 70 AND 89` appear here *unless* they have already been acted on (approve/reject audit event timestamp > outcome timestamp).

**CTE query logic:**
```sql
WITH latest_outcomes AS (
    SELECT DISTINCT ON (brand) ...
    FROM agent_run_outcomes WHERE run_type = 'initial_validation'
    ORDER BY brand, checked_at DESC        -- one row per brand, most recent
),
latest_audit AS (
    SELECT DISTINCT ON (brand) ...
    FROM agent_audit_log WHERE action IN ('approve','reject')
    ORDER BY brand, created_at DESC
)
SELECT ... FROM latest_outcomes o
LEFT JOIN latest_audit a ON o.brand = a.brand
WHERE o.recommendation = 'pending' OR (score BETWEEN 70 AND 89)
  AND (a.created_at IS NULL OR a.created_at < o.checked_at)
```

**Approve flow:**
1. Operator edits the JSON config in the text area (live validation guards against invalid JSON).
2. `approve_pending_target()` writes the config file to `config/targets/<slug>.json`.
3. Calls `run_registration(state)` which atomically writes `competitors`, `prefect_target_registry`, and `agent_audit_log` (action=`'approve'`).
4. After success, `_mark_outcome_resolved()` updates the original pending `agent_run_outcomes` row to `recommendation='approved'` so it disappears from the queue.

**Reject flow:**
1. Operator fills in a rejection reason text area.
2. `reject_pending_target()` writes `agent_audit_log` (action=`'reject'`) and calls `_mark_outcome_resolved()` to flip the row to `recommendation='reject'`.

### Tab 2 — Active Registry

- Queries all `prefect_target_registry` rows ordered by brand name.
- Shows: brand, config path, registered_by, registered_at, enabled status.
- **Toggle button** — calls `toggle_target_status()` which atomically updates `prefect_target_registry.enabled` + `competitors.enabled` + writes `enable_target`/`disable_target` audit event.

### Tab 3 — Audit Trail Log

- Paginated (25 rows/page) via `LIMIT`/`OFFSET` to avoid loading all rows into memory.
- Filter by action type (multiselect).
- Expandable JSON panels show `details` JSONB (confidence score, rejection reasons, config diffs).

### Tab 4 — Trigger Exploration Agent

- Operates as a completely self-contained UI to add new targets.
- Takes **Brand Name**, **Target URL**, and optional **Requirements** as input.
- Dynamically executes `build_agent_graph().invoke()` within a spinner.
- Displays live outcome banners: auto-approved, pending review, or rejected.

---

## Sidebar Features

| Element | Behaviour |
|---|---|
| DB status dot | Green/red indicator — pings `SELECT 1` on each render |
| Operator User ID input | Pre-filled from `AGENT_USER_ID` env var via `get_current_user()`; editable for simulation |
| Operational Summary | Live counts of pending reviews, active targets, audit events |
| Developer Tools | Seeds a mock pending outcome + config file into the DB for immediate end-to-end testing |

---

## Key Design Decisions

### `_mark_outcome_resolved()` — why it exists

The original `agent_run_outcomes` row written by the validation agent has `recommendation='pending'`. After an operator approves or rejects the target, `run_registration()` inserts a *second* row with `was_auto_approved=True`, but the first row was never updated. This left the queue CTE in a correct-but-inconsistent state (the audit timestamp guard filtered it out, but the pending row persisted in the table forever).

`_mark_outcome_resolved()` explicitly updates the original row by `id` (with a fallback to brand+recommendation lookup) so the table accurately reflects history.

### `generate_notes="operator_approved"`

The `GeneratedArtifacts.generation_notes` field is set to `"operator_approved"` when calling `run_registration()` from the dashboard. This makes it easy to distinguish operator-approved registrations from auto-approved ones in `agent_run_outcomes`.

### Session management

All DB helper functions (`approve_pending_target`, `reject_pending_target`, `toggle_target_status`) open and close their own sessions. The dashboard UI sections also open/close sessions per render. Sessions are **never** left open across Streamlit reruns.

---

## Test Results

```
dashboard/test_approval_ui.py → 19/19 checks passed ✓  exit 0

agent/test_orchestrator.py  →  5/5  tests  ✓  exit 0
agent/test_validation.py    →  25/25 checks ✓  exit 0
agent/test_registration.py  →  20/20 checks ✓  exit 0
```

### Test scenarios in `test_approval_ui.py`

| Test | Checks |
|---|---|
| `test_approve_callback` | Competitor written; registry written; audit log written; config file on disk; **original pending outcome row resolved** |
| `test_reject_callback` | Audit reject row written; rejection comment persisted; outcome row flipped to `'reject'` |
| `test_toggle_status_callback` | Disable: registry+competitor disabled, audit event written; Enable: both re-enabled, audit event written |

---

## What Task 8 (Health Check Agent) Will Consume

### From `agent_run_outcomes`

| Column | Used for |
|---|---|
| `brand` | Identify which target to re-validate |
| `checked_at` | `days_since_registration` calculation |
| `was_auto_approved` | Filter: only health-check auto-approved targets |
| `still_healthy_at_check` | `NULL` until Task 8 fills it in |
| `recommendation` | Skip brands already rejected |

### From `prefect_target_registry`

| Column | Used for |
|---|---|
| `enabled` | Skip disabled targets |
| `config_path` | Load config for re-validation |
| `registered_at` | Calculate days since registration |

---

## Alembic

No new migrations in Task 7. The `agent_run_outcomes`, `agent_audit_log`, `competitors` (agent columns), and `prefect_target_registry` tables from Task 6 (`t6d004000004`) are all that's needed.

```bash
# Confirm you're at the right head:
../env/bin/alembic current
# Expected output: t6d004000004 (head)
```
