# HANDOFF — Task 5: Validation Agent

## What Was Built

### 1. `agent/validation_agent.py`
A fully implemented `run_validation_agent(state: AgentState) -> AgentState` function that:

- **Idempotency guard** — if `state.validation_report` is already populated (e.g. pre-wired in tests or repair runs), honours it and skips the sandbox entirely.
- **Sandbox execution** — calls `sandbox_runner.run_scraper_in_sandbox(scraper_code, config_json, timeout_seconds=60)`.
- **Violation fast-path** — any violation forces `confidence_score=0`, `recommendation="reject"`, early return.
- **Offer parsing** — parses the container's `stdout` as JSON → `{"offers": [...], "count": N}`.
- **Schema validation** — validates each offer for required `title` (non-empty) and `confidence` (`"high"/"medium"/"low"`). Optional fields (`category`, `discount_min`, `discount_max`) are allowed to be `None`.
- **Confidence scoring** — full §9b rules (see table below).
- **Sample offers** — first ≤ 3 valid offers attached to report for human preview.
- **State population** — sets `state.validation_report` and `state.status = "validation"`.

### 2. `agent/orchestrator.py` (lines 33–36 only)
Replaced the stub with:
```python
def run_validation_agent(state: AgentState) -> AgentState:
    from agent.validation_agent import run_validation_agent as _run
    return _run(state)
```

No other lines were touched.

### 3. `agent/test_validation.py`
Five standalone tests that mock `run_scraper_in_sandbox` — no Docker daemon required.

### 4. `docs/HANDOFF_TASK_5.md` (this file)

---

## How to Run Validation Tests

```bash
cd /home/arsha/Desktop/Autonomous_scraping_agent/myers_competitive_analysis

# Validation agent tests (no Docker needed)
../env/bin/python agent/test_validation.py

# Orchestrator tests (still passing after wiring)
../env/bin/python agent/test_orchestrator.py
```

Expected output for both: exit code 0, all ✓.

---

## Confidence Scoring Rules

| Component | Rule | Value |
|---|---|---|
| **Base (yield ≥ 5 or yield_rate ≥ 80%)** | `offers_extracted ≥ 5` OR `(offers / estimated) ≥ 0.8` | +70 |
| **Base (yield ≥ 1)** | At least 1 offer | +50 |
| **Base (yield = 0)** | No offers found | +10 |
| **Schema 100% valid** | All offers pass schema | +20 |
| **Schema ≥ 80% valid** | | +10 |
| **Schema < 80% valid** | | +0 |
| **Title populated (all)** | `title` non-empty on 100% of offers | +5 |
| **Category populated (≥ 80%)** | `category` not None on ≥ 80% | +5 |
| **discount_min populated (≥ 50%)** | `discount_min` not None on ≥ 50% | +5 |
| **Anti-bot: high risk** | `anti_bot_risk == "high"` | −20 |
| **Anti-bot: medium risk** | `anti_bot_risk == "medium"` | −5 |
| **Sandbox violation override** | Any violation → force score = 0 | =0 |

**Thresholds:** score ≥ 90 → `auto_approve` · 70–89 → `pending` · < 70 → `reject`

---

## `ValidationReport` — Scenarios

### Scenario 1: `test_clean_5_offers`
5 valid offers, exit 0, no violations, `anti_bot_risk="low"`, `estimated_offer_count=5`

```json
{
  "brand": "TestBrand",
  "scraper_ran": true,
  "offers_extracted": 5,
  "schema_valid": true,
  "schema_errors": [],
  "confidence_score": 105,
  "score_breakdown": {
    "yield_rate": 1.0,
    "yield_bonus_applied": true,
    "base_score": 70,
    "schema_valid_pct": 100.0,
    "schema_bonus": 20,
    "title_fully_populated": true,
    "title_bonus": 5,
    "category_pct": 1.0,
    "category_bonus": 5,
    "discount_min_pct": 1.0,
    "discount_bonus": 5,
    "anti_bot_risk": "low",
    "antibot_penalty": 0,
    "score_before_violation_check": 105,
    "violation_override": false,
    "final_score": 105
  },
  "issues": [],
  "sample_offers": ["...up to 3 offer dicts..."],
  "recommendation": "auto_approve",
  "sandbox_violations": []
}
```

### Scenario 2: `test_zero_offers`
0 offers, exit 0, no violations

```json
{
  "scraper_ran": true,
  "offers_extracted": 0,
  "schema_valid": true,
  "confidence_score": 10,
  "recommendation": "reject",
  "sandbox_violations": []
}
```

### Scenario 3: `test_sandbox_violation`
`violations=["filesystem_violation"]`

```json
{
  "scraper_ran": true,
  "offers_extracted": 0,
  "confidence_score": 0,
  "recommendation": "reject",
  "sandbox_violations": ["filesystem_violation"],
  "score_breakdown": { "violation_override": true, "final_score": 0 }
}
```

### Scenario 4: `test_high_antibot_penalty`
5 valid offers + `anti_bot_risk="high"` → score drops by exactly 20 vs low-risk baseline

```json
{
  "confidence_score": 85,
  "recommendation": "pending",
  "score_breakdown": { "antibot_penalty": -20 }
}
```
> Score 85 routes to **pending** (70–89 band).

### Scenario 5: `test_timeout_crash`
`exit_code=None`, `violations=["timeout_or_crash"]`

```json
{
  "scraper_ran": false,
  "offers_extracted": 0,
  "confidence_score": 0,
  "recommendation": "reject",
  "sandbox_violations": ["timeout_or_crash"]
}
```

---

## What Task 6 (Streamlit Approval UI) Consumes

Task 6 will read `ValidationReport` from the LangGraph state (or a persistence store). Key fields it will use:

| Field | UI purpose |
|---|---|
| `brand` | Page title / header |
| `recommendation` | Traffic-light badge: 🟢 auto_approve / 🟡 pending / 🔴 reject |
| `confidence_score` | Gauge or progress bar (0–100+) |
| `score_breakdown` | Expandable breakdown table showing each scoring term |
| `offers_extracted` | Summary count |
| `sample_offers` | Offer preview cards (title, category, discount range, confidence) |
| `schema_valid` + `schema_errors` | Validation status indicator; error list if invalid |
| `sandbox_violations` | Alert banner (hidden when empty) |
| `issues` | Warning list for soft issues (e.g. JSON parse failure) |
| `scraper_ran` | Boolean to show "Sandbox did not complete" warning |

The `recommendation` field drives the primary CTA buttons:
- **`pending`** → show **Approve** / **Reject** buttons for human review
- **`auto_approve`** → show confirmation with option to override
- **`reject`** → show reason and option to trigger repair run
