# Autonomous Scraping Agent

An autonomous agent system designed to dynamically explore competitor websites, analyze layouts, evaluate anti-bot protections, identify visual promotional areas, determine optimal CSS extraction strategies, validate scrapers in a Docker sandbox, and register approved configurations.

The system is built on **LangGraph** to orchestrate step-by-step agent executions, using **Playwright** for browser automation, **LiteLLM** for Claude model access, and direct **Gemini** API fallbacks.

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
      Conditional
      Routing (based on confidence score & sandbox violations)
      ┌───┼───┐
      │   │   │
      │   │   ▼
      │   │ [END] (score < 70 or any sandbox violation → Rejected)
      │   │
      │   ▼
      │ [END] (score 70–89 → Pending Human Review via Streamlit UI)
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

3. **Validation Agent** (`agent/validation_agent.py`) — **fully implemented (Task 5)**:
   - Runs the generated scraper inside the Docker sandbox via `sandbox_runner.run_scraper_in_sandbox()`.
   - If sandbox violations are detected, immediately forces `confidence_score=0`, `recommendation="reject"`.
   - Parses offer data from container stdout, validates each offer against the schema.
   - Computes a **confidence score** (0–100+) using yield rate, schema quality, field-population rates, and anti-bot risk penalty.
   - Selects up to 3 sample offers for human preview.
   - Populates `state.validation_report` (`ValidationReport`) and sets `state.status = "validation"`.

4. **Registration Agent** (`agent/orchestrator.py` — stub, Task 6+):
   - Stores and activates approved configurations for production scraping.

---

## Setup & Initialization

