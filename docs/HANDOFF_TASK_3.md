# Task 3: Generation Agent Handoff Documentation

This artifact details the accomplishments for Task 3: Generation Agent, including the generated scraper configuration for Van Heusen, verification of its compatibility with the `HybridPromoExtractor`, and details of our space-invariant deduplication optimization.

## 1. Accomplishments Overview

We have successfully implemented, integrated, and validated the Generation Agent module into the Autonomous Scraping Agent. The specific milestones achieved include:

- **Module Implementation (`agent/generation_agent.py`)**:
  - Implemented `generate_scraper_config(state: AgentState) -> AgentState` node function.
  - Formatted and executed `CONFIG_GENERATION_PROMPT` using inputs extracted from `SiteAnalysis`.
  - Implemented `call_generation_llm` with the dual-client pattern: primary LiteLLM model call with auto-fallback to direct Gemini, robust retries, and comprehensive token/cost logging.
  - Parsed unstructured or nested LLM outputs into a clean target configuration JSON dictionary.
  - Created and populated the `GeneratedArtifacts` schema inside the agent state.
  - Added selector syntax validation and cleaning utility functions (`clean_selectors` & `is_valid_css_selector`) that automatically strip out non-standard jQuery pseudo-classes (like `:contains()`, `:has()`, `:first`, `:last`) that cause Playwright syntax errors.

- **Integration into LangGraph (`agent/orchestrator.py` & `agent/models.py`)**:
  - Modified `SiteAnalysis` data model to capture `text_selectors` and `screenshot_selectors` from the exploration stage.
  - Updated `exploration_agent.py` to write these discovered selectors into the `SiteAnalysis` object.
  - Replaced the stub `run_generation_agent` node inside `agent/orchestrator.py` with the functional generator agent node.
  - Configured the node to save the generated configuration directly to `config/targets/<brand_name_snake_case>.json`.

- **Validation & Testing**:
  - Updated unit tests in `agent/test_orchestrator.py` to patch the generation node in orchestrator routing tests, maintaining offline test safety.
  - Added a dedicated test case `test_generation_agent_node` to verify the JSON configuration parsing, Pydantic artifact construction, file-writing, and proper resource cleanup.
  - Ran a live end-to-end agent run against **Van Heusen** (`https://www.vanheusen.com.au/`) to produce a production-grade target configuration.
  - Tested compatibility by executing the hybrid scraper on the generated configuration, verifying that it successfully parses the site and upserts the promo data to the database.

---

## 2. Generated Van Heusen Configuration

Below is the live configuration produced by the Generation Agent for **Van Heusen** and saved in `config/targets/van_heusen.json`:

```json
{
  "brand": "Van Heusen",
  "source_url": "https://www.vanheusen.com.au/",
  "spider": "image_promo",
  "extraction_strategy": "hybrid",
  "text_selectors": [
    ".promo-header",
    "[class*='promo']",
    ".mgz-element-text span",
    ".owl-item.active .mgz-content-carouse-slide p",
    ".owl-stage .owl-item p span",
    "div[class*='mgz-element-text'] p span",
    ".mgz-carousel .mgz-content-carouse-slide"
  ],
  "screenshot_selectors": [
    ".promo-header--top",
    ".promo-header",
    ".mgz-carousel.owl-carousel",
    ".mgz-content-carouse-slide",
    ".owl-stage-outer",
    "div[class*='mgz-element-row'][class*='full_width']",
    ".mgz-parallax-inner",
    "section.react-plp-app__gender-landing-page",
    "div[class*='mgz-element-text'] p",
    ".widget.block.block-static-block"
  ],
  "min_image_width": 400,
  "min_image_height": 150,
  "min_aspect_ratio": 1.2,
  "request_delay_seconds": 4,
  "scroll_depth": 2,
  "enabled": true,
  "extraction_notes": "The site uses Magezon builder with complex nested structures and generated class names. Primary risks: (1) Carousel rotation may require waiting for active slide state; (2) Generated class IDs (e.g., 'susyduc', 'jnm73p0') are not targeted but consistent patterns like 'mgz-element', 'mgz-carousel', 'promo-header' provide resilience; (3) Terms and conditions disclaimers appear as small text with asterisks (*) - these should be captured but flagged as non-binding text; (4) The shirt promotion (3 for $119, 4 for $149) appears in multiple locations and may be deduplicated; (5) Medium anti-bot risk suggests potential for rate-limiting or dynamic content loading - scroll_depth set to 2 to capture lazy-loaded promotional sections. Expected offers: 2-3 unique promotions (Shirts bundle deal, Trousers $59, and potentially additional hero section messaging)."
}
```

