# Myer Retail Competitive Intelligence Platform

A production-grade, concurrent hybrid competitive intelligence system. The platform scrapes promotional offers directly from competitor websites, extracts text and visual banner offers, classifies them with an LLM, routes them into business team feeds via a configurable policy engine, deduplicates by source page, and stores clean promotion rows in PostgreSQL for dashboard review.

---

## System Architecture

```text
BRONZE LAYER — Dynamic browser & anti-bot scraping
  - Parallel Playwright headless scraping via config/targets/*.json
  - Multi-tier AntiBotBypassService (Playwright Stealth → httpx → curl_cffi TLS impersonation)
  - Bypasses PerimeterX / HUMAN Security and Cloudflare Managed Challenges (Turnstile / 403 / 429)
  - Text extraction from configured CSS selectors
  - Screenshot/image extraction for banner-like promotional elements
  - Vision LLM extraction for image-based offers

        ↓

SILVER LAYER — PostgreSQL storage
  - LLM category assignment from env-driven category taxonomy
  - URL-aware deduplication: source + brand + source_url + offer_title
  - Stores offer title, category, brand, source URL, confidence, timestamps

        ↓

ROUTING LAYER — Team Policy Engine
  - config/teams.json defines which categories and brands route to which team
  - TeamPolicyEngine evaluates each promotion and writes to promotion_team_assignments
  - Supports allowlists, denylists, regional suffix normalisation

        ↓

GOLD LAYER — Streamlit Dashboard
  - Team-wise and category-wise promotional feeds
  - Weekly competitor matrix by team, brand, and day of week
  - Filters by team, category, brand, date, and extraction source
```

---

## Extraction Approach

The scraper uses a hybrid extraction strategy with automated anti-bot bypass:

- **Anti-Bot Bypass Architecture (`AntiBotBypassService`)** — Bypasses bot verification screens (Cloudflare Managed Challenges, Turnstile, PerimeterX v2, `local_rate_limited` stubs) by executing a multi-tier fallback cascade. When headless browsers encounter rate limits or 403 blocks, `AntiBotBypassService` uses `curl_cffi` to execute authentic Chrome/Firefox TLS Client Hello (JA3/JA4) impersonation, retrieving the full page DOM.
- **Text extraction** collects visible promotional text from configured CSS selectors.
- **Screenshot/image extraction** captures banner-like page elements and sends them to the configured vision LLM.
- **LLM category classification** runs once per brand scrape as a batched text call. It assigns each extracted offer to one category from the environment-driven taxonomy.
- **Category fallback override** — if the LLM assigns "Others" but the target config declares a top-level `category` field (e.g. `"category": "Beauty"`), the system overrides the LLM's choice. This ensures brand-specific target configs always produce the correct category.
- **URL-aware deduplication** keeps identical promo text separate when it appears on different pages, such as `/men/` and `/kids/`.

> If a target config defines **no** `text_selectors` and no `screenshot_selectors`, the extractor skips that phase entirely. No fallback selectors are used.

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

### Example `config/teams.json` entry

```json
{
  "Menswear": {
    "team_id": "menswear_team",
    "categories": ["Menswear", "Others"],
    "allowed_brands": [
      "Tommy Hilfiger", "Calvin Klein", "Gazman", "ASOS", "Superdry"
    ]
  }
}
```

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

This re-evaluates and updates `promotion_team_assignments` for all 
existing promotions in the database without scraping anything.

---

## Database Schema

### `competitors` table

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `name` | text unique | Brand/competitor name |
| `enabled` | bool | Active scraping flag |
| `added_at` | timestamp | Creation timestamp |
| `modified_at` | timestamp | Last update timestamp |

### `promotions` table

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

### `promotion_team_assignments` table

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Primary key |
| `promotion_id` | int FK | References promotions |
| `team_id` | text | Team identifier from teams.json |
| `assigned_at` | timestamp | Assignment timestamp |

---

## Tech Stack

| Component | Technology |
|---|---|
| Browser Automation | Playwright Python |
| Vision / Text LLM | LiteLLM gateway (Claude Haiku 4.5) |
| Database | PostgreSQL + SQLAlchemy |
| Orchestration | Prefect |
| Dashboard | Streamlit |

### Pinned dependency notes

| Package | Pinned Version | Reason |
|---|---|---|
| `pyarrow` | `17.0.0` | PyArrow 25.x causes SIGSEGV on macOS arm64 (Apple Silicon) with NumPy 2.x |
| `altair` | `5.3.0` | Streamlit 1.57 explicitly blocks altair 5.4.0 and 5.4.1 |

---

## Project Structure

