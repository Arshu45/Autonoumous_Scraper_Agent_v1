# Myer Retail Competitive Intelligence Platform

A production-grade, concurrent hybrid competitive intelligence system with **Autonomous Scraper Agent orchestration**. The platform automatically discovers, builds, validates, and registers brand scraper configurations via a **LangGraph agent pipeline**, executes scrapers inside an isolated **Docker Sandbox container**, routes offers into business team feeds via a configurable policy engine, deduplicates by source page, and stores clean promotion rows in PostgreSQL for dashboard review.

---

## System Architecture

```text
AUTONOMOUS AGENT LAYER — LangGraph Scraper Generation & Validation
  - Site Exploration: Navigates target URL, captures DOM/screenshots, measures anti-bot risk
  - Target Config Generation: Generates custom CSS selectors, delays, and category hints
  - Sandbox Validation: Executes scraper inside isolated Docker container with egress filtering
  - Confidence Scoring: Evaluates schema validity, offer yield, and selector accuracy (0-100)
  - Registration & Audit: Atomically updates DB target registry and logs immutable audit trail

        ↓

BRONZE LAYER — Dynamic browser & anti-bot scraping
  - Parallel Playwright headless scraping via config/targets/*.json
  - Multi-tier AntiBotBypassService (Playwright Stealth → httpx → curl_cffi TLS impersonation)
  - Bypasses PerimeterX / HUMAN Security and Cloudflare Managed Challenges (Turnstile / 403 / 429)
  - Text extraction from configured CSS selectors
  - Screenshot/image extraction for banner-like promotional elements
  - Vision LLM extraction for image-based offers

        ↓

SILVER LAYER — PostgreSQL storage & Health Monitoring
  - LLM category assignment from env-driven category taxonomy
  - URL-aware deduplication: source + brand + source_url + offer_title
  - Stores offer title, category, brand, source URL, confidence, timestamps
  - Continuous Health Check agent monitors active targets for decay or selector drift

        ↓

ROUTING LAYER — Team Policy Engine
  - config/teams.json defines which categories and brands route to which team
  - TeamPolicyEngine evaluates each promotion and writes to promotion_team_assignments
  - Supports allowlists, denylists, regional suffix normalisation

        ↓

GOLD LAYER — Streamlit Intelligence & Approval Dashboard
  - Human-in-the-loop Approval UI: Review pending agent configurations, dry-run tests, and audit logs
  - Team-wise and category-wise promotional feeds
  - Weekly competitor matrix by team, brand, and day of week
  - Filters by team, category, brand, date, and extraction source
```

---

## Autonomous Scraper Agent & Sandbox Execution

The platform includes an end-to-end **LangGraph Autonomous Agent** that builds and verifies scrapers for new retail sites without human manual coding:

### 1. Agent Workflow Nodes (`agent/`)
- **Site Exploration (`exploration_agent.py`)**: Navigates target URLs, detects anti-bot challenges (PerimeterX, Cloudflare), inspects DOM structure, and takes high-resolution screenshots. Sends screenshots to a Vision LLM for promotional area identification.
- **Config & Scraper Generation (`generation_agent.py`)**: Generates optimized JSON target configurations (`config/targets/<brand>.json`) with custom CSS text selectors, screenshot selectors, and category hints. Validates selectors against the live page using Playwright to prune hallucinated selectors.
- **Sandbox Validation (`validation_agent.py`)**: Executes generated scraper code inside a single-use Docker container, enforcing strict time limits (default: 240s) and isolation. Validates extracted offers against a schema and calculates a **Confidence Score (0–100)**.
- **Registration & Audit (`registration_agent.py`)**: Auto-approves configurations with confidence ≥ 90. Atomically updates `competitors`, `prefect_target_registry`, and writes an immutable entry to `agent_audit_log` inside a single `session.begin()` transaction block.