---

## 3. Scraper Run & DB Verification

To verify full compatibility, we ran the scraper script using the newly generated configuration. 

### CLI Invocation
```bash
./env/bin/python scripts/run_hybrid_promo_scraper.py --target config/targets/van_heusen.json
```

### Scraper Log Output
```
18:49:57 [INFO] hybrid_promo_scraper: ============================================================
18:49:57 [INFO] hybrid_promo_scraper: Processing: Van Heusen
18:49:57 [INFO] hybrid_promo_scraper: ============================================================
18:50:00 [INFO] promo_scraper.hybrid_promo_extractor: HybridPromoExtractor ready: brand='Van Heusen', strategy='hybrid', model=openai/claude-haiku-4.5 (via litellm)
18:50:00 [INFO] promo_scraper.hybrid_promo_extractor: Starting extraction → https://www.vanheusen.com.au/ [category=uncategorized, strategy=hybrid]
18:50:03 [INFO] promo_scraper.hybrid_promo_extractor: One of the configured selectors became visible
...
19:01:09 [INFO] promo_scraper.hybrid_promo_extractor: Playwright strategy: 10 images/elements processed → 9 offers
19:01:13 [INFO] promo_scraper.hybrid_promo_extractor: Done: 9 offers from 10 images processed (0 cache hits) | cost=$0.019396
19:01:13 [INFO] hybrid_promo_scraper: Auto-created competitor 'Van Heusen' in DB
19:01:13 [INFO] hybrid_promo_scraper: Van Heusen — Inserted: 9  |  Updated: 0  |  Cost: $0.019396
```

---

## 4. Spacing-Invariant Deduplication Optimization

### Diagnosis of Duplicate Records
We observed duplicate promotions generated for rotating carousels/elements (e.g. `"Blazers $179 each*"` vs `"Blazers$179 each*"` and `"Suits From $249*"` vs `"SuitsFrom $249*"`).

1. **Root Cause**:
   - In dynamic layouts (like the owl-carousel sliders used by Van Heusen), inactive slides are hidden via CSS styles.
   - For the visible active slide, Playwright's `inner_text()` returns the styled text containing spacing (e.g. `"Blazers $179 each*"`).
   - For the hidden inactive slides, `inner_text()` returns an empty string (`""`), which triggers the scraper to fall back to `text_content()`.
   - Native `textContent` joins DOM text nodes directly without block/inline layout awareness, merging adjacent nodes without inserting spacing (e.g. producing `"Blazers$179 each*"`).
   - Since the previous deduplication key `_norm_key` preserved spaces, both variations were treated as distinct records and inserted.

2. **Resolution & Implementation**:
   - Modified `_norm_key` in `promo_scraper/hybrid_promo_extractor.py` to remove all whitespace characters during normalisation comparison.
   - Since the extractor sorts candidate texts descending by length (preferring longer strings), the correctly spaced and formatted variant (which is longer by 1 character) gets evaluated first.
   - The version lacking whitespace evaluates to the same whitespace-insensitive normalization key, matching the existing key, and is discarded.
   - Cleared the database and re-scraped, successfully verifying that only clean, correctly spaced, non-duplicated offers are written to the database.
