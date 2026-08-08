# Autonomous Scraping Agent

An autonomous agent system designed to dynamically explore competitor websites, analyze layouts, evaluate anti-bot protections, identify visual promotional areas, determine optimal CSS extraction strategies, validate scrapers in a Docker sandbox, register approved configurations, and surface pending decisions to a human operator for review.

The system is built on **LangGraph** to orchestrate step-by-step agent executions, using **Playwright** for browser automation, **LiteLLM** for Claude model access, direct **Gemini** API fallbacks, and a **Streamlit** operator workspace for human-in-the-loop approvals.

---

## Agentic Architecture

The autonomous pipeline is represented as a state graph coordinated by a LangGraph orchestrator:

```text
       [START]
          │
          ▼
   ┌─────────────┐
   │ Exploration │  ◄── visits site, scores anti-bot, analyzes layout & DOM
   └─────────────┘
          │
          ▼
   ┌─────────────┐
   │ Generation  │  ◄── generates HybridPromoExtractor config, saves to config/targets/
   └─────────────┘
          │
          ▼
   ┌─────────────┐
   │ Validation  │  ◄── runs scraper in Docker sandbox, validates schema, scores confidence
   └─────────────┘
          │
      Conditional Routing (confidence score & sandbox violations)
      ┌───┼─────────────┐
      │   │             │
      │   │             ▼
      │   │          [END]   ← score < 70 or any sandbox violation → Rejected
      │   │
      │   ▼
      │  [END]   ← score 70–89 → Pending Human Review (Streamlit UI)
      │
      ▼
   ┌──────────────┐
   │ Registration │ ◄── registers verified config (score ≥ 90 → Auto-Approved)
   └──────────────┘
          │
          ▼
        [END]
```

### Agent Nodes & Responsibilities

1. **Exploration Agent** (`agent/exploration_agent.py`) — **fully implemented**:
   - Opens target site headlessly with stealth flags, custom headers, and webdriver detection blocks.
   - Evaluates page height, triggers dynamic scroll actions to bypass lazy loading, and resets scroll.
   - Computes an **anti-bot risk score** based on DOM presence of security elements (CAPTCHA, Cloudflare, etc.) and response headers.
   - Cleans non-semantic HTML tags (`<script>`, `<style>`, `<head>`, `<svg>`) to generate a truncated DOM.
   - Uses multimodal models (LiteLLM Claude falling back to direct Gemini) to identify visual promotional areas from pre/post-scroll screenshots.
   - Evaluates cleaned DOM alongside visual analysis to recommend extraction strategy and CSS selectors.

2. **Generation Agent** (`agent/generation_agent.py`) — **fully implemented**:
   - Generates target configs matching the exploration recommendations.
   - Saves config to `config/targets/<brand>.json` in `HybridPromoExtractor`-compatible format.
   - Sets `state.status = "generated"` on success.

3. **Validation Agent** (`agent/validation_agent.py`) — **fully implemented**:
   - Runs the generated scraper inside the Docker sandbox via `sandbox_runner.run_scraper_in_sandbox()`.
   - If sandbox violations are detected, immediately forces `confidence_score=0`, `recommendation="reject"`.
   - Parses offer data from container stdout, validates each offer against the schema.
   - Computes a **confidence score** (0–100+) using yield rate, schema quality, field-population rates, and anti-bot risk penalty.
   - Selects up to 3 sample offers for human preview.
   - **Writes every validation run to `agent_run_outcomes`** (including `sample_offers`, `schema_errors`, `issues`, and `sandbox_violations` inside the JSONB `score_breakdown`) so the operator workspace can display the full picture.
   - Populates `state.validation_report` (`ValidationReport`) and sets `state.status = "validation"`.

4. **Registration Agent** (`agent/registration_agent.py`) — **fully implemented**:
   - Atomically UPSERTs the `competitors` row, inserts `prefect_target_registry`, and inserts `agent_audit_log` in a single `session.begin()` block (all three roll back together on failure).
   - Writes a separate, non-critical `agent_run_outcomes` row with `was_auto_approved=True`.
   - Reads the active operator identity via `auth.approval_rbac.get_current_user()` (reads `AGENT_USER_ID` env var, falls back to `"default_operator"`).

