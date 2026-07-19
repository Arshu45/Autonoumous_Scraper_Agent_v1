# Autonomous Scraping Agent

An autonomous agent system designed to dynamically explore competitor websites, analyze layouts, evaluate anti-bot protections, identify visual promotional areas, determine optimal CSS extraction strategies, and register scraping configurations.

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
   │ Generation  │  ◄── generates CSS configuration for scraping target
   └─────────────┘
          │
          ▼
   ┌─────────────┐
   │ Validation  │  ◄── runs scraper in Docker sandbox, scores confidence
   └─────────────┘
          │
      Conditional
      Routing (based on validation score & sandbox violations)
      ┌───┼───┐
      │   │   │
      │   │   ▼
      │   │ [END] (Score < 70 or Sandbox Violation → Rejected)
      │   │
      │   ▼
      │ [END] (Score 70 - 89 → Pending Human Review)
      │
      ▼
   ┌──────────────┐
   │ Registration │ ◄── registers verified config (Score >= 90)
   └──────────────┘
          │
          ▼
        [END]
```

### Agent Nodes & Responsibilities

1. **Exploration Agent** (`agent/exploration_agent.py`):
   - Opens target site headlessly with stealth flags, custom headers, and webdriver detection blocks.
   - Evaluates page height, triggers dynamic scroll actions to bypass lazy loading, and resets scroll.
   - Computes an **anti-bot risk score** based on DOM presence of security elements (CAPTCHA, Cloudflare, etc.) and response headers.
   - Cleans non-semantic HTML tags (e.g. `<script>`, `<style>`, `<head>`, `<svg>`) to generate a truncated DOM.
   - Uses multimodal models (LiteLLM Claude falling back to direct Gemini) to identify visual promotional areas from pre/post-scroll screenshots.
   - Evaluates cleaned DOM structure alongside the visual analysis to recommend the extraction strategy and suggest selectors.
2. **Generation Agent** (`agent/generation_agent.py`) — **fully implemented**:
   - Generates target configurations matching the exploration recommendations.
   - Saves config to `config/targets/<brand>.json` in `HybridPromoExtractor`-compatible format.
   - Sets `state.status = "generated"` on success for unambiguous outcome logging.
3. **Validation Agent** (`agent/orchestrator.py` - stub, wired in Task 5):
   - Evaluates scraping coverage, compares output schema, and verifies security sandboxing.
4. **Registration Agent** (`agent/orchestrator.py` - stub):
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

Task 4 introduced a **Docker sandbox** that runs every generated scraper in an isolated, resource-capped container before validation. This prevents malicious or runaway AI-generated code from affecting the host machine.

### What the sandbox enforces
| Constraint | Value |
|---|---|
| Max RAM | 512 MB (OOM kill if exceeded) |
| Max CPU | 1 core |
| Max processes | 64 (prevents fork bombs) |
| Filesystem | Read-only (only `/tmp` is writable, max 64 MB) |
| Network | Outbound HTTPS to target domain only |
| Capabilities | All Linux capabilities dropped |

### Prerequisites
- **Docker Desktop** installed ([download](https://www.docker.com/products/docker-desktop/))
- Docker version ≥ 20.10

### Step 1 — Start Docker Desktop

Docker Desktop does **not** use `systemctl` like regular Docker Engine. Start it with:

```bash
systemctl --user start docker-desktop
```

> You need to run this once per login session. To check it's running: `docker info`

### Step 2 — Build the sandbox image

Run from the **repository root** (not from inside `docker/`):

```bash
docker build -f docker/Dockerfile.sandbox -t promo-scraper-sandbox:latest .
```

This takes ~10 minutes on first run (downloads Chromium). Subsequent builds use the cache and are instant.

### Step 3 — Create the egress network

The network restricts containers to only talk to the target scraping domain:

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

> **Note:** The network only needs to be created once. It persists until you explicitly remove it or restart Docker Desktop.

---

## Executing the System

### Running Sandbox Violation Tests

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

### Running Unit Tests (Routing & Graph Mocked Runs)
Execute the orchestrator unit tests to verify the routing flows based on validation scores and sandbox checks:
```bash
PYTHONPATH=. ../env/bin/python3 agent/test_orchestrator.py
```

Expected output: `🎉 All orchestrator and generation agent tests passed successfully!`

### Running the Exploration Agent (Live Dynamic Analysis)
To test the site exploration agent directly on a live retail page:
Create or run a script calling `explore_site(url, brand)`.

Example test invocation:
```bash
PYTHONPATH=. ../env/bin/python3 -c "
from agent.exploration_agent import explore_site
res = explore_site('https://www.oxfordshop.com.au/', 'Oxford Shop')
print('Strategy:', res.extraction_strategy)
print('Anti-bot risk:', res.anti_bot_risk)
"
```

The system will print token usage logs and USD costing breakdowns for both steps:
- **Visual Call**:
  `LiteLLM Vision SUCCESS: model=openai/claude-haiku-4.5 | prompt_tokens=3436 | completion_tokens=544 | total_tokens=3980 | cost=$0.006156`
- **DOM Reasoning Call**:
  `LiteLLM Reasoning SUCCESS: model=openai/claude-haiku-4.5 | prompt_tokens=19975 | completion_tokens=522 | total_tokens=20497 | cost=$0.022585`

---

## Key Files — Task 4 Sandbox

| File | Purpose |
|---|---|
| `docker/Dockerfile.sandbox` | Minimal Python 3.12 image with Playwright + all scraper dependencies |
| `docker/setup_egress_network.sh` | Creates/tears down the egress-filtered Docker network |
| `agent/sandbox_entrypoint.py` | Runs **inside** the container; reads config from env vars, executes scraper |
| `agent/sandbox_runner.py` | Host-side: creates container, enforces limits, detects violations |
| `agent/test_sandbox.py` | Tests 3 violation scenarios (filesystem / network / memory) |
| `docs/HANDOFF_TASK_4.md` | Full command reference and Task 5 API contract |

### Sandbox violation types (returned by `sandbox_runner.run_scraper_in_sandbox()`)

| Violation | Trigger | Meaning |
|---|---|---|
| `oom_kill` | Exit code 137 | Container killed by kernel — exceeded 512 MB RAM |
| `filesystem_violation` | EROFS in logs | Tried to write outside `/tmp` |
| `network_violation` | Connection error in logs | Tried to reach a blocked host |
| `nonzero_exit` | Any other non-zero exit | Generic scraper failure |
| `timeout_or_crash` | Docker API error | Container failed to start or timed out |

Any non-empty violations list → automatic rejection in the validation pipeline.