### 2. Confidence Score Routing
| Score Range | Route | Action |
|---|---|---|
| ≥ 90 | `auto_approve` | Automatically registered to Prefect pipeline |
| 70–89 | `pending` | Queued for human review in Streamlit UI |
| < 70 | `reject` | Discarded; logged for diagnostics |

### 3. Docker Sandbox Security (`agent/sandbox_runner.py` & `docker/Dockerfile.sandbox`)
- **Network Egress Isolation**: Runs container on `scraper-egress-only` Docker network, blocking non-essential outbound traffic.
- **Resource Limits**: 768 MB RAM, 1 CPU, 128 PIDs, read-only root filesystem with tmpfs for `/tmp` and `/dev/shm`.
- **Security Hardening**: All Linux capabilities dropped (`cap_drop=ALL`), `no-new-privileges` enforced.
- **Violation Detection**: OOM kills, filesystem writes, and network violations are detected and force confidence score to 0.
- **LiteLLM Offline Configuration**: Configured with `LITELLM_LOCAL_MODEL_COST_MAP=True` to prevent container stalls from remote GitHub cost-map fetches.

---

## Human-in-the-Loop Approval UI

The Streamlit dashboard includes a dedicated **Agent Approval UI** (`dashboard/approval_ui.py`):

- **Pending Review Feed**: Shows generated configurations requiring human sign-off (confidence 70–89).
- **Dry-Run Validation**: Test scrapers interactively before committing them to production.
- **Config Editor**: Edit target JSON selectors directly in the UI with instant JSON syntax validation.
- **Audit Log Viewer**: Full historical audit trail tracking user approvals, rejections, and manual overrides.

---

## Extraction Approach

The scraper uses a hybrid extraction strategy with automated anti-bot bypass:

- **Anti-Bot Bypass Architecture (`AntiBotBypassService`)** — Bypasses bot verification screens (Cloudflare Managed Challenges, Turnstile, PerimeterX v2, `local_rate_limited` stubs) by executing a multi-tier fallback cascade. When headless browsers encounter rate limits or 403 blocks, `AntiBotBypassService` uses `curl_cffi` to execute authentic Chrome/Firefox TLS Client Hello (JA3/JA4) impersonation, retrieving the full page DOM.
- **Text extraction** collects visible promotional text from configured CSS selectors.
- **Screenshot/image extraction** captures banner-like page elements and sends them to the configured vision LLM.
- **LLM category classification** runs once per brand scrape as a batched text call. It assigns each extracted offer to one category from the environment-driven taxonomy.
- **Category fallback override** — if the LLM assigns "Others" but the target config declares a top-level `category` field (e.g. `"category": "Beauty"`), the system overrides the LLM's choice. This ensures brand-specific target configs always produce the correct category.
- **URL-aware deduplication** keeps identical promo text separate when it appears on different pages, such as `/men/` and `/kids/`.

Categories are configured in `.env`:

```env
PROMO_CATEGORIES="Home, Entertainment, Womens, Beauty, Kids, Toys, Menswear, Footwear, Others"
```

---

## Team Routing

Business team routing is controlled by `config/teams.json`. This is completely separate from scraping — it is applied after promotions are stored in the database.

### How it works

1. Each promotion has an LLM-assigned `category` and a `brand`.
2. `TeamPolicyEngine` reads `teams.json` and evaluates each promotion against:
   - **`categories`** — the promotion's category must match one in the team's list.
   - **`allowed_brands`** — if specified, the promotion's brand must be in this list.
   - **`excluded_brands`** — if specified, the promotion's brand must NOT be in this list.
3. Matching team IDs are written to the `promotion_team_assignments` table.
4. A promotion can belong to multiple teams.

### Current team configuration

| Team | Team ID | Categories |
|---|---|---|
| Womens (WIFA) | `womens_wifa` | Womens |
| Kids & Toys | `kids_toys_team` | Kids, Toys |
| Toys | `toys_team` | Toys |
| Menswear | `menswear_team` | Menswear, Others |
| Entertainment | `entertainment_team` | Entertainment |
| Beauty | `beauty_team` | Beauty |
| Home | `home_team` | Home |