```text
.
├── alembic/                               # Alembic database migration scripts
├── alembic.ini                             # Alembic configuration
├── config/
│   ├── teams.json                         # Business team routing rules
│   └── targets/                           # Per-brand scrape configuration (one JSON per brand)
├── database/
│   ├── connection.py                      # SQLAlchemy engine/session setup
│   └── models.py                          # ORM models: Competitor, Promotion, PromotionTeamAssignment
├── flows/
│   └── master_pipeline.py                 # Prefect flow for parallel scraping
├── llm/                                   # LLM pricing, metrics, and helper utilities
├── promo_scraper/
│   ├── hybrid_promo_extractor.py          # Core extraction, LLM calls, deduplication, cost tracking
│   └── pipelines.py                       # Scrapy-compatible DB pipeline
├── services/
│   └── team_policy_engine.py             # Reads teams.json, evaluates and routes promotions to teams
├── scripts/
│   ├── init_db.py                         # Create/update DB schema
│   ├── reset_db.py                        # Truncate all tables
│   ├── run_hybrid_promo_scraper.py        # Run a single target scrape
│   └── reassign_teams.py                  # Re-apply team routing to all existing DB promotions
├── dashboard/
│   ├── app.py                             # Streamlit dashboard (team-wise + category-wise views)
│   └── utils/
│       ├── db.py                          # DB query helpers (cached with @st.cache_resource)
│       ├── exporter.py                    # Excel export for weekly competitor matrix
│       └── styles.py                      # CSS design tokens and component helpers
├── tests/
│   └── test_team_policy_engine.py         # Unit tests for TeamPolicyEngine routing logic
├── requirements.txt
├── .env.example                           # Environment configuration template
└── .env                                   # Local configuration and credentials
```

---

## Quickstart

### 1. Install Dependencies

```bash
python -m venv env
source env/bin/activate
pip install -r requirements.txt
playwright install
```

### 2. Configure Environment

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql://postgres:password@localhost:5432/promo_db_v3

# LiteLLM / corporate gateway
LLM_PROVIDER=litellm
LITELLM_API_KEY=your_litellm_key
LITELLM_API_BASE=https://your-litellm-gateway.example/v1
VISION_LLM_MODEL=claude-haiku-4.5

# Direct Gemini fallback
GEMINI_API_KEY=your_gemini_api_key

# Category taxonomy used by the LLM classifier
PROMO_CATEGORIES="Home, Entertainment, Womens, Beauty, Kids, Toys, Menswear, Footwear, Others"

MAX_CONCURRENT_BROWSERS=3
VISION_API_MIN_DELAY=1.0

# Cost estimate rate for Vision API calls (order-of-magnitude, not billing-accurate)
VISION_COST_PER_MILLION_TOKENS_USD=1.00