---

## Operator Workspace (Streamlit)

`dashboard/approval_ui.py` provides a premium-styled operator dashboard with four tabs:

### Tab 1 — Pending Approvals Queue
- Surfaces all `agent_run_outcomes` rows with `recommendation = 'pending'` that have not yet been acted on (CTE-based deduplication by brand + audit log timestamp).
- Side-by-side layout: editable JSON config on the left, full validation outcome details on the right (score badge, score breakdown, warnings/schema errors, sample offers dataframe).
- **Approve**: calls `run_registration()` atomically, then marks the pending outcome row as resolved.
- **Reject**: writes a `reject` audit event with operator comment, marks the outcome row as resolved.

### Tab 2 — Active Registry
- Lists all `prefect_target_registry` entries with config path, registering user, and timestamp.
- Enable/Disable toggle — atomically updates both `prefect_target_registry.enabled` and `competitors.enabled`, writes an audit event.

### Tab 3 — Audit Trail Log
- Paginated (25 rows/page) with action-type filter.
- Expandable JSON panels show the full `details` JSONB (confidence score, rejection reasons, config diffs).

### Tab 4 — Trigger Exploration Agent
- Allows operators to add and test target websites completely from the browser UI (no CLI needed).
- Includes inputs for **Brand Name**, **Target URL**, and optional **Extraction Requirements**.
- Runs the autonomous agent state graph (explore, generate config, run containerized sandbox validation) with live UI status notifications.

### Running the workspace
```bash
export AGENT_USER_ID=your.name@company.com
../env/bin/streamlit run dashboard/approval_ui.py
```

---

## Setup & Initialization