### 1. Requirements
- Python 3.10+
- Google Gemini API Key
- LiteLLM Gateway / Corporate API Access (optional; falls back to direct Gemini API)
- **Docker Desktop** (for sandbox execution — see [Docker Setup](#docker-setup--sandbox-infrastructure) below)

### 2. Installation
Clone the repository, initialize your virtual environment, and install dependencies:

```bash
# Create and activate virtual environment
python -m venv env
source env/bin/activate

# Install required packages
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 3. Environment Configuration
Create a `.env` file in the project root:

```env
# LiteLLM Configuration (Primary)
LITELLM_API_KEY=your_litellm_api_key
LITELLM_API_BASE=https://your-litellm-gateway.example/v1
LLM_MODEL=openai/claude-haiku-4.5
VISION_LLM_MODEL=openai/claude-haiku-4.5

# Direct Gemini Fallback Configuration
GEMINI_API_KEY=your_gemini_api_key

# Promotion categories for HybridPromoExtractor
PROMO_CATEGORIES="Home, Entertainment, Womens, Beauty, Kids, Toys, Menswear, Footwear, Null"
```

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

Docker Desktop on Linux uses a non-standard socket (`~/.docker/desktop/docker.sock`). The `sandbox_runner` auto-detects this. Start Docker with:

```bash
systemctl --user start docker-desktop
```

> Run once per login session. To check it's running: `docker info`

### Step 2 — Build the sandbox image

Run from the **repository root** (not from inside `docker/`):

```bash
docker build -f docker/Dockerfile.sandbox -t promo-scraper-sandbox:latest .
```

> The build context must be the repo root — `COPY promo_scraper/` and `COPY agent/sandbox_entrypoint.py` resolve relative to it.

### Step 3 — Create the egress network

```bash
# With iptables (Linux Docker Engine — requires sudo):
sudo ./docker/setup_egress_network.sh create <target-domain>
# Example:
sudo ./docker/setup_egress_network.sh create www.vanheusen.com.au

# Without iptables (Docker Desktop — internal bridge only):
docker network create \
  --driver bridge \
  --subnet 172.28.0.0/16 \
  --internal \
  scraper-egress-only
```

To tear down:
```bash
# Docker Engine:
sudo ./docker/setup_egress_network.sh teardown

# Docker Desktop:
docker network rm scraper-egress-only
```

> **Note:** The network persists until explicitly removed or Docker Desktop restarts. If missing after a restart, re-run the create command above.

---

## Running the Tests

### Validation Agent Tests (no Docker required)

Tests all 5 validation scenarios by mocking `run_scraper_in_sandbox`:

```bash
../env/bin/python agent/test_validation.py
```

Expected output:
```
[test_clean_5_offers]
  ✓ recommendation == auto_approve — got: 'auto_approve'
  ✓ confidence_score >= 90 — got: 105
  ✓ offers_extracted == 5 — got: 5
  ✓ schema_valid is True — got: True
  ...
  ✓ OVERALL test_clean_5_offers

[test_zero_offers]
  ✓ recommendation == reject
  ✓ confidence_score == 10
  ...

[test_sandbox_violation]
  ✓ recommendation == reject
  ✓ confidence_score == 0
  ...

[test_high_antibot_penalty]
  ✓ high-risk score is exactly 20 lower than low-risk score
  ✓ score_breakdown.antibot_penalty == -20
  ...

[test_timeout_crash]
  ✓ recommendation == reject
  ✓ confidence_score == 0
  ...

==================================================
Results: 25/25 checks passed
🎉 All validation agent tests passed!
```

### Sandbox Violation Tests (requires Docker)

Verifies that the Docker sandbox correctly blocks filesystem writes, unauthorized network access, and memory exhaustion:

```bash
../env/bin/python agent/test_sandbox.py
```

Expected output:
```
✓ test_filesystem_violation — PASSED
✓ test_network_violation    — PASSED
✓ test_memory_violation     — PASSED

Results: 3/3 passed
✓  All 3 tests PASSED.
```

### Orchestrator Unit Tests (mocked, no Docker required)

Verifies graph routing logic across all conditional edges:

```bash
../env/bin/python agent/test_orchestrator.py
```

Expected output: `🎉 All orchestrator and generation agent tests passed successfully!`

### Exploration Agent (live site — hits external APIs)

```bash
PYTHONPATH=. ../env/bin/python3 -c "
from agent.exploration_agent import explore_site
res = explore_site('https://www.oxfordshop.com.au/', 'Oxford Shop')
print('Strategy:', res.extraction_strategy)
print('Anti-bot risk:', res.anti_bot_risk)
"
```

---

## Confidence Scoring (Validation Agent)

The validation agent computes a confidence score (0–100+) that determines the routing outcome:

| Score band | Routing | Meaning |
|---|---|---|
| ≥ 90 | `auto_approve` → Registration | Scraper meets quality bar; config activated |
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

The full per-term breakdown is stored in `ValidationReport.score_breakdown` for display in the Task 6 Streamlit UI.

---

## Key Files

### Core Agent Pipeline

| File | Purpose |
|---|---|
| `agent/models.py` | `AgentState`, `SiteAnalysis`, `GeneratedArtifacts`, `ValidationReport` Pydantic models |
| `agent/orchestrator.py` | LangGraph graph definition; wires all agent nodes and conditional routing |
| `agent/exploration_agent.py` | Live Playwright browser, anti-bot scoring, Gemini vision analysis |
| `agent/generation_agent.py` | LLM config generator; saves `config/targets/<brand>.json` |
| `agent/validation_agent.py` | **Task 5** — sandbox execution, schema validation, confidence scoring |
| `agent/prompts.py` | All 5 LLM prompt constants |

### Sandbox Infrastructure

| File | Purpose |
|---|---|
| `docker/Dockerfile.sandbox` | Minimal Python 3.12 image with Playwright + scraper dependencies |
| `docker/setup_egress_network.sh` | Creates/tears down the egress-filtered Docker network |
| `agent/sandbox_entrypoint.py` | Runs **inside** the container; reads config from env vars, executes scraper |
| `agent/sandbox_runner.py` | Host-side: creates container, enforces limits, detects violations |

### Tests

| File | Purpose | Docker needed? |
|---|---|---|
| `agent/test_validation.py` | 5 scenarios for validation agent (mocked sandbox) | No |
| `agent/test_orchestrator.py` | Graph routing + generation agent unit tests | No |
| `agent/test_sandbox.py` | 3 live sandbox violation scenarios | **Yes** |

### Documentation

| File | Contents |
|---|---|
| `docs/HANDOFF_TASK_5.md` | Task 5 summary, per-scenario `ValidationReport` shapes, Task 6 API surface |
| `docs/HANDOFF_TASK_4.md` | Sandbox infrastructure setup, `sandbox_runner` API contract |
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

## Task Status

| Task | Status | Description |
|---|---|---|
| Task 0 | ✅ Done | Repo scaffold, DB schema, base models |
| Task 1 | ✅ Done | Exploration agent (Playwright + Gemini vision) |
| Task 2 | ✅ Done | LangGraph orchestrator + routing |
| Task 3 | ✅ Done | Generation agent (LLM config generator) |
| Task 4 | ✅ Done | Docker sandbox infrastructure |
| Task 5 | ✅ Done | Validation agent (sandbox execution + confidence scoring) |
| Task 6 | 🔲 Next | Streamlit approval UI (human-in-the-loop for `pending` reports) |
