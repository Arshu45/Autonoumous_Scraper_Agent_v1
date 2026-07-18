# Handoff Documentation: Task 2 (Exploration Agent & Anti-bot Detection)

## Accomplished Work

1. **Created Site Exploration Agent (`agent/exploration_agent.py`)**:
   - Built the complete Playwright headless site visit workflow.
   - Configured Chrome stealth settings (e.g., custom headers, user-agent, `navigator.webdriver` prototype removal) to prevent detection.
   - Implemented page load timeouts with a retry budget of 3.
   - Handled lazy loading using a scroll depth-based strategy, resetting scroll to top for screenshot accuracy.
   - Captured and stored pre-scroll (`agent_screenshots/<brand>_initial.png`) and post-scroll (`agent_screenshots/<brand>_post_scroll.png`) screenshots.

2. **Added Anti-Bot Scoring & DOM Cleaning**:
   - Implemented the `score_anti_bot_risk()` logic evaluating the Playwright response headers/status code and DOM markers.
   - Wrote a regex-based `clean_dom_regex()` function to remove non-text tags (such as `<head>`, `<script>`, `<style>`, `<svg>`, `<noscript>`) and truncate DOM structure to under 50KB to respect LLM input token limits.

3. **Unified Multimodal LiteLLM & Gemini Fallback for Vision/Reasoning**:
   - Updated the visual screenshot call (`call_exploration_vision`) to first attempt multimodal LiteLLM execution using the configured model (e.g., `openai/claude-haiku-4.5` / `VISION_LLM_MODEL`).
   - If LiteLLM fails, it automatically falls back to direct Gemini (`gemini-2.5-flash`) via the `google-genai` SDK with exponential-backoff retries.
   - Updated the DOM selector reasoning call (`call_exploration_reasoning`) to follow the exact same LiteLLM first with direct Gemini fallback behavior.
   - Fixed prompt braces inside `DOM_ANALYSIS_PROMPT` in `agent/prompts.py` to avoid format KeyError.

4. **Added Token Usage and Costing Logs**:
   - Logs precise prompt, completion, and total token usage for both the visual exploration and the reasoning steps.
   - Computes and logs USD costing details in real-time using model price rates ($1.00/1M input and $5.00/1M output for Claude; $0.075/1M input and $0.30/1M output for Gemini fallback).
   - Log example format:
     `LiteLLM Vision SUCCESS: model=openai/claude-haiku-4.5 | prompt_tokens=3436 | completion_tokens=544 | total_tokens=3980 | cost=$0.006156`

5. **Wired into LangGraph Orchestrator**:
   - Integrated `explore_site()` directly into the `exploration` node in `agent/orchestrator.py`.
   - Updated the LangGraph unit tests (`agent/test_orchestrator.py`) to mock the site exploration step, enabling fast, isolated, and cost-efficient testing of graph routing configurations.

---

## Verification Results: Oxford Shop

A trial run against the target website **Oxford Shop** (`https://www.oxfordshop.com.au/`) was successfully executed. The results are summarized below:

- **Target URL**: `https://www.oxfordshop.com.au/`
- **Computed Anti-Bot Risk**: `medium` (due to the presence of recaptcha/hcaptcha/turnstile script strings, though the page navigated and rendered perfectly).
- **Promotional Extraction Strategy**: `hybrid`
- **Visual Banners Detected**:
  - Topmost rotating announcement bar:
    - `"FREE SHIPPING ON ALL DOMESTIC ORDERS OVER $75"`
    - `"SHOP THE OUTLET FOR 50-80% OFF PAST SEASON STYLES"`
  - Main hero banner:
    - `"END OF SEASON SALE"`
  - Mid-page sections:
    - `"UP TO 70% OFF TIMELESS STYLE EXCEPTIONAL SAVINGS"`
  - Side floating tab:
    - `"$30 off*"`
- **Notes & CSS Selectors**:
  - The DOM analysis successfully identified `.announcement-bar-container`, `.announcement-message`, and `.c-announcement_bar_message` as key selector targets.
  - Initial and post-scroll screenshots were successfully saved in `agent_screenshots/`.

---

## How to Run Tests

### Running Unit Tests (Mocked)
Verify that the LangGraph orchestrator correctly routes state based on validation score recommendations and sandbox violations:
```bash
PYTHONPATH=. ../env/bin/python3 agent/test_orchestrator.py
```

### Running Real Exploration (Live API / Playwright Run)
Verify the actual Playwright, Gemini, and LiteLLM loop:
```bash
PYTHONPATH=. ../env/bin/python3 /home/arsha/.gemini/antigravity/brain/3b13e4b2-239b-4c46-ae8e-ab87b6ea7d92/scratch/test_exploration.py
```
