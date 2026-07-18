"""
hybrid_promo_extractor.py
=========================
Multi-strategy promotional offer extractor for retail websites.

Supports three extraction strategies (set per site in config):

  "text"       — Scrape promo text directly from HTML elements.
                 No Vision API needed. Works for sites with text-based banners.

  "screenshot" — Playwright screenshots banner *elements* from within the live
                 browser session, then sends screenshots to Gemini Vision.
                 Bypasses CDN 403s because no external image download is needed.

  "image"      — Original strategy: collect <img> src URLs, download via httpx,
                 send to Gemini Vision. Works when CDN allows external downloads.

  "hybrid"     — Runs "text" + "screenshot" together.

All strategies yield offer dicts that feed into the existing PostgresPipeline.

CHANGELOG (result-quality / reliability pass)
----------------------------------------------
- Unified LLM dispatch (_call_llm): both text and vision calls now share the
  same rate-gated, retrying, JSON-mode-forcing path. Previously only vision
  calls retried on rate limits; text (categorization) calls did not, so a
  single 429 silently zeroed out categorization for an entire run.
- JSON-mode is requested from the provider (response_mime_type /
  response_format) instead of relying purely on prompt instructions + regex
  fence-stripping. Falls back gracefully on older SDKs that reject the param.
- Categorization now runs in batches (CATEGORIZATION_BATCH_SIZE) instead of
  one giant call, so one malformed/truncated response only affects its batch.
- Text-promo detection regex broadened (BOGO, multi-buy, member pricing,
  non-USD currency symbols, coupon/code language) and made overridable per
  site via config["promo_keywords_pattern"].
- Skip reasons are now tracked per-cause (too_small, bad_aspect,
  excluded_pattern, download_failed, non_image_content, svg_skipped,
  fetch_failed, no_offers_found, unparsable_response) instead of one flat
  counter, so a zero-offer run can actually be diagnosed from the summary.
- Optional pluggable `cache` (config["cache"], duck-typed .get(key)/.set(key,
  value)) lets a persistent store (e.g. backed by PostgresPipeline) short-
  circuit repeat Vision API calls for images already seen in a prior run.
- cost_basis in the summary now reflects the model actually used
  (self.model / litellm vs direct) instead of a hardcoded "Claude Haiku 4.5"
  label that was wrong whenever a different model was configured. The
  per-token rate is a configurable estimate (VISION_COST_PER_MILLION_TOKENS_USD),
  not a billing-accurate figure — flagged as such in the output.
- Page navigation (page.goto) now retries once on timeout before giving up
  on a source entirely.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import httpx
import google.genai as genai
from google.genai import types as genai_types
from PIL import Image
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_message,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

# ── Global Vision API rate-gate ─────────────────────────────────────────────
# Shared across all HybridPromoExtractor instances in the same process.
# Works for both LiteLLM (Claude Haiku) and direct Gemini calls.
#
# VISION_API_MIN_DELAY controls the minimum gap between successive API
# dispatches (not completions). Tune this against your gateway quota:
#   - Corporate LiteLLM (Claude Haiku): set to 1.0–2.0 if gateway is generous
#   - Free Gemini tier (15 RPM):        keep at 4.5
#   - Paid Gemini tier:                 set to 0.5–1.0
_vision_api_lock = threading.Lock()
_last_vision_api_time = 0.0
VISION_API_MIN_DELAY = float(os.getenv("VISION_API_MIN_DELAY", "4.5"))

# Matches rate-limit signals from all supported providers:
#   - Generic:            429, quota, exhausted
#   - Claude / Anthropic: overloaded, rate_limit_error, AnthropicError
#   - LiteLLM gateway:    RateLimitError, litellm.RateLimitError
_RATE_LIMIT_PATTERN = (
    r".*429.*"
    r"|.*quota.*"
    r"|.*exhausted.*"
    r"|.*overloaded.*"
    r"|.*rate.?limit.*"
    r"|.*AnthropicError.*"
    r"|.*litellm\.RateLimit.*"
)


# ── Gemini Vision prompt ────────────────────────────────────────────────────
VISION_PROMPT_TEMPLATE = """You are a retail promotions analyst.
Examine the promotional banner image provided.
Extract every offer or discount visible in the image.

Return a JSON array only — no explanation, no markdown, no code fences.
Each element must have exactly these fields:
  - "promo_text" : the full offer text as it appears in the image
  - "category"   : one reporting category from this list only: {categories}, or null
  - "confidence" : "high", "medium", or "low"