# Dashboard rolling window (days of history to load on startup)
DASHBOARD_LOOKBACK_DAYS=90
```

### 3. Initialize the Database

```bash
python scripts/init_db.py
```

Creates all tables (`competitors`, `promotions`, `promotion_team_assignments`) if they don't exist.

### 4. Run the Scraper Pipeline

You can run the scraper in multiple ways:

#### A. Direct Manual Execution (Single-pass)
```bash
python flows/master_pipeline.py
```

> The pipeline runs all configured target brands in parallel, handles automatic retries for errored and zero-offer brands after the run, and generates a summary report.

#### B. Daily 6 AM Cron Scheduled Execution (Prefect Serve)
Run the master pipeline as a background daemon on a daily cron schedule (`0 6 * * *` = 6:00 AM daily):
```bash
python flows/master_pipeline.py --serve
```
*(Optional)* Override the schedule (e.g. 6:30 AM daily):
```bash
python flows/master_pipeline.py --serve --cron "30 6 * * *"
```

#### C. Interactive Streamlit UI Runner
Launch the dashboard and navigate to the **Scraper Runner** page to run full pipelines or select individual brands interactively with real-time log streaming.

#### D. Run a Single Target Config
Run a single target for quick testing/debugging:

```bash
python scripts/run_hybrid_promo_scraper.py --target config/targets/bobbi_brown.json
```

### 5. Apply Team Routing

Team assignments are applied automatically after each scrape. To manually re-apply after editing `teams.json`:

```bash
python scripts/reassign_teams.py
```

### 6. Launch the Dashboard

```bash
streamlit run dashboard/app.py
```

Open [http://localhost:8501](http://localhost:8501).

---

## Logging & Retries

### Timestamped File Logging
Every scraper execution automatically creates a timestamped log file in the `/logs/` directory:
- `logs/scraper_<YYYYMMDD_HHMMSS>_pipeline.log` (Prefect master pipeline runs)
- `logs/scraper_<YYYYMMDD_HHMMSS>_cli.log` (Direct CLI single-target runs)
- `logs/scraper_<YYYYMMDD_HHMMSS>_ui.log` (Streamlit Runner runs)

### Post-Pipeline Retry Pass
After the main pipeline execution completes, the pipeline automatically detects brands that encountered errors or returned **0 offers** (e.g. due to temporary rate-limiting or anti-bot challenges):
1. **Attempt 1**: Waits 5 minutes (`RETRY_DELAY_SECONDS=300`), then retries failed/zero-offer targets sequentially.
2. **Attempt 2**: Waits 10 minutes (`RETRY_DELAY_SECONDS=600`) for any remaining failed targets.
3. Generates a separate **Retry Report** block summarizing recovered vs still-failing brands.

---

## Target Configuration

Each file in `config/targets/` controls how a brand is scraped.

```json
{
  "brand": "Bobbi Brown",
  "source_url": [
    {
      "url": "https://www.bobbibrown.com.au/",
      "category_hint": "Cosmetics, makeup, and skincare products -> Beauty. Always Beauty."
    },
    {
      "url": "https://www.bobbibrown.com.au/offers",
      "category_hint": "Cosmetics, makeup, and skincare products -> Beauty. Always Beauty."
    }
  ],
  "spider": "image_promo",
  "extraction_strategy": "text",
  "category": "Beauty",
  "text_selectors": [
    ".content-over-media__text",
    ".content-block--mpp-header .content-block__line--content"
  ],
  "screenshot_selectors": [],
  "min_image_width": 400,
  "min_image_height": 150,
  "min_aspect_ratio": 1.2,
  "exclude_url_patterns": ["/logo", "/icon", "social", "payment"],
  "request_delay_seconds": 4,
  "scroll_depth": 3,
  "enabled": true,
  "promo_keywords_pattern": "\\d+\\s*%|\\boff\\b|\\bsale\\b"
}
```

`source_url` may be a single string, a list of strings, or a list of `{"url": "...", "category_hint": "..."}` objects for per-page LLM context.

- **`category_hint`** (per-URL or top-level): Injected into the LLM categorization prompt as a strong prior. Guides the LLM to prefer this category over "Others" for ambiguous offers.
- **`category`** (top-level): Fallback category override. If the LLM assigns "Others" but this field is set, the system overrides with this value. Also used as default category fallback.
- **`promo_keywords_pattern`**: Optional regex override for promotional text filtering. If not set, a broad default pattern is used.
- **`enabled`**: Set to `false` to temporarily disable a target without deleting it. Defaults to `true`.

If both `text_selectors` and `screenshot_selectors` are empty lists, the extractor skips that phase entirely. No fallback selectors are used.

---

## Deduplication

Promotions are fingerprinted using:

```text
SHA-256(source_name + brand + source_url + offer_title + scraped_date)
```

Identical offer text on different pages (e.g. `/men/` vs `/kids/`) is stored as separate rows.
The date component ensures the same offer is re-recorded daily for timeline tracking.

---

## LLM Cost Tracking

Each scrape reports cumulative API cost. The estimate uses the model configured via `VISION_LLM_MODEL` (default: Gemini 2.5 Flash for direct, or any model via LiteLLM gateway):

```text
USD per 1M input tokens (configurable via VISION_COST_PER_MILLION_TOKENS_USD, default 1.00)
image_tokens ≈ (width × height / 800) + 170
text_tokens  ≈ characters / 4
```

This is an order-of-magnitude estimate, not a billing-accurate figure.

---

## Dashboard

The Streamlit dashboard at `http://localhost:8501` provides:

- **Feed Metrics** — total promotions, active brands, and offers scraped today
- **Promotions by Brand** — horizontal bar chart
- **Extraction Timeline** — daily scrape volume line chart
- **Weekly Competitor Matrix (Team View)** — per-team pivot of brands × weekday with offer titles
- **Extracted Promotions Table** — filterable table with brand, assigned team feeds, AI category, source, offer title, URL, confidence, and timestamp
- **Excel Export** — download the weekly matrix as a formatted `.xlsx` file

### Sidebar Filters

| Filter | Description |
|---|---|
| Select Business Teams | Multi-select by team (Menswear, Beauty, Home, etc.) |
| Select Categories | Multi-select by LLM-assigned category |
| Select Brands | Multi-select by competitor brand |
| Start / End Date | Date range for scraped_at |
| Select Extraction Sources | Filter by `text_scraper` or `image_promo` |

Promotions not assigned to any team appear under **Unassigned / General**.

---

## Running Tests

```bash
python -m pytest tests/ -v
```
