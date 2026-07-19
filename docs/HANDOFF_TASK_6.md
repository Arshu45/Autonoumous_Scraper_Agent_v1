# HANDOFF — Task 6: Auth Hooks + Data-Driven Registration

**Branch:** `task_6`  
**Date:** 2026-07-19  
**Depends on:** Task 5 (Validation Agent, `task_5` branch)

---

## Summary of What Was Built

Task 6 wires the `registration` node in the LangGraph pipeline with a real,
database-backed implementation. It adds four new DB tables, an RBAC auth
module, a DB-first target loader, and a full test suite.

### Files Created / Modified

| File | Status | Purpose |
|---|---|---|
| `auth/__init__.py` | **new** | Auth package init |
| `auth/approval_rbac.py` | **new** | Role enum + `get_current_user()` |
| `agent/registration_agent.py` | **new** | Full registration node implementation |
| `agent/orchestrator.py` | **modified** (lines 37–39 only) | Replaced stub with real import |
| `database/models.py` | **modified** | 4 new SQLAlchemy models + 5 new columns on `Competitor` |
| `scripts/run_hybrid_promo_scraper.py` | **modified** | DB-first `load_targets()` + `_load_filesystem_targets()` helper |
| `agent/test_registration.py` | **new** | 5 test scenarios, 20 checks, exit 0 |
| `agent/test_orchestrator.py` | **modified** | Added `@patch` for `run_registration` in `test_auto_approve` |
| `alembic/versions/t6a001000001_*.py` | **new** | Migration A — `agent_run_outcomes` |
| `alembic/versions/t6b002000002_*.py` | **new** | Migration B — `agent_audit_log` |
| `alembic/versions/t6c003000003_*.py` | **new** | Migration C — new columns on `competitors` |
| `alembic/versions/t6d004000004_*.py` | **new** | Migration D — `prefect_target_registry` |

---

## Migration Files and How to Apply

### Migration chain (linear from existing head `56dc637c376b`)

```
56dc637c376b  ← previous head (add_is_on_sale_and_sku_to_product_snapshots)
    └── t6a001000001  agent_run_outcomes table
        └── t6b002000002  agent_audit_log table
            └── t6c003000003  new columns on competitors
                └── t6d004000004  prefect_target_registry table
```

### Apply

```bash
cd /home/arsha/Desktop/Autonomous_scraping_agent/myers_competitive_analysis

# If DB has no alembic version record yet (fresh DB):
../env/bin/alembic stamp 56dc637c376b

# Apply all Task 6 migrations:
../env/bin/alembic upgrade t6d004000004

# Verify:
../env/bin/alembic current
```

> **Note:** Each migration uses `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`
> so they are safe to re-run.

### SQL equivalent (for reference)

```sql
-- Migration A
CREATE TABLE IF NOT EXISTS agent_run_outcomes (
    id SERIAL PRIMARY KEY, brand VARCHAR(255), run_type VARCHAR(20),
    confidence_score INTEGER, score_breakdown JSONB, recommendation VARCHAR(20),
    offers_extracted INTEGER, was_auto_approved BOOLEAN,
    days_since_registration INTEGER, still_healthy_at_check BOOLEAN,
    checked_at TIMESTAMP DEFAULT NOW()
);

-- Migration B
CREATE TABLE IF NOT EXISTS agent_audit_log (
    id SERIAL PRIMARY KEY, brand VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL, action VARCHAR(50) NOT NULL,
    details JSONB, created_at TIMESTAMP DEFAULT NOW()
);

-- Migration C
ALTER TABLE competitors
    ADD COLUMN IF NOT EXISTS extraction_strategy VARCHAR(20) DEFAULT 'hybrid',
    ADD COLUMN IF NOT EXISTS agent_generated     BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS agent_confidence    INTEGER,
    ADD COLUMN IF NOT EXISTS agent_notes         TEXT,
    ADD COLUMN IF NOT EXISTS source_url          TEXT;

-- Migration D
CREATE TABLE IF NOT EXISTS prefect_target_registry (
    id SERIAL PRIMARY KEY, brand VARCHAR(255) UNIQUE,
    config_path VARCHAR(500), enabled BOOLEAN DEFAULT TRUE,
    registered_at TIMESTAMP DEFAULT NOW(), registered_by VARCHAR(255)
);
```