If the image contains no promotional text (lifestyle photo, brand logo, product photo), return: []
Only extract text explicitly visible in the image. Do not create discount fields."""

# ── Default text-promo detection pattern ────────────────────────────────────
# Broadened beyond simple "% off / sale / deal" language to catch BOGO,
# multi-buy thresholds, member pricing, coupon/code language, and non-USD
# currency symbols. Overridable per-site via config["promo_keywords_pattern"]
# for locale-specific phrasing.
DEFAULT_PROMO_PATTERN = (
    r"\d+\s*%|\boff\b|\bsale\b|\bdeal\b|\bsave\b|\bdiscount\b|\bextra\b"
    r"|free\s+(?:[a-zA-Z]+\s+)?(?:delivery|shipping|gift|returns?)"
    r"|clearance|\boffer\b|\bpromo(?:tion)?\b|\bcode\b|\bcoupon\b|\bvoucher\b"
    r"|\bbogo\b|buy\s*\d+\s*get\s*\d+|\b\d+\s*for\s*\d+\b"
    r"|\bmembers?\b.{0,15}\b(save|only|exclusive|price|pricing)\b"
    r"|\bfor\s+[A-Za-z]{0,3}[\$£€¥₹]\d|\bfrom\s+[A-Za-z]{0,3}[\$£€¥₹]\d"
    r"|[\$£€¥₹]\d+\s*\*|[\$£€¥₹]\d+\s*each"
)

# ── Common browser setup ────────────────────────────────────────────────────
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class HybridPromoExtractor:
    """
    Extracts promotional offers from a single retail site.

    Config keys:
        brand, source_url, extraction_strategy ("text"|"screenshot"|"image"|"hybrid")
        text_selectors      list[str]  — CSS selectors for text extraction
        screenshot_selectors list[str] — CSS selectors for element screenshots
        banner_selectors    list[str]  — CSS selectors for <img> src URLs (image strategy)
        min_image_width     int        — filter small images (default 400)
        min_image_height    int        — filter thin strips (default 150)
        min_aspect_ratio    float      — filter non-banner shapes (default 1.2)
        exclude_url_patterns list[str] — URL substrings to skip
        request_delay_seconds int      — delay between Gemini calls (default 4)
        scroll_depth        int        — scroll iterations to trigger lazy load (default 3)
        promo_keywords_pattern str     — override for text-promo detection regex
        cache                object   — optional persistent cache with .get(key)
                                         / .set(key, value) methods, keyed by
                                         image content hash, used to skip
                                         repeat Vision API calls across runs
        nav_retry_attempts   int       — retries for page.goto on timeout (default 2)
    """

    DEFAULT_INPUT_USD_PER_MILLION_TOKENS = 1.00
    IMAGE_TOKEN_PIXELS_PER_TOKEN = 800
    IMAGE_TOKEN_FIXED_OVERHEAD = 170
    TEXT_TOKEN_CHARS_PER_TOKEN = 4
    GEMINI_MODEL = "gemini-2.5-flash"
    CATEGORIZATION_BATCH_SIZE = 25

    def __init__(self, target_config: dict):
        self.cfg        = target_config
        self.brand      = target_config["brand"]
        # Can be a single URL string, a list of strings, or a list of
        # {"url": ..., "category": ...} objects for report context.
        raw_sources = target_config["source_url"]
        if not isinstance(raw_sources, list):
            raw_sources = [raw_sources]

        self.source_entries = []
        for entry in raw_sources:
            if isinstance(entry, dict):
                url = entry.get("url") or entry.get("source_url")
                category = entry.get("category") or entry.get("business_category")
            else:
                url = entry
                category = target_config.get("category")
            if url:
                self.source_entries.append({"url": url, "category": category})

        self.source_urls = [entry["url"] for entry in self.source_entries]
        self.source_url = self.source_urls[0] if self.source_urls else ""
        self.current_category = self.source_entries[0].get("category") if self.source_entries else target_config.get("category")
        self.strategy   = target_config.get("extraction_strategy", "image")
        self.allowed_categories = self._load_allowed_categories()
        self.category_list_text = ", ".join(self.allowed_categories)
        self.vision_prompt = VISION_PROMPT_TEMPLATE.format(categories=self.category_list_text)

        # Image filter thresholds
        self.min_width  = target_config.get("min_image_width",  400)
        self.min_height = target_config.get("min_image_height", 150)
        self.min_aspect = target_config.get("min_aspect_ratio", 1.2)
        self.exclude_patterns = target_config.get("exclude_url_patterns", [
            "/logo", "/icon", "/avatar", "social", "payment", "brand-logo",
        ])
        self.delay        = target_config.get("request_delay_seconds", 4)
        self.scroll_depth = target_config.get("scroll_depth", 3)
        self.nav_retry_attempts = target_config.get("nav_retry_attempts", 2)

        # Text-promo detection, overridable per site for locale-specific copy
        self.promo_text_re = re.compile(
            target_config.get("promo_keywords_pattern", DEFAULT_PROMO_PATTERN), re.I
        )

        # Optional persistent cache (duck-typed .get(key) / .set(key, value)),
        # keyed by image content hash, to avoid re-billing Vision API calls
        # for banners already seen in a prior run. None = no caching (default,
        # unchanged behavior).
        self.cache = target_config.get("cache")

        # Cost estimate rate — configurable since it varies a lot by
        # provider/model and this is only ever an order-of-magnitude figure.
        self.input_usd_per_million_tokens = float(
            os.getenv("VISION_COST_PER_MILLION_TOKENS_USD", self.DEFAULT_INPUT_USD_PER_MILLION_TOKENS)
        )

        # Counters
        self._images_found     = 0
        self._images_processed = 0
        self._images_skipped   = 0
        self._offers_extracted = 0
        self._gemini_api_calls = 0
        self._image_api_calls  = 0
        self._text_api_calls   = 0
        self._cache_hits       = 0
        self._estimated_cost_usd = 0.0
        # Per-cause skip tracking so a zero-offer run can be diagnosed from
        # the summary instead of requiring a log dive.
        self._skip_reasons: dict[str, int] = {}

        # Select client type (litellm or direct gemini)
        self.use_litellm = bool(os.getenv("LITELLM_API_BASE"))
        if self.use_litellm:
            model_name = os.getenv("VISION_LLM_MODEL") or os.getenv("LLM_MODEL")
            if not model_name:
                raise EnvironmentError("VISION_LLM_MODEL or LLM_MODEL must be set in .env when using LiteLLM.")
            if "/" not in model_name:
                self.model = f"openai/{model_name}"
            else:
                self.model = model_name
        else:
            self.model = os.getenv("VISION_LLM_MODEL") or self.GEMINI_MODEL

        # Init API client (only needed for screenshot / image strategies)
        if self.strategy in ("screenshot", "image", "hybrid"):
            if self.use_litellm:
                import litellm
                litellm.suppress_debug_info = True
                self._client = litellm
            else:
                api_key = os.getenv("GEMINI_API_KEY")
                if not api_key:
                    raise EnvironmentError(
                        "GEMINI_API_KEY is not set. Add it to .env.\n"
                        "Free key: https://aistudio.google.com/app/apikey"
                    )
                self._client = genai.Client(api_key=api_key)
        else:
            self._client = None

        logger.info(
            "HybridPromoExtractor ready: brand='%s', strategy='%s', model=%s (via %s)%s",
            self.brand, self.strategy, self.model, "litellm" if self.use_litellm else "direct gemini",
            ", cache=enabled" if self.cache is not None else "",
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Public entry point
    # ═══════════════════════════════════════════════════════════════════════

    def run(self) -> dict[str, Any]:
        """Run the extraction pipeline. Returns a summary dict."""
        all_offer_items: list[dict] = []

        for source_entry in self.source_entries:
            self.source_url = source_entry["url"]
            self.current_category = source_entry.get("category") or self.cfg.get("category")
            logger.info("Starting extraction → %s [category=%s, strategy=%s]", self.source_url, self.current_category or "uncategorized", self.strategy)

            offer_items: list[dict] = []

            if self.strategy in ("text", "screenshot", "hybrid"):
                offer_items = self._run_playwright_extraction()
            elif self.strategy == "image":
                offer_items = self._run_image()
            else:
                logger.error("Unknown strategy '%s' — skipping", self.strategy)
                continue

            all_offer_items.extend(offer_items)

        # Deduplicate extracted offers within this run to ensure clean reporting
        seen_keys = set()
        deduped_items = []
        for item in all_offer_items:
            title_clean = (item.get("title") or "").strip()
            item["title"] = title_clean
            key = (item.get("source"), item.get("brand"), item.get("source_url"), title_clean.lower())
            if key not in seen_keys:
                seen_keys.add(key)
                deduped_items.append(item)
        all_offer_items = self._categorize_offer_items(deduped_items)

        self._offers_extracted = len(all_offer_items)
        summary = {
            "brand":             self.brand,
            "strategy":          self.strategy,
            "images_found":      self._images_found,
            "images_processed":  self._images_processed,
            "images_skipped":    self._images_skipped,
            "skip_reasons":      dict(self._skip_reasons),
            "cache_hits":        self._cache_hits,
            "offers_extracted":  self._offers_extracted,
            "offers_stored":     0,
            "gemini_api_calls":  self._gemini_api_calls,
            "image_api_calls":   self._image_api_calls,
            "text_api_calls":    self._text_api_calls,
            "estimated_cost_usd": round(self._estimated_cost_usd, 6),
            "cost_basis": self._cost_basis_label(),
            "offer_items":       all_offer_items,
        }
        logger.info(
            "Done: %d offers from %d images processed (%d cache hits) | cost=$%s",
            self._offers_extracted, self._images_processed, self._cache_hits, summary["estimated_cost_usd"],
        )
        return summary

    def _cost_basis_label(self) -> str:
        route = "LiteLLM gateway" if self.use_litellm else "direct API"
        return (
            f"Approximate input-token cost for model='{self.model}' (via {route}) "
            f"at ${self.input_usd_per_million_tokens:.2f}/1M input tokens "
            "(override with env VISION_COST_PER_MILLION_TOKENS_USD). Image tokens "
            "estimated from resized pixel dimensions — this is an order-of-magnitude "
            "estimate, not a billing-accurate figure for every provider."
        )

    def _record_skip(self, reason: str) -> None:
        self._images_skipped += 1
        self._skip_reasons[reason] = self._skip_reasons.get(reason, 0) + 1

    def _create_stealth_page(self, pw) -> tuple[Any, Any]:
        """Launch browser and context with stealth settings to bypass anti-bot screens."""
        browser = pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1440,900",
            ]
        )
        context = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "accept-language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )
        page = context.new_page()
        page.add_init_script("delete navigator.__proto__.webdriver;")
        return browser, page

    def _goto_with_retry(self, page, url: str) -> None:
        """
        Navigate with a small retry budget on timeout. A transient bot-check
        or slow first paint previously meant a single failed page.goto killed
        the entire source with zero offers; this gives it one more shot.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        last_exc: Exception | None = None
        for attempt in range(1, self.nav_retry_attempts + 1):
            try:
                page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                return
            except PWTimeout as e:
                last_exc = e
                logger.warning("Navigation timeout (attempt %d/%d) for %s", attempt, self.nav_retry_attempts, url)
                if attempt < self.nav_retry_attempts:
                    page.wait_for_timeout(3_000)
        if last_exc:
            raise last_exc

    @staticmethod
    def _norm_key(s: str) -> str:
        # Strip punctuation/separators and all whitespace so near-identical strings
        # (with/without spaces, desktop vs. mobile-nav copies, or Gemini OCR variance)
        # dedupe as one offer.
        return re.sub(r"[^\w%$]", "", s).lower().strip()

    def _run_playwright_extraction(self) -> list[dict]:
        """
        Unified Playwright extraction flow for text and screenshot/hybrid strategies.
        Uses stealth parameters to bypass Cloudflare/Akamai bot detection.
        """
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        offer_items: list[dict] = []
        seen_hashes: set[str] = set()

        with sync_playwright() as pw:
            browser, page = self._create_stealth_page(pw)
            try:
                self._goto_with_retry(page, self.source_url)
                self._wait_and_scroll(page)

                # 1. Text Extraction Strategy
                if self.strategy in ("text", "hybrid"):
                    text_selectors = self.cfg.get("text_selectors", [])
                    candidate_texts: list[str] = []
                    seen_norms: set[str] = set()
                    _norm = self._norm_key

                    for frame in page.frames:
                        for selector in text_selectors:
                            try:
                                elements = frame.query_selector_all(selector)
                            except Exception:
                                continue
                            for el in elements:
                                try:
                                    # Prefer inner_text() — it applies normal line-break-to-space
                                    # conversion for laid-out (visible) elements. But many
                                    # carousels (owl-carousel, custom sliders, this site's
                                    # "keyline" promo strip, etc.) only render one slide at a
                                    # time and hide the rest via CSS, and the hiding scheme/
                                    # class naming varies too much across sites to reliably
                                    # detect by keyword — so fall back to text_content() for
                                    # any element inner_text() can't read, rather than
                                    # skipping it and silently losing real, distinct rotating
                                    # promos (as happened with this site's 5-slide carousel,
                                    # where only the currently-active slide was ever captured).
                                    text = el.inner_text().strip()
                                    if not text:
                                        text = el.text_content().strip()
                                except Exception:
                                    try:
                                        text = el.text_content().strip()
                                    except Exception:
                                        continue

                                # Deduplicate & filter noise
                                text = re.sub(r"\s+", " ", text)
                                if not text or len(text) < 8:
                                    continue
                                norm = _norm(text)
                                if not norm or norm in seen_norms:
                                    continue

                                # Skip leaked <style>/CSS content some themes render as
                                # visible text (e.g. scoped block-width rules)
                                if re.search(r"[.#]?[\w-]+\s*\{[^{}]*:[^{}]*\}", text):
                                    continue

                                seen_norms.add(norm)

                                # Only keep text that looks promotional. Pattern is
                                # broadened (BOGO, multi-buy, member pricing, coupon
                                # language, non-USD currencies) and overridable per
                                # site via config["promo_keywords_pattern"].
                                if not self.promo_text_re.search(text):
                                    continue

                                logger.info("  TEXT  %-60s [%s]", text[:60], selector)
                                candidate_texts.append(text)

                    # Overlapping/nested selectors (e.g. a broad container matched
                    # alongside its own child) can yield several near-duplicate
                    # strings that are prefixes/substrings of one another — keep
                    # only the longest version of each (compared on normalized form
                    # so punctuation differences don't defeat the check).
                    candidate_texts.sort(key=len, reverse=True)
                    kept_texts: list[str] = []
                    kept_norms: list[str] = []
                    for text in candidate_texts:
                        norm = _norm(text)
                        if any(norm in longer_norm for longer_norm in kept_norms):
                            continue
                        kept_texts.append(text)
                        kept_norms.append(norm)

                    for text in kept_texts:
                        offer_items.append(self._make_text_offer(text))

                # 2. Screenshot/Vision Strategy
                if self.strategy in ("screenshot", "hybrid"):
                    screenshot_selectors = self.cfg.get("screenshot_selectors", [])
                    if not screenshot_selectors:
                        # Generic default selectors to locate promos/banners
                        screenshot_selectors = [
                            "[class*='Hero']", "[class*='hero']",
                            "[class*='Banner']", "[class*='banner']",
                            "[class*='Editorial']", "[class*='editorial']",
                            "[class*='Promo']", "[class*='promo']",
                            "[class*='Campaign']", "[class*='campaign']",
                            ".discover-more", ".discover-more-content", ".dm-sale",
                            "img"
                        ]

                    for frame in page.frames:
                        for selector in screenshot_selectors:
                            try:
                                elements = frame.query_selector_all(selector)
                            except Exception:
                                continue
                            logger.debug("Selector '%s' → %d elements", selector, len(elements))

                            for el in elements:
                                # Handle IMG tags (both visible and hidden)
                                is_img = False
                                img_src = ""
                                try:
                                    if el.evaluate("el => el.tagName") == "IMG":
                                        is_img = True
                                        img_src = el.get_attribute("src") or el.get_attribute("data-src") or ""
                                        if img_src.startswith("//"):
                                            img_src = "https:" + img_src
                                        elif img_src.startswith("/"):
                                            from urllib.parse import urlparse
                                            parsed = urlparse(self.source_url)
                                            img_src = f"{parsed.scheme}://{parsed.netloc}{img_src}"
                                except Exception:
                                    pass

                                if is_img and img_src:
                                    if any(p in img_src.lower() for p in self.exclude_patterns):
                                        self._record_skip("excluded_pattern")
                                        continue

                                # Visibility check
                                box = None
                                try:
                                    box = el.bounding_box()
                                except Exception:
                                    pass

                                ss_bytes = None
                                if is_img and img_src:
                                    # For IMG tags, if visible we can screenshot; if hidden we fetch bytes directly
                                    if box and box['width'] >= 100 and box['height'] >= 50:
                                        try:
                                            ss_bytes = el.screenshot()
                                        except Exception:
                                            pass

                                    # If screenshot failed or element is hidden, fetch via frame API to bypass CORS/403
                                    if not ss_bytes:
                                        try:
                                            logger.info("Fetching hidden/uncaptured image bytes for: %s", img_src[:80])
                                            image_b64 = frame.evaluate("""async (url) => {
                                                const response = await fetch(url);
                                                const blob = await response.blob();
                                                return new Promise((resolve, reject) => {
                                                    const reader = new FileReader();
                                                    reader.onloadend = () => resolve(reader.result.split(',')[1]);
                                                    reader.onerror = reject;
                                                    reader.readAsDataURL(blob);
                                                });
                                            }""", img_src)
                                            import base64
                                            ss_bytes = base64.b64decode(image_b64)
                                        except Exception as e:
                                            logger.debug("Failed to fetch image bytes via frame fetch: %s", e)
                                            self._record_skip("fetch_failed")
                                            continue
                                else:
                                    # For non-img elements (containers, divs), they MUST be visible
                                    if not box or box['width'] < 100 or box['height'] < 50:
                                        continue
                                    try:
                                        ss_bytes = el.screenshot()
                                    except Exception:
                                        self._record_skip("fetch_failed")
                                        continue

                                if not ss_bytes:
                                    continue

                                # Deduplicate identical screenshots
                                ss_hash = hashlib.sha256(ss_bytes).hexdigest()
                                if ss_hash in seen_hashes:
                                    continue
                                seen_hashes.add(ss_hash)

                                # Check dimensions
                                try:
                                    img = Image.open(BytesIO(ss_bytes))
                                    if img.width < self.min_width or img.height < self.min_height:
                                        self._record_skip("too_small")
                                        continue
                                    if img.width > 0 and (img.width / max(img.height, 1)) < self.min_aspect:
                                        self._record_skip("bad_aspect")
                                        continue
                                except Exception:
                                    self._record_skip("unreadable_image")
                                    continue

                                self._images_found += 1
                                label = f"screenshot:{selector}"

                                if self._gemini_api_calls > 0:
                                    time.sleep(self.delay)

                                offers = self._vision_extract_cached(ss_bytes, "image/png", label, content_hash=ss_hash)
                                if offers is None:
                                    self._record_skip("unparsable_response")
                                elif offers:
                                    items = self._build_offer_items(offers, label)
                                    offer_items.extend(items)
                                    self._images_processed += 1
                                    logger.info("  SHOT  %d offers from selector '%s'", len(items), selector)
                                else:
                                    self._record_skip("no_offers_found")

            except PWTimeout:
                logger.error("Playwright timed out loading %s", self.source_url)
            except Exception as e:
                logger.error("Playwright extraction error: %s", e)
            finally:
                browser.close()

        # Vision (Gemini OCR) output isn't perfectly deterministic — the same
        # banner can yield "...purchase." on one call and "...purchase" (no
        # trailing period) on another. Apply the same normalized-text dedup
        # used for scraped text here too, across both text and image offers.
        deduped_items: list[dict] = []
        seen_final_norms: set[str] = set()
        for item in offer_items:
            norm = self._norm_key(item.get("title", ""))
            if norm and norm in seen_final_norms:
                continue
            if norm:
                seen_final_norms.add(norm)
            deduped_items.append(item)
        offer_items = deduped_items

        logger.info("Playwright strategy: %d images/elements processed → %d offers",
                    self._images_processed, len(offer_items))
        return offer_items


    # ═══════════════════════════════════════════════════════════════════════
    # Strategy 3: IMAGE — download <img> src URLs, send to Gemini Vision
    # ═══════════════════════════════════════════════════════════════════════

    def _run_image(self) -> list[dict]:
        """Original strategy: collect img src URLs → download → Gemini Vision."""
        image_urls = self._collect_image_urls()
        self._images_found = len(image_urls)
        logger.info("Image strategy: %d candidate URLs", self._images_found)

        offer_items: list[dict] = []

        for url in image_urls:
            if self._gemini_api_calls > 0:
                time.sleep(self.delay)

            img_bytes, mime = self._download_image(url)
            if img_bytes is None:
                continue  # _download_image already recorded the specific skip reason

            content_hash = hashlib.sha256(img_bytes).hexdigest()
            offers = self._vision_extract_cached(img_bytes, mime, url, content_hash=content_hash)
            if offers is None:
                self._record_skip("unparsable_response")
            elif offers:
                offer_items.extend(self._build_offer_items(offers, url))
                self._images_processed += 1
                logger.info("  IMG  %d offers from %s", len(offers), url)
            else:
                self._record_skip("no_offers_found")

        return offer_items

    # ═══════════════════════════════════════════════════════════════════════
    # Shared Playwright helpers
    # ═══════════════════════════════════════════════════════════════════════

    def _wait_and_scroll(self, page) -> None:
        """Wait for any banner selector to become visible, then scroll to trigger lazy loads."""
        from playwright.sync_api import TimeoutError as PWTimeout

        all_selectors = (
            self.cfg.get("screenshot_selectors", [])
            + self.cfg.get("text_selectors", [])
            + self.cfg.get("banner_selectors", [])
        )

        valid_selectors = [s for s in all_selectors if s.strip()]
        if valid_selectors:
            combined = ", ".join(valid_selectors)
            try:
                page.wait_for_selector(combined, state="visible", timeout=15_000)
                logger.info("One of the configured selectors became visible")
            except PWTimeout:
                logger.warning("None of the configured selectors became visible within 15s")
                page.wait_for_timeout(5_000)
        else:
            page.wait_for_timeout(5_000)

        for i in range(self.scroll_depth):
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(2_000)

        # Scroll back to top to ensure screenshot visibility
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1_000)

    def _collect_image_urls(self) -> list[str]:
        """Playwright: collect filtered <img> src URLs from the rendered DOM."""
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        from urllib.parse import urlparse

        urls: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(
                user_agent=_UA, viewport={"width": 1920, "height": 1080}
            ).new_page()
            try:
                self._goto_with_retry(page, self.source_url)
                self._wait_and_scroll(page)

                imgs = page.query_selector_all("img")
                logger.info("Total img tags: %d", len(imgs))

                parsed_base = urlparse(self.source_url)

                for img in imgs:
                    src = img.get_attribute("src") or img.get_attribute("data-src") or ""
                    if not src or src.startswith("data:"):
                        continue
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        src = f"{parsed_base.scheme}://{parsed_base.netloc}{src}"

                    if any(p in src.lower() for p in self.exclude_patterns):
                        self._record_skip("excluded_pattern")
                        continue

                    try:
                        w = img.evaluate("el => el.naturalWidth")
                        h = img.evaluate("el => el.naturalHeight")
                        if w and h:
                            if w < self.min_width or h < self.min_height:
                                self._record_skip("too_small")
                                continue
                            if (w / max(h, 1)) < self.min_aspect:
                                self._record_skip("bad_aspect")
                                continue
                    except Exception:
                        pass

                    urls.append(src)

            except PWTimeout:
                logger.error("Playwright timed out on %s", self.source_url)
            except Exception as e:
                logger.error("collect_image_urls error: %s", e)
            finally:
                browser.close()

        seen: set[str] = set()
        return [u for u in urls if not (u in seen or seen.add(u))]  # type: ignore

    # ═══════════════════════════════════════════════════════════════════════
    # Category configuration
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _load_allowed_categories() -> list[str]:
        raw_categories = os.getenv("PROMO_CATEGORIES")
        if not raw_categories:
            raise EnvironmentError(
                "PROMO_CATEGORIES must be set in .env as a comma-separated category list."
            )

        categories = [category.strip() for category in raw_categories.split(",") if category.strip()]
        if not categories:
            raise EnvironmentError("Promotion category list is empty. Check PROMO_CATEGORIES in .env.")

        return categories

    # ═══════════════════════════════════════════════════════════════════════
    # Cost estimation
    # ═══════════════════════════════════════════════════════════════════════

    def _input_token_cost(self, token_count: float) -> float:
        return (token_count / 1_000_000) * self.input_usd_per_million_tokens

    def _estimate_text_tokens(self, text: str) -> int:
        return max(1, int(len(text or "") / self.TEXT_TOKEN_CHARS_PER_TOKEN))

    def _estimate_image_tokens(self, width: int, height: int) -> int:
        visual_tokens = (width * height) / self.IMAGE_TOKEN_PIXELS_PER_TOKEN
        return int(visual_tokens + self.IMAGE_TOKEN_FIXED_OVERHEAD)

    def _estimate_image_input_cost(self, width: int, height: int, prompt: str) -> float:
        tokens = self._estimate_image_tokens(width, height) + self._estimate_text_tokens(prompt)
        return self._input_token_cost(tokens)

    def _estimate_text_input_cost(self, prompt: str) -> float:
        return self._input_token_cost(self._estimate_text_tokens(prompt))

    # ═══════════════════════════════════════════════════════════════════════
    # Unified LLM dispatch (text + vision, direct Gemini or LiteLLM gateway)
    # ═══════════════════════════════════════════════════════════════════════

    def _apply_rate_gate(self) -> None:
        """
        Enforce the minimum delay between dispatches (not completions).

        The global _vision_api_lock protects ONLY the timestamp read/write
        (a microsecond operation); the actual API call runs OUTSIDE the lock
        so other threads aren't blocked on network latency. The timestamp is
        written BEFORE the lock is released so a burst of threads can't all
        read a stale timestamp and fire simultaneously the moment the delay
        expires.
        """
        global _last_vision_api_time
        with _vision_api_lock:
            now = time.time()
            elapsed = now - _last_vision_api_time
            if elapsed < VISION_API_MIN_DELAY:
                sleep_time = VISION_API_MIN_DELAY - elapsed
                logger.debug("Vision API rate gate: sleeping %.2fs", sleep_time)
                time.sleep(sleep_time)
            _last_vision_api_time = time.time()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=5, max=70),
        retry=retry_if_exception_message(match=_RATE_LIMIT_PATTERN),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _call_llm(
        self,
        *,
        text_prompt: str,
        image_bytes: bytes | None = None,
        mime: str | None = None,
        json_mode: bool = True,
        cost_usd: float = 0.0,
    ) -> str:
        """
        Unified, rate-gated, retrying dispatch to the configured LLM — used
        for both text-only calls (categorization) and vision calls (offer
        extraction). Previously these were two separate methods and only the
        vision path retried on rate limits, so a 429 on the categorization
        call silently zeroed out categorization for the whole run. Now both
        share the same retry/backoff behavior.

        json_mode requests structured JSON output from the provider directly
        (response_mime_type for direct Gemini, response_format for
        OpenAI-compatible LiteLLM routes) rather than relying solely on the
        prompt instructing "no markdown, no fences" — this materially cuts
        down on malformed/truncated responses. Falls back to an unstructured
        call if the installed SDK version rejects the parameter.
        """
        self._apply_rate_gate()

        if self.use_litellm:
            if image_bytes is not None:
                import base64
                b64 = base64.b64encode(image_bytes).decode("utf-8")
                content = [
                    {"type": "text", "text": text_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]
            else:
                content = text_prompt
            kwargs = {
                "model": self.model,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0.0,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            api_key = os.getenv("LITELLM_API_KEY")
            api_base = os.getenv("LITELLM_API_BASE")
            if api_key:
                kwargs["api_key"] = api_key
            if api_base:
                kwargs["api_base"] = api_base
            try:
                response = self._client.completion(**kwargs)
            except TypeError:
                # Some LiteLLM-routed providers reject response_format outright.
                kwargs.pop("response_format", None)
                response = self._client.completion(**kwargs)
            reply = response.choices[0].message.content
        else:
            contents: list = [text_prompt]
            if image_bytes is not None:
                contents.append(genai_types.Part.from_bytes(data=image_bytes, mime_type=mime))
            try:
                gen_config = genai_types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json" if json_mode else None,
                )
                response = self._client.models.generate_content(
                    model=self.model, contents=contents, config=gen_config,
                )
            except TypeError:
                # Older google-genai SDK versions may not support this config
                # shape — degrade to an unstructured call rather than failing.
                response = self._client.models.generate_content(model=self.model, contents=contents)
            reply = response.text

        self._gemini_api_calls += 1
        if image_bytes is not None:
            self._image_api_calls += 1
        else:
            self._text_api_calls += 1
        self._estimated_cost_usd += cost_usd
        return reply

    @staticmethod
    def _parse_json_array(raw: str) -> list | None:
        """
        Parse a JSON array out of a model response, tolerating the odd cases
        JSON-mode doesn't fully eliminate: stray code fences, a wrapper
        object like {"offers": [...]} instead of a bare array, or trailing
        commentary around the JSON. Returns None (not []) when nothing usable
        could be parsed, so callers can distinguish "genuinely no offers"
        from "the response was unusable" for skip-reason tracking.
        """
        raw = re.sub(r"```(?:json)?|```", "", raw or "").strip()
        if not raw:
            return None

        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", raw, re.S)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    parsed = None

        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("offers", "items", "results", "data"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # Category classification
    # ═══════════════════════════════════════════════════════════════════════

    def _categorize_offer_items(self, offer_items: list[dict]) -> list[dict]:
        """
        Assign reporting categories to extracted offers, in batches. Batching
        (rather than one call for the whole run) means a truncated/malformed
        response only costs you the categories for that batch — the rest of
        the run's offers keep their vision-provided category as a fallback
        instead of the whole run silently losing categorization.
        """
        if not offer_items or not self._client:
            return offer_items

        for start in range(0, len(offer_items), self.CATEGORIZATION_BATCH_SIZE):
            chunk = offer_items[start:start + self.CATEGORIZATION_BATCH_SIZE]
            self._categorize_chunk(chunk)

        return offer_items

    def _categorize_chunk(self, chunk: list[dict]) -> None:
        allowed = set(self.allowed_categories)
        records = [
            {
                "id": idx,
                "brand": item.get("brand"),
                "source_url": item.get("source_url"),
                "promo_text": item.get("title"),
                "current_category": item.get("category"),
            }
            for idx, item in enumerate(chunk)
        ]
        prompt = (
            "You categorize retail promotions for a weekly competitor matrix.\n"
            f"Assign exactly one category from this list only: {self.category_list_text}.\n"
            "Use the promo text, brand, and source_url. If the source_url is a department page such as /men/ or /kids/, use that as strong evidence.\n"
            "Return JSON only, as an array of objects with exactly: id, category.\n"
            "Do not add explanations or markdown.\n\n"
            f"Promotions:\n{json.dumps(records, ensure_ascii=False)}"
        )

        try:
            raw = self._call_llm(
                text_prompt=prompt, json_mode=True,
                cost_usd=self._estimate_text_input_cost(prompt),
            )
            rows = self._parse_json_array(raw)
        except Exception as e:
            logger.warning("Category classification failed for a batch of %d offers; keeping existing categories: %s", len(chunk), e)
            return

        if rows is None:
            logger.warning("Category classification returned unparsable JSON for a batch of %d offers; keeping existing categories", len(chunk))
            return

        by_id = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            category = row.get("category")
            if category in allowed:
                by_id[idx] = category

        if len(by_id) < len(chunk):
            logger.info("Category classification matched %d/%d offers in batch", len(by_id), len(chunk))

        for idx, item in enumerate(chunk):
            if idx in by_id:
                item["category"] = by_id[idx]

    # ═══════════════════════════════════════════════════════════════════════
    # Gemini Vision call (shared by image + screenshot strategies)
    # ═══════════════════════════════════════════════════════════════════════

    def _vision_extract_cached(self, img_bytes: bytes, mime: str, label: str, content_hash: str | None = None) -> list[dict] | None:
        """
        Vision extraction with an optional persistent-cache short-circuit.
        If config["cache"] was supplied, identical images (by content hash,
        scoped per brand) seen in a previous run skip the Vision API call
        entirely. Returns None (not []) when the model response couldn't be
        parsed, so callers can tell "no offers" apart from "unusable response".
        """
        cache_key = None
        if self.cache is not None:
            digest = content_hash or hashlib.sha256(img_bytes).hexdigest()
            cache_key = f"{self.brand}:{digest}"
            try:
                cached = self.cache.get(cache_key)
            except Exception as e:
                logger.debug("Cache get failed for %s: %s", cache_key, e)
                cached = None
            if cached is not None:
                self._cache_hits += 1
                logger.debug("Vision cache hit for %s", label)
                return cached

        offers = self._vision_extract(img_bytes, mime, label)

        if self.cache is not None and cache_key is not None and offers is not None:
            try:
                self.cache.set(cache_key, offers)
            except Exception as e:
                logger.debug("Cache set failed for %s: %s", cache_key, e)

        return offers

    def _vision_extract(self, img_bytes: bytes, mime: str, label: str) -> list[dict] | None:
        """
        Resize image, call the vision LLM, parse JSON. Returns a list of
        offer dicts (possibly empty — genuinely no offers found), or None if
        the response couldn't be parsed at all.
        """
        try:
            img = Image.open(BytesIO(img_bytes))
            if img.width > 1600:
                ratio = 1600 / img.width
                img = img.resize((1600, int(img.height * ratio)), Image.LANCZOS)
            buf = BytesIO()
            fmt = "PNG" if mime == "image/png" else "JPEG"
            img.save(buf, format=fmt)
            cost_usd = self._estimate_image_input_cost(img.width, img.height, self.vision_prompt)
            raw = self._call_llm(
                text_prompt=self.vision_prompt,
                image_bytes=buf.getvalue(),
                mime=f"image/{fmt.lower()}",
                json_mode=True,
                cost_usd=cost_usd,
            )
        except Exception as e:
            logger.error("Vision API call failed for %s: %s", label, e)
            return None

        offers = self._parse_json_array(raw)
        if offers is None:
            logger.warning("Malformed JSON from vision model for %s — raw: %s", label, (raw or "")[:200])
            stripped = re.sub(r"```(?:json)?|```", "", raw or "").strip()
            if stripped and stripped != "[]":
                return [{"promo_text": stripped[:400], "category": None,
                         "discount_min": None, "discount_max": None, "confidence": "low"}]
            return None
        return offers

    # ═══════════════════════════════════════════════════════════════════════
    # Image download (image strategy only)
    # ═══════════════════════════════════════════════════════════════════════

    def _download_image(self, url: str) -> tuple[bytes | None, str]:
        """Download image bytes. Returns (bytes, mime) or (None, '')."""
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                r = client.get(url, headers={"User-Agent": _UA, "Referer": self.source_url})
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "image" not in ct:
                logger.warning("Non-image content-type '%s' for %s", ct, url)
                self._record_skip("non_image_content")
                return None, ""
            if "svg" in ct:
                logger.debug("Skipping SVG: %s", url)
                self._record_skip("svg_skipped")
                return None, ""
            return r.content, ct.split(";")[0].strip()
        except Exception as e:
            logger.warning("Download failed %s: %s", url, e)
            self._record_skip("download_failed")
            return None, ""

    # ═══════════════════════════════════════════════════════════════════════
    # Item builders
    # ═══════════════════════════════════════════════════════════════════════

    def _make_text_offer(self, text: str) -> dict:
        """Build an offer dict from scraped text. discount_* parsed by regex."""
        numbers = re.findall(r"(\d+)\s*%", text)
        nums    = [int(n) for n in numbers if 1 <= int(n) <= 99]
        return {
            "source":       "text_scraper",
            "brand":        self.brand,
            "source_url":   self.source_url,
            "title":        text[:200],
            "category":     None,
            "discount_min": min(nums) if len(nums) >= 2 else (nums[0] if nums else None),
            "discount_max": max(nums) if len(nums) >= 2 else None,
            "confidence":   "high",
            "scraped_at":   datetime.now(timezone.utc).isoformat(),
        }

    def _build_offer_items(self, offers: list[dict], source_url: str) -> list[dict]:
        """Convert vision-model JSON offers into offer dicts."""
        items = []
        for offer in offers:
            text = (offer.get("promo_text") or "").strip()
            if not text:
                continue

            # Reject low-signal labels vision sometimes returns for generic
            # category/nav tiles (e.g. bare "SALE") that carry no actual
            # offer detail (%, $, or a concrete benefit like free shipping)
            has_number = re.search(r"\d", text)
            has_benefit = re.search(r"free\s+(delivery|shipping|gift)|buy\s+\d|\bBOGO\b", text, re.I)
            if not has_number and not has_benefit:
                continue

            def _f(v):
                try: return float(v) if v is not None else None
                except (TypeError, ValueError): return None

            items.append({
                "source":       "image_promo",
                "brand":        self.brand,
                "source_url":   self.source_url,
                "title":        text,
                "category":     offer.get("category"),
                "discount_min": _f(offer.get("discount_min")),
                "discount_max": _f(offer.get("discount_max")),
                "confidence":   offer.get("confidence", "medium"),
                "scraped_at":   datetime.now(timezone.utc).isoformat(),
            })
        return items