### Updating team rules (no re-scrape needed)

After editing `config/teams.json`, re-apply routing to all existing promotions:

```bash
python scripts/reassign_teams.py
```

---

## Database Schema

The system uses **6 core PostgreSQL tables**:

### 1. `competitors` table
Tracks competitor brands and agent generation metadata.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `name` | text unique | Brand/competitor name |
| `enabled` | bool | Active scraping flag |
| `added_at` | timestamp | Creation timestamp |
| `modified_at` | timestamp | Last update timestamp |
| `extraction_strategy` | text | `hybrid`, `text`, or `image` |
| `agent_generated` | bool | True if created by autonomous agent |
| `agent_confidence` | int | Agent validation confidence score (0-100) |
| `agent_notes` | text | Diagnostic notes from validation agent |
| `source_url` | text | Main website URL |

### 2. `promotions` table
Stores scraped promotional offers.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `competitor_id` | int FK | References competitors |
| `brand` | text | Denormalised brand name |
| `offer_title` | text | Promotional headline |
| `category` | text | LLM-assigned category |
| `source_name` | text | Extractor type: `text_scraper` or `image_promo` |
| `source_url` | text | Exact page URL scraped |
| `extraction_confidence` | text | `high`, `medium`, or `low` |
| `offer_hash` | text | SHA-256 fingerprint for deduplication |
| `scraped_at` | timestamp | Latest scrape time |
| `created_at` | timestamp | Row insertion time |

### 3. `promotion_team_assignments` table
Junction table linking promotions to business teams.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `promotion_id` | int FK | References promotions |
| `team_id` | text | Team identifier from teams.json |
| `assigned_at` | timestamp | Assignment timestamp |

### 4. `prefect_target_registry` table
Active Prefect target registry for registered scrapers.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `brand` | text unique | Target brand name |
| `config_path` | text | Path to target JSON config |
| `enabled` | bool | Active flag |
| `registered_at` | timestamp | Registration timestamp |
| `registered_by` | text | User ID / Agent identifier |

### 5. `agent_audit_log` table
Immutable audit trail for agent actions and approvals.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `brand` | text | Brand identifier |
| `user_id` | text | User or agent ID |
| `action` | text | `trigger_run`, `approve`, `reject`, `edit_config` |
| `details` | jsonb | Configuration diff or rejection reason |
| `created_at` | timestamp | Action timestamp |

### 6. `agent_run_outcomes` table
Records historical outcome and confidence metrics for health checks.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `brand` | text | Competitor brand |
| `run_type` | text | `initial_validation` or `health_check` |
| `confidence_score` | int | Confidence score (0-100) |
| `score_breakdown` | jsonb | Detailed metric scoring |
| `recommendation` | text | `auto_approve`, `pending`, or `reject` |
| `offers_extracted` | int | Number of extracted offers |
| `was_auto_approved` | bool | Auto-approval status |
| `days_since_registration` | int | Days since brand was registered (health checks) |
| `still_healthy_at_check` | bool | Whether target is still healthy (health checks) |
| `checked_at` | timestamp | Check timestamp |

---

## Tech Stack

| Component | Technology |
|---|---|
| Agent Orchestration | LangGraph Python |
| Container Isolation | Docker (Playwright + Python) |
| Browser Automation | Playwright Python + AntiBotBypassService (`curl_cffi` TLS) |
| Vision / Text LLM | LiteLLM gateway → Claude / Groq / Gemini (automatic failover) |
| Database | PostgreSQL + SQLAlchemy |
| Orchestration | Prefect |
| Dashboard | Streamlit |

---

## Project Structure

