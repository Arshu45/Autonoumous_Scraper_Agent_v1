# HANDOFF — Task 8: Health Check Agent & Final System Verification

**Branch:** `task_8`
**Date:** 2026-07-19
**Depends on:** Task 7 (Streamlit Approval Workspace, `task_7` branch)

---

## Summary of What Was Built

Task 8 delivers the final piece of the autonomous scraping agent system: a **scraper health-monitoring agent** that runs against all enabled `prefect_target_registry` entries, evaluates recent run outcomes, flags unhealthy scrapers, and writes detection results back to `agent_run_outcomes` — without triggering any automated repair (deferred to fast-follow per `§19a` of `docs/implementation_plan.md`).

### Files Created / Modified

| File | Status | Purpose |
|---|---|---|
| `scripts/run_health_check.py` | **new** | Health check agent — detection + DB logging |
| `scripts/test_health_check.py` | **new** | 26-check integration test suite (mock + real DB states) |
| `README.md` | **modified** | Added health check section, updated test suite table, task status, docs table |

---

## Health Check Architecture

### Detection Logic (`scripts/run_health_check.py`)

```
run_health_check()
    │
    ├── 1. Load all enabled targets from `prefect_target_registry`
    │
    └── For each target:
        │
        ├── 2. Query last 3 `agent_run_outcomes` rows (initial_validation + health_check)
        │
        ├── 3. _analyse_target_health(brand, recent_outcomes)
        │       │
        │       ├── No rows yet?  → still_healthy=True,  note="no_outcome_rows_yet"
        │       │
        │       ├── All 3 yields == 0?
        │       │    → still_healthy=False, unhealthy_reason="zero_yield"
        │       │
        │       ├── avg(schema_valid_pct) < 50%?
        │       │    → still_healthy=False, unhealthy_reason="high_schema_failure_rate"
        │       │
        │       └── Otherwise → still_healthy=True
        │
        ├── 4. _log_health_check_outcome() → INSERT agent_run_outcomes
        │       run_type="health_check"
        │       still_healthy_at_check=True|False
        │       score_breakdown={unhealthy_reason, recent_yield_counts, schema_pcts, ...}
        │
        └── 5. TODO [fast-follow]: trigger_repair_agent(brand, url)
```

### Thresholds (configurable at module top)

| Constant | Value | Meaning |
|---|---|---|
| `RECENT_RUN_LOOKBACK` | `3` | Number of most-recent outcome rows inspected |
| `ZERO_YIELD_THRESHOLD` | `0` | Runs with `offers_extracted <= 0` count as zero-yield |
| `SCHEMA_FAILURE_PCT_THRESHOLD` | `50.0` | Avg `schema_valid_pct` below this is a high failure rate |

### DB columns written per health-check run