---

## Verifying DB Rows After a Registration Run

After running the full pipeline (or `agent/test_registration.py`), check:

```sql
-- Was the competitor upserted with agent flags?
SELECT name, agent_generated, agent_confidence, extraction_strategy
FROM competitors WHERE agent_generated = TRUE;

-- Was the brand registered for Prefect?
SELECT brand, config_path, enabled, registered_by, registered_at
FROM prefect_target_registry WHERE enabled = TRUE;

-- Is there an approval audit record?
SELECT brand, user_id, action, details, created_at
FROM agent_audit_log ORDER BY created_at DESC LIMIT 10;

-- What were the validation outcomes?
SELECT brand, run_type, confidence_score, recommendation, was_auto_approved, checked_at
FROM agent_run_outcomes ORDER BY checked_at DESC LIMIT 10;
```

---

## How `load_targets()` DB-First Path Works

`scripts/run_hybrid_promo_scraper.py::load_targets()` now follows this priority:

```
1. If --target flag given → load that single file (unchanged)
2. Query prefect_target_registry WHERE enabled=TRUE
   ├── If rows found → load each config_path file
   │     └── Also append any filesystem configs NOT already in the registry
   │         (so manually added JSON files still get picked up)
   └── If empty or DB unavailable → fall back to _load_filesystem_targets()
3. _load_filesystem_targets() → glob config/targets/*.json (original behaviour)
```

**Filesystem fallback is always intact** — if the DB is down or the registry
is empty, `load_targets()` silently falls back to the glob scan. No changes
to callers required.

---

## Why `flows/master_pipeline.py` Needs No Changes

`flows/master_pipeline.py` calls `load_targets()` with no arguments.
The new implementation is a **drop-in replacement** — same signature, same
return type (`list[dict]`), same semantic (returns enabled targets). The DB
path is additive; the filesystem fallback preserves the original behaviour
exactly.

---

## What Task 7 (Streamlit Approval UI) Will Consume

### From `agent_audit_log`

| Column | Used for |
|---|---|
| `brand` | Filter/group by brand |
| `user_id` | Show who approved/rejected |
| `action` | Filter by `approve` / `reject` / `edit_config` |
| `details` | Display `confidence_score`, `recommendation`, config diff |
| `created_at` | Timeline view, most-recent-first ordering |

### From `ValidationReport` (via `agent_run_outcomes`)

| Column | Used for |
|---|---|
| `confidence_score` | Display score badge (red/amber/green) |
| `score_breakdown` | Expandable breakdown chart |
| `recommendation` | Default action pre-selected in approval UI |
| `offers_extracted` | Show extraction health |
| `was_auto_approved` | Badge: "Auto-approved" vs "Pending review" |
| `still_healthy_at_check` | Health check indicator (populated by Task 8) |

### `prefect_target_registry`

Task 7 will display the registered brand list and provide a toggle to
`enabled=FALSE` (soft-disable without deleting) and show `registered_by`.

---

## Test Results

```
agent/test_orchestrator.py  →  5/5 tests  ✓  exit 0
agent/test_validation.py    →  25/25 checks ✓  exit 0
agent/test_registration.py  →  20/20 checks ✓  exit 0
```

### Key design decisions

- **Atomicity:** `competitors` UPSERT + `prefect_target_registry` INSERT +
  `agent_audit_log` INSERT are wrapped in a single `session.begin()` block.
  Any failure rolls back all three. `agent_run_outcomes` is written in a
  separate session (diagnostic record — non-critical).

- **`test_orchestrator.py` patch:** `test_auto_approve` now mocks
  `agent.registration_agent.run_registration` with a lightweight stub.
  This is correct — the orchestrator test covers *routing logic*, not DB
  persistence. DB persistence is covered by `test_registration.py`.

- **RBAC placeholder:** `auth/approval_rbac.py::get_current_user()` reads
  `AGENT_USER_ID` from the environment. Set it via:
  ```bash
  export AGENT_USER_ID=your.name@company.com
  ```
  All audit log rows will carry this identity.