```text
.
├── agent/                                  # LangGraph Autonomous Scraper Agent
│   ├── models.py                          # Agent state schemas (AgentState, ValidationReport)
│   ├── orchestrator.py                    # LangGraph state graph builder & conditional routing
│   ├── exploration_agent.py              # Site exploration & anti-bot risk analysis
│   ├── generation_agent.py               # Selector & target config generator
│   ├── validation_agent.py               # Sandbox execution, schema validation & scoring
│   ├── sandbox_runner.py                 # Docker sandbox executor & violation detection
│   ├── sandbox_entrypoint.py             # Container bootstrap script (runs inside Docker)
│   ├── registration_agent.py             # Atomic DB registration & audit logging
│   └── prompts.py                         # LLM prompt templates (vision, DOM, config gen)
├── auth/
│   └── approval_rbac.py                   # Role-based access control (RBAC) placeholder
├── config/
│   ├── teams.json                         # Business team routing rules
│   └── targets/                           # Per-brand scrape configuration (one JSON per brand)
├── database/
│   ├── connection.py                      # SQLAlchemy engine/session setup with connection pooling
│   └── models.py                          # ORM models: Competitor, Promotion, Audit Logs, Registry
├── dashboard/
│   ├── app.py                             # Streamlit dashboard entrypoint
│   ├── approval_ui.py                     # Human-in-the-loop Approval & Audit UI
│   └── utils/                             # DB helpers, Excel exporter, styling
├── docker/
│   ├── Dockerfile.sandbox                 # Isolated sandbox container image definition
│   └── setup_egress_network.sh            # Docker network egress filtering setup script
├── flows/
│   └── master_pipeline.py                 # Prefect flow for parallel scraping & retry passes
├── llm/
│   ├── base.py                            # Abstract LLMClient base class
│   ├── factory.py                         # LLM provider factory with fallback support
│   ├── groq_client.py                     # Groq (LangChain) client implementation
│   └── litellm_client.py                  # LiteLLM unified client implementation
├── promo_scraper/
│   ├── hybrid_promo_extractor.py          # Core extraction, LLM calls, deduplication
│   └── anti_bot_service.py                # Multi-tier TLS & browser bypass
├── scripts/
│   ├── run_scraper_agent.py               # CLI entrypoint for autonomous scraper agent
│   ├── run_health_check.py                # Scraper decay & health monitoring agent
│   ├── run_hybrid_promo_scraper.py        # Run single target scrape
│   ├── init_db.py                         # Initialize database tables and schema migrations
│   ├── reset_db.py                        # Truncate tables (with optional --keep-registry flag)
│   ├── reassign_teams.py                  # Re-apply team routing rules
│   ├── logging_setup.py                   # Centralised logging configuration
│   └── check_consumption.py               # LiteLLM API consumption checker
├── services/                              # Business logic services (email, reporting)
├── tests/                                 # Pytest test suite
│   ├── test_team_policy_engine.py
│   ├── test_exporter.py
│   └── agent/                             # Agent & sandbox test suite
├── alembic/                               # Database migration scripts
├── alembic.ini                            # Alembic configuration
├── requirements.txt
├── .env.example                           # Environment configuration template
└── .env                                   # Local configuration and credentials
```

---

## Quickstart

### 1. Install Dependencies & Build Sandbox Image

```bash
python -m venv env
source env/bin/activate
pip install -r requirements.txt
playwright install

# Build the Docker Sandbox image for agent validation
docker build -t promo-scraper-sandbox:latest -f docker/Dockerfile.sandbox .

# Create the egress-filtered Docker network for sandbox isolation
./docker/setup_egress_network.sh create <target-domain>
```

### 2. Initialize Database

```bash
python scripts/init_db.py
```

### 3. Run Autonomous Agent for New Brand

To discover, build, and register a new competitor brand automatically:

```bash
python scripts/run_scraper_agent.py --url "https://www.vanheusen.com.au/" --brand "Van Heusen"
```

### 4. Run Scraper Pipeline

```bash
python flows/master_pipeline.py
```

### 5. Launch Dashboard & Approval UI

```bash
streamlit run dashboard/app.py
```

Open [http://localhost:8501](http://localhost:8501) to view promotional feeds and manage pending agent approvals.

---

## Running Tests

```bash
pytest
```
