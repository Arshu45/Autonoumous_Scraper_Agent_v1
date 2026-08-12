# agent/prompts.py

EXPLORATION_VISUAL_PROMPT = """You are analyzing a retail website screenshot to identify promotional content.

Look at this screenshot and identify:
1. All visible promotional banners, sale announcements, and discount offers
2. Whether the promotions are text-based (HTML elements) or image-based (banner images)
3. The approximate number of distinct promotional offers visible
4. Any promotional categories visible (e.g., "Women's Fashion", "Shoes", "Home")

For each promotional area you find, describe:
- Its approximate location on the page (top banner, mid-page tile, footer strip, etc.)
- Whether it appears to be an HTML text element or an image/banner
- The promotional text visible (e.g., "Up to 50% off", "Buy 2 Get 1 Free")
- The product category if identifiable

Respond in JSON format:
{
    "promotional_areas": [
        {
            "location": "top banner",
            "type": "image" | "text",
            "promo_text": "Up to 50% off selected styles",
            "category": "Women's Fashion",
            "confidence": "high" | "medium" | "low"
        }
    ],
    "total_promo_areas_found": 3,
    "dominant_promo_type": "image" | "text" | "mixed",
    "summary": "Brief description of the promotional content layout"
}"""

DOM_ANALYSIS_PROMPT = """You are analyzing the DOM HTML of a retail website to identify CSS selectors for promotional content.

Given the visual analysis summary, candidate promotional DOM elements, and DOM HTML, identify the most reliable CSS selectors that target promotional banners, sale announcements, and discount text.

Visual analysis summary: {visual_summary}

Candidate Promotional Elements (Extracted directly from DOM):
{candidate_elements}

DOM HTML (truncated to relevant sections):
{dom_html}

For each type of promotional content, provide CSS selectors that will work with
querySelector/querySelectorAll. Prefer:
- Class-based selectors over tag-based
- Partial attribute matches ([class*='promo']) for resilience to minor class name changes
- Multiple fallback selectors per area

The target scraper (HybridPromoExtractor) uses two types of selectors:
1. `text_selectors`: CSS selectors whose `.textContent` contains promotional text
2. `screenshot_selectors`: CSS selectors for elements to screenshot and send to Vision API

Respond in JSON format:
{{
    "extraction_strategy": "text" | "screenshot" | "hybrid",
    "text_selectors": ["selector1", "selector2"],
    "screenshot_selectors": ["selector1", "selector2"],
    "notes": "Explanation of selector choices and any concerns"
}}"""

CONFIG_GENERATION_PROMPT = """You are generating a scraper configuration JSON for a retail website.

Based on the site analysis below, produce a JSON config that HybridPromoExtractor can consume directly.

Site Analysis:
- URL: {url}
- Brand: {brand}
- Category Hint: {category_hint}
- Extraction Strategy: {extraction_strategy}
- Visual Summary: {visual_summary}
- Identified Promo Areas: {promo_areas}
- CSS Selectors Found: {selectors}
- Anti-Bot Risk: {anti_bot_risk}
- Notes: {notes}

Requirements from the user: {requirements}

The config JSON MUST have this exact shape:
{{
    "brand": "{brand}",
    "source_url": [
        {{
            "url": "{url}",
            "category_hint": "{category_hint}"
        }}
    ],
    "spider": "image_promo",
    "extraction_strategy": "{extraction_strategy}",
    "text_selectors": [...],
    "screenshot_selectors": [...],
    "exclude_selectors": ["nav", "footer", ".cookie-banner", ".breadcrumb", ".search", ".logo"],
    "exclude_url_patterns": ["/logo", "/icon", "/avatar", "social", "payment", "brand-logo"],
    "min_image_width": 400,
    "min_image_height": 150,
    "min_aspect_ratio": 1.2,
    "request_delay_seconds": 4,
    "scroll_depth": 3,
    "enabled": true
}}

Rules:
- source_url MUST be an array of objects, each with "url" and "category_hint" keys. Never a plain string.
- category_hint: one short sentence describing the site's product category and how to classify promotions.
  Examples: "Women's fashion and accessories. All promotions → Womens."
            "Men's and women's leather footwear. Always Footwear."
- extraction_strategy must be one of: "text", "screenshot", "image", "hybrid"
- text_selectors: CSS selectors whose textContent contains promo text
- screenshot_selectors: CSS selectors for elements to capture as screenshots for Vision API
- Use [class*='partial'] selectors for resilience to class name changes
- Provide at least 3 text_selectors and 3 screenshot_selectors
- Do NOT use jQuery-style or non-standard selectors (e.g. :contains(), :has(), :first, :last). Use only valid native CSS selectors.
- Set request_delay_seconds to 4 (default) unless the site is known to rate-limit aggressively
- Set scroll_depth to 3 by default; reduce to 2 only if the site has no lazy-loaded content

Also provide:
1. An estimated offer count (how many offers you expect the scraper to find)
2. Any notes about extraction risks or edge cases

Respond with ONLY valid JSON — no markdown fences, no commentary outside the JSON."""

CUSTOM_SCRAPER_PROMPT = """You are generating a custom Python scraper script for a non-standard retail website.

Write a custom scraper script that can extract promotions from the target website.
The script will run inside a sandbox and should populate a global `offers` list.

Site URL: {url}
Brand: {brand}
Requirements: {requirements}

Guidelines:
- Focus on extracting promotions and sale offers.
- Make the script resilient and handle extraction failures gracefully.
"""

TEST_ASSERTION_PROMPT = """You are generating test assertions for the custom scraper or config.

Based on the website's promotional structure, write test assertions that can validate
if the scraper's output matches the expected behavior.
"""