### 1. Requirements
- Python 3.10+
- PostgreSQL database (see `.env` for `DATABASE_URL`)
- Google Gemini API Key
- LiteLLM Gateway / Corporate API Access (optional; falls back to direct Gemini API)
- **Docker Desktop** (for sandbox execution — see [Docker Setup](#docker-setup--sandbox-infrastructure) below)

### 2. Installation
Clone the repository, initialize your virtual environment, and install dependencies:

```bash
python -m venv env
source env/bin/activate

pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 3. Environment Configuration
Create a `.env` file in the project root:

```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/autonomous_agentic_scraping

# LiteLLM Configuration (Primary)
LITELLM_API_KEY=your_litellm_api_key
LITELLM_API_BASE=https://your-litellm-gateway.example/v1
LLM_MODEL=openai/claude-haiku-4.5
VISION_LLM_MODEL=openai/claude-haiku-4.5

# Direct Gemini Fallback Configuration
GEMINI_API_KEY=your_gemini_api_key

# Promotion categories for HybridPromoExtractor
PROMO_CATEGORIES="Home, Entertainment, Womens, Beauty, Kids, Toys, Menswear, Footwear, Null"

# Operator identity (written to all audit log rows)
AGENT_USER_ID=your.name@company.com
```

### 4. Database Setup

Apply all Alembic migrations to provision the full schema:

```bash
# Confirm current migration head (should be t6d004000004):
../env/bin/alembic current

# Apply all migrations (idempotent — safe to re-run):
../env/bin/alembic upgrade head
```

#### Tables created by migrations

| Table | Migration | Purpose |
|---|---|---|
| `competitors` | base + Task 6C | Brand registry with agent-generated metadata columns |
| `promotions` | base | Scraped offer rows |
| `agent_run_outcomes` | Task 6A (`t6a001000001`) | Validation run history, confidence scores, JSONB breakdown |
| `agent_audit_log` | Task 6B (`t6b002000002`) | Immutable operator action log |
| `prefect_target_registry` | Task 6D (`t6d004000004`) | Active targets registered for Prefect execution |

---

## Docker Setup & Sandbox Infrastructure

The **Docker sandbox** runs every generated scraper in an isolated, resource-capped container before validation. This prevents malicious or runaway AI-generated code from affecting the host machine.

### What the sandbox enforces

| Constraint | Value |
|---|---|
| Max RAM | 512 MB (OOM kill if exceeded) |
| Max CPU | 1 core |
| Max processes | 64 (prevents fork bombs) |
| Filesystem | Read-only (only `/tmp` is writable, max 64 MB) |
| Network | Egress-filtered Docker bridge network |
| Capabilities | All Linux capabilities dropped |

### Prerequisites
- **Docker Desktop** installed ([download](https://www.docker.com/products/docker-desktop/))
- Docker version ≥ 20.10

### Step 1 — Start Docker Desktop

```bash
systemctl --user start docker-desktop
```

> Run once per login session. To check it's running: `docker info`

### Step 2 — Build the sandbox image

Run from the **repository root**:

```bash
docker build -f docker/Dockerfile.sandbox -t promo-scraper-sandbox:latest .
```

> The build context must be the repo root — `COPY promo_scraper/` and `COPY agent/sandbox_entrypoint.py` resolve relative to it.

### Step 3 — Create the egress network

```bash
# Without iptables (Docker Desktop — internal bridge only):
docker network create \
  --driver bridge \
  --subnet 172.28.0.0/16 \
  --internal \
  scraper-egress-only

# With iptables (Linux Docker Engine — requires sudo):
sudo ./docker/setup_egress_network.sh create <target-domain>
# Example:
sudo ./docker/setup_egress_network.sh create www.vanheusen.com.au
```

To tear down:
```bash
docker network rm scraper-egress-only
# or: sudo ./docker/setup_egress_network.sh teardown
```

> **Note:** The network persists until explicitly removed or Docker Desktop restarts.

---

## Running the Tests

All test scripts exit 0 on success and print a per-check result table.

```bash
# Validation agent (mocked sandbox, no Docker required) — 25 checks
../env/bin/python agent/test_validation.py

# Registration agent (real DB required) — 20 checks
../env/bin/python agent/test_registration.py

# Orchestrator routing + generation agent unit tests (no Docker, no DB) — 5 tests
../env/bin/python agent/test_orchestrator.py

# Approval UI database callbacks (real DB required) — 19 checks
../env/bin/python dashboard/test_approval_ui.py

# Sandbox violation detection (requires Docker running) — 3 tests
../env/bin/python agent/test_sandbox.py

# Health check agent (real DB required) — 26 checks
../env/bin/python scripts/test_health_check.py
```

### Full test suite

```
agent/test_orchestrator.py      →  5/5  tests  ✓  exit 0
agent/test_validation.py        → 25/25 checks ✓  exit 0
agent/test_registration.py      → 20/20 checks ✓  exit 0
dashboard/test_approval_ui.py   → 19/19 checks ✓  exit 0
agent/test_sandbox.py           →  3/3  tests  ✓  exit 0  (requires Docker)
scripts/test_health_check.py    → 26/26 checks ✓  exit 0
```

---

## Confidence Scoring (Validation Agent)

The validation agent computes a confidence score (0–100+) that determines the routing outcome:

| Score band | Routing | Meaning |
|---|---|---|
| ≥ 90 | `auto_approve` → Registration | Scraper meets quality bar; config activated automatically |
| 70–89 | `pending` → Streamlit UI | Human review required before activation |
| < 70 | `reject` → END | Config discarded; re-exploration recommended |
| Any violation | `reject` (score forced to 0) | Sandbox security breach; instant discard |

### Scoring breakdown

| Component | Condition | Δ Score |
|---|---|---|
| Base (yield ≥ 5 offers or yield_rate ≥ 80%) | `offers_extracted ≥ 5` or `offers / estimated ≥ 0.8` | +70 |
| Base (yield ≥ 1 offer) | At least 1 offer returned | +50 |
| Base (yield = 0) | No offers found | +10 |
| Schema 100% valid | All offers pass schema check | +20 |
| Schema ≥ 80% valid | | +10 |
| Title populated (all) | `title` non-empty on 100% of offers | +5 |
| Category populated (≥ 80%) | `category` not None on ≥ 80% | +5 |
| Discount populated (≥ 50%) | `discount_min` not None on ≥ 50% | +5 |
| Anti-bot: high risk | `anti_bot_risk == "high"` | −20 |
| Anti-bot: medium risk | `anti_bot_risk == "medium"` | −5 |
| **Sandbox violation override** | Any violation in result | **= 0** |

The full per-term breakdown is stored in `ValidationReport.score_breakdown` (and persisted in `agent_run_outcomes.score_breakdown` JSONB) for display in the operator workspace.

---

## Health Check Agent

`scripts/run_health_check.py` monitors all enabled targets registered in `prefect_target_registry` and flags unhealthy scrapers based on recent scrape-run outcomes.

### What it detects

| Alert Type | Condition | `still_healthy_at_check` |
|---|---|---|
| Zero-yield collapse | All 3 recent runs returned 0 offers | `False` |
| High schema failure rate | Average `schema_valid_pct` < 50% across 3 runs | `False` |
| Data gap | No outcome rows yet for a newly registered target | `True` (no data) |
| Healthy | Recent yields > 0 and schema quality acceptable | `True` |

### Running the health check

```bash
# Run standalone (from the project root):
../env/bin/python scripts/run_health_check.py

# Exit codes:
#   0 — all targets healthy (or no targets registered)
#   1 — one or more targets flagged as unhealthy
#   2 — fatal error (DB connection failure)
```

### Output

Every check writes a new `agent_run_outcomes` row with `run_type="health_check"` and `still_healthy_at_check=True|False`. The `score_breakdown` JSONB stores:
- `unhealthy_reason` — `"zero_yield"` | `"high_schema_failure_rate"` | `null`
- `recent_yield_counts` — list of offer counts from the 3 inspected runs
- `recent_schema_valid_pcts` — list of schema validity percentages per run
- `days_since_registration` — age of the target's registry entry

### Automated repair (deferred)

Detection is the scope of Task 8. Automated repair (re-exploration + selector patching) is left as a `TODO` in `run_health_check.py` with a descriptive comment pointing to `agent/repair_agent.py` (Appendix A of `docs/implementation_plan.md`).

---

## Key Files

### Core Agent Pipeline

| File | Purpose |
|---|---|
| `agent/models.py` | `AgentState`, `SiteAnalysis`, `GeneratedArtifacts`, `ValidationReport` Pydantic models |
| `agent/orchestrator.py` | LangGraph graph definition; wires all agent nodes and conditional routing |
| `agent/exploration_agent.py` | Live Playwright browser, anti-bot scoring, Gemini vision analysis |
| `agent/generation_agent.py` | LLM config generator; saves `config/targets/<brand>.json` |
| `agent/validation_agent.py` | Sandbox execution, schema validation, confidence scoring, DB persistence |
| `agent/registration_agent.py` | Atomic DB registration: competitors UPSERT + registry + audit log |
| `agent/prompts.py` | All LLM prompt constants |
| `auth/approval_rbac.py` | `AgentRole` enum + `get_current_user()` (reads `AGENT_USER_ID` env var) |

### Operator Workspace

| File | Purpose |
|---|---|
| `dashboard/approval_ui.py` | Streamlit human-in-the-loop workspace (3 tabs: pending queue, registry, audit log) |
| `dashboard/app.py` | Existing promotions matrix and timeline monitoring dashboard |
| `dashboard/utils/styles.py` | Shared CSS design system (Inter font, KPI cards, badges) |

### Sandbox Infrastructure

| File | Purpose |
|---|---|
| `docker/Dockerfile.sandbox` | Minimal Python 3.12 image with Playwright + scraper dependencies |
| `docker/setup_egress_network.sh` | Creates/tears down the egress-filtered Docker network |
| `agent/sandbox_entrypoint.py` | Runs **inside** the container; reads config from env vars, executes scraper |
| `agent/sandbox_runner.py` | Host-side: creates container, enforces limits, detects violations |

### Database

| File | Purpose |
|---|---|
| `database/models.py` | All SQLAlchemy models: `Competitor`, `Promotion`, `AgentRunOutcome`, `AgentAuditLog`, `PrefectTargetRegistry` |
| `database/connection.py` | `get_session()`, `init_db()` |
| `alembic/versions/` | Migration chain (`t6a001000001` → `t6b002000002` → `t6c003000003` → `t6d004000004`) |

### Scripts

| File | Purpose |
|---|---|
| `scripts/run_hybrid_promo_scraper.py` | DB-first target loader + HybridPromoExtractor runner |
| `scripts/run_scraper_agent.py` | CLI entry point: explore → generate → validate → register |
| `scripts/run_health_check.py` | **Health check agent** — monitors active targets, logs outcomes |

### Tests

| File | Checks | Docker needed? | DB needed? |
|---|---|---|---|
| `agent/test_validation.py` | 25 | No | No |
| `agent/test_orchestrator.py` | 5 | No | No |
| `agent/test_registration.py` | 20 | No | **Yes** |
| `dashboard/test_approval_ui.py` | 19 | No | **Yes** |
| `agent/test_sandbox.py` | 3 | **Yes** | No |
| `scripts/test_health_check.py` | 26 | No | **Yes** |

### Documentation

| File | Contents |
|---|---|
| `docs/HANDOFF_TASK_8.md` | **Task 8 final summary** — health check architecture, full project checklist, fast-follow items |
| `docs/HANDOFF_TASK_7.md` | Task 7 summary, 4-tab Streamlit approval workspace design decisions |
| `docs/HANDOFF_TASK_6.md` | Task 6 summary, migration chain, DB-first `load_targets()` design, auth hooks |
| `docs/HANDOFF_TASK_5.md` | Task 5 summary, per-scenario `ValidationReport` shapes, sandbox API contract |
| `docs/HANDOFF_TASK_4.md` | Sandbox infrastructure setup, `sandbox_runner` API contract |
| `docs/task_breakdown.md` | Full task breakdown and dependency order (Tasks 0–8) |
| `docs/implementation_plan.md` | Full system design and scoring specification |

---

## Sandbox Violation Types

Returned by `sandbox_runner.run_scraper_in_sandbox()` in the `violations` list:

| Violation | Trigger | Meaning |
|---|---|---|
| `oom_kill` | Exit code 137 | Container killed by kernel — exceeded 512 MB RAM |
| `filesystem_violation` | EROFS in logs | Tried to write outside `/tmp` |
| `network_violation` | Connection error in logs | Tried to reach a blocked host |
| `nonzero_exit` | Any other non-zero exit | Generic scraper failure |
| `timeout_or_crash` | Docker API error / timeout | Container failed to start or timed out |

Any non-empty `violations` list → `confidence_score = 0`, `recommendation = "reject"`.

---

## Target Registry & `load_targets()` Priority

`scripts/run_hybrid_promo_scraper.py::load_targets()` follows this priority chain:

```
1. If --target flag given → load that single file (unchanged)
2. Query prefect_target_registry WHERE enabled=TRUE
   ├── If rows found → load each config_path file
   │     └── Also append filesystem configs NOT already in the registry
   └── If empty or DB unavailable → fall back to filesystem glob
3. _load_filesystem_targets() → glob config/targets/*.json (original behaviour)
```

Filesystem fallback is always intact — if the DB is down or the registry is empty, `load_targets()` silently falls back to the glob scan.

---

## Task Status

| Task | Status | Description |
|---|---|---|
| Task 0 | ✅ Done | Repo orientation — ground truth schema, config shapes, DB setup |
| Task 1 | ✅ Done | `agent/` package scaffold, Pydantic models, LangGraph skeleton |
| Task 2 | ✅ Done | Exploration agent (Playwright + Gemini vision + anti-bot scoring) |
| Task 3 | ✅ Done | Generation agent (LLM config generator, saves `config/targets/`) |
| Task 4 | ✅ Done | Docker sandbox infrastructure (Dockerfile, network, sandbox runner) |
| Task 5 | ✅ Done | Validation agent (sandbox execution, schema validation, confidence scoring) |
| Task 6 | ✅ Done | Auth hooks + atomic DB registration + DB-first target loader |
| Task 7 | ✅ Done | Streamlit human-in-the-loop operator workspace (4-tab approval workspace) |
| Task 8 | ✅ Done | Health check agent (`scripts/run_health_check.py`) — monitors active targets, logs detection outcomes |