```sql
-- One new row per target per run_health_check() invocation:
INSERT INTO agent_run_outcomes (
    brand,
    run_type,                   -- 'health_check'
    confidence_score,           -- NULL (not re-scored)
    recommendation,             -- NULL (health check has no routing recommendation)
    offers_extracted,           -- NULL (not re-scraped)
    was_auto_approved,          -- NULL
    days_since_registration,    -- computed from prefect_target_registry.registered_at
    still_healthy_at_check,     -- True | False  ← primary signal
    score_breakdown             -- JSONB with unhealthy_reason, yield counts, schema pcts
)
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All checked targets healthy (or no targets registered) |
| `1` | One or more targets flagged unhealthy |
| `2` | Fatal error (DB unreachable, import failure) |

### Automated Repair (deferred — TODO in code)

Detection-only is the scope of this task. The code contains a clearly marked `TODO [fast-follow]` block at the exact integration point where `trigger_repair_agent()` will be called once `agent/repair_agent.py` is implemented:

```python
# TODO [fast-follow — Repair Agent]: Once `agent/repair_agent.py` is
# implemented, trigger automated re-exploration and selector patching here:
#
#     from agent.repair_agent import trigger_repair_agent
#     trigger_repair_agent(brand=brand, url=target_url)
#
# Reference: docs/implementation_plan.md §19a, Appendix A.
```

---

## Test Results — Task 8

```
scripts/test_health_check.py → 26/26 checks ✓  exit 0
```

### Test scenarios

| Test | What it verifies |
|---|---|
| `test_healthy_target` | 3 positive-yield runs → `still_healthy=True`, no reason, 3 yield counts returned |
| `test_zero_yield_collapse` | 3× zero-yield runs → `still_healthy=False`, `unhealthy_reason='zero_yield'` |
| `test_high_schema_failure_rate` | avg schema_valid_pct=20% → `still_healthy=False`, `unhealthy_reason='high_schema_failure_rate'` |
| `test_no_outcome_rows` | Empty outcome list → treated healthy, note=`'no_outcome_rows_yet'` |
| `test_full_run_health_check` | End-to-end: seeds 2 registry targets + outcomes, calls `run_health_check()`, verifies summary dict and DB rows written with correct `still_healthy_at_check` values and `unhealthy_reason` in JSONB |

---

## Final Project Checklist — Tasks 1–8

### ✅ Task 1 — Agent Scaffold & Models

- [x] `agent/` package structure with `__init__.py`
- [x] `AgentState`, `SiteAnalysis`, `GeneratedArtifacts`, `ValidationReport` Pydantic models
- [x] LangGraph orchestrator skeleton with conditional routing

### ✅ Task 2 — Exploration Agent

- [x] Playwright headless Chromium with stealth flags and custom headers
- [x] Full-page screenshot capture (pre- and post-scroll)
- [x] Anti-bot signal detection (Cloudflare, CAPTCHA, DataDome, perimeterX, Akamai)
- [x] Anti-bot risk scoring: `low` / `medium` / `high`
- [x] Gemini Vision multimodal analysis of promotional areas
- [x] DOM cleaning (removes `<script>`, `<style>`, `<head>`, `<svg>`)
- [x] LLM DOM analysis for CSS selector recommendation via LiteLLM / Gemini fallback

### ✅ Task 3 — Generation Agent

- [x] LLM config generator via `CONFIG_GENERATION_PROMPT`
- [x] Saves `config/targets/<brand_slug>.json` in `HybridPromoExtractor`-compatible format
- [x] Self-correction loop on invalid JSON (max 1 retry)
- [x] `state.status = "generated"` on success

### ✅ Task 4 — Docker Sandbox Infrastructure

- [x] `docker/Dockerfile.sandbox` — minimal Python 3.12 image with Playwright + scraper deps
- [x] `docker/setup_egress_network.sh` — creates egress-filtered Docker network
- [x] `agent/sandbox_runner.py` — host-side container lifecycle (start, wait, detect violations, remove)
- [x] `agent/sandbox_entrypoint.py` — runs inside container; reads config/code from env vars
- [x] Resource caps: 512 MB RAM, 1 CPU, 64 pids, read-only filesystem, `/tmp` capped at 64 MB

### ✅ Task 5 — Validation Agent

- [x] Sandbox execution via `sandbox_runner.run_scraper_in_sandbox()`
- [x] Sandbox violation fast-path: any violation → `confidence_score=0`, `recommendation="reject"`
- [x] Offer schema validation (title + confidence required; category/discount optional)
- [x] Confidence scoring (0–100+) with itemised `score_breakdown`
- [x] Routing: `auto_approve` (≥90) / `pending` (70–89) / `reject` (<70 or violation)
- [x] `sample_offers` (up to 3) for human preview
- [x] Writes every validation run to `agent_run_outcomes` with JSONB breakdown

### ✅ Task 6 — Auth Hooks & Atomic DB Registration

- [x] `auth/approval_rbac.py` — `AgentRole`, `get_current_user()`, `require_role()`
- [x] `agent/registration_agent.py` — atomically UPSERTs `competitors`, inserts `prefect_target_registry`, inserts `agent_audit_log` in a single `session.begin()` block
- [x] `scripts/run_hybrid_promo_scraper.py::load_targets()` — DB-first loading with filesystem fallback
- [x] Four Alembic migrations (`t6a` → `t6b` → `t6c` → `t6d`) create all agent tables

### ✅ Task 7 — Streamlit Operator Workspace

- [x] **Tab 1 — Pending Approvals Queue**: CTE-based deduplication, side-by-side config editor + validation outcome, Approve/Reject callbacks, `_mark_outcome_resolved()`
- [x] **Tab 2 — Active Registry**: enable/disable toggle, atomic `prefect_target_registry` + `competitors` update + audit event
- [x] **Tab 3 — Audit Trail Log**: paginated (25/page), action-type filter, expandable JSONB panels
- [x] **Tab 4 — Trigger Exploration Agent**: brand/URL/requirements input, live agent invocation, outcome banners
- [x] Sidebar: DB health ping, operator identity, live counts, Developer Tools seed button
- [x] 19/19 integration tests pass

### ✅ Task 8 — Health Check Agent (this task)

- [x] `scripts/run_health_check.py` queries all `enabled=True` targets from `prefect_target_registry`
- [x] Checks last 3 outcome rows per target (both `initial_validation` and `health_check` types)
- [x] Detects zero-yield collapse (all 3 runs returned 0 offers)
- [x] Detects high schema failure rate (avg `schema_valid_pct < 50%`)
- [x] Treats missing outcome rows as a data gap (healthy, not an alert)
- [x] Logs every check to `agent_run_outcomes` with `run_type="health_check"` and `still_healthy_at_check`
- [x] `score_breakdown` JSONB persists `unhealthy_reason`, recent yield counts, schema pcts, days_since_registration
- [x] Descriptive `TODO` comment for future repair-agent integration
- [x] Standalone CLI with meaningful exit codes (0/1/2)
- [x] 26/26 integration tests pass

---

## Full Test Suite — Final State

```
agent/test_orchestrator.py      →  5/5  tests  ✓  exit 0
agent/test_validation.py        → 25/25 checks ✓  exit 0
agent/test_registration.py      → 20/20 checks ✓  exit 0
dashboard/test_approval_ui.py   → 19/19 checks ✓  exit 0
agent/test_sandbox.py           →  3/3  tests  ✓  exit 0  (requires Docker)
scripts/test_health_check.py    → 26/26 checks ✓  exit 0
───────────────────────────────────────────────────────────
TOTAL                           → 98/98 checks ✓
```

> **Note:** Tasks 1–7 previously totalled 72 checks (5+25+20+19+3). Task 8 adds 26 checks, bringing the cumulative total to **98 checks** across 6 test files.

---

## Remaining Fast-Follow Items (Operational / Post-Production)

These are explicitly deferred per `docs/implementation_plan.md §19a` and `Appendix A`. They are designed but not yet implemented:

| Item | Trigger Condition | Where to Start |
|---|---|---|
| **Repair Agent** (`agent/repair_agent.py`) | First instance of selector staleness detected by health check | `TODO` comment in `run_health_check.py` at the repair hook; `docs/implementation_plan.md §19a` |
| `REPAIR_DIFF_PROMPT` | Built alongside repair agent | `agent/prompts.py` — add after repair agent is scaffolded |
| **Calibration script** | 30+ days of `agent_run_outcomes` data accumulated | `docs/implementation_plan.md §9c` |
| **RBAC role split** (OPERATOR / APPROVER) | Team grows beyond 1 operator | `auth/approval_rbac.py` — hooks already in place via `AgentRole` enum |
| **`--repair` CLI flag** | Repair agent is implemented | `scripts/run_scraper_agent.py` |
| **LDAP / SSO integration** | Production deployment with multiple users | `auth/approval_rbac.py::get_current_user()` — currently reads `AGENT_USER_ID` env var; replace with real identity provider call |
| **Health check cron/Prefect scheduling** | Moved to production monitoring | Wire `run_health_check()` into `flows/master_pipeline.py` or create a daily Prefect flow |
| **Streamlit health monitoring tab** | Once repair agent ships | Add Tab 5 to `dashboard/approval_ui.py` surfacing `still_healthy_at_check=False` rows |

---

## Alembic State

No new migrations in Task 8. All tables required for health checking (`agent_run_outcomes`, `prefect_target_registry`) were created in Task 6 migrations.

```bash
# Confirm migration head is unchanged:
../env/bin/alembic current
# Expected: t6d004000004 (head)
```

---

## Quick-Start Commands

```bash
cd /home/arsha/Desktop/Autonomous_scraping_agent/myers_competitive_analysis

# 1. Run all tests (no Docker required for health check tests)
../env/bin/python scripts/test_health_check.py

# 2. Run health check against live DB
../env/bin/python scripts/run_health_check.py
# Exit 0 = healthy, 1 = unhealthy targets found, 2 = fatal error

# 3. Run full pipeline (requires LLM + Playwright)
../env/bin/python scripts/run_scraper_agent.py \
    --url "https://www.example.com/sale" \
    --brand "Example Brand"

# 4. Launch operator workspace
export AGENT_USER_ID=your.name@company.com
../env/bin/streamlit run dashboard/approval_ui.py
```
