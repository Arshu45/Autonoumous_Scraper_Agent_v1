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
    retry_if_exception,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

from .utils import extract_and_log_metrics

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

# Compiled once at import — rejects bare discount-amount-only strings
# that carry no actionable offer context (e.g. "$600 OFF", "42% OFF", "1/2 PRICE").
GENERIC_DISCOUNT_PATTERN = re.compile(
    r"""
    ^
    (
        \$\d+\s*OFF      |   # $600 OFF
        \d+\s*%\s*OFF    |   # 42% OFF
        \d+/\d+\s*PRICE      # 1/2 PRICE
    )
    $
    """,
    re.I | re.X,
)

# Matches rate-limit signals from all supported providers:
#   - Generic:            429, quota, exhausted
#   - Claude / Anthropic: overloaded, rate_limit_error, AnthropicError
#   - LiteLLM gateway:    RateLimitError, litellm.RateLimitError
_RATE_LIMIT_PATTERN = re.compile(
    r"429|quota|exhausted|overloaded|rate.?limit|resource_exhausted|too_many_requests|AnthropicError",
    re.IGNORECASE,
)


def _is_rate_limit_or_transient_error(exc: Exception) -> bool:
    """
    Predicate for tenacity retry in _call_llm.
    Checks HTTP status codes (429, 502, 503, 504, 529), exception class names
    (RateLimit, ResourceExhausted, etc.), wrapped response objects, and
    regex message matching across LiteLLM, direct Gemini, and HTTP clients.
    """
    if exc is None:
        return False

    # 1. Check direct status code attributes
    for attr in ("status_code", "status", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and val in (429, 502, 503, 504, 529):
            return True

    # 2. Check wrapped response object (e.g. httpx.HTTPStatusError)
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code in (429, 502, 503, 504, 529):
            return True

    # 3. Check exception class hierarchy and type name
    exc_type_str = f"{type(exc).__module__}.{type(exc).__name__}"
    if any(name in exc_type_str for name in ("RateLimit", "ResourceExhausted", "TooManyRequests", "Overloaded")):
        return True

    # 4. Fallback: string regex match on exception representation
    return bool(_RATE_LIMIT_PATTERN.search(str(exc)))


# ── Gemini Vision prompt ────────────────────────────────────────────────────
VISION_PROMPT_TEMPLATE = """You are a retail promotions analyst.
Examine the promotional banner image provided.
Extract every offer or discount visible in the image.

We only want direct promotional offers (e.g., % off, spend-and-save, buy-one-get-one, multi-buys, direct discounts).
Do NOT extract:
  - First-purchase / first-order / new customer welcome incentives (e.g., "Enjoy 15% off your first purchase", "10% off your first order", welcome coupons).
  - Loyalty/rewards program point accumulations or milestone achievements (e.g., "Collect 750 more ICONS", "1000 ICONS = $10 REWARD", "EARN 2x ICONS").
  - Newsletter signup incentives (e.g., "Sign up to THE ICONIC News for your $20 voucher").
  - General shipping/locker rules or logistics notices (e.g., "FREE Express Delivery When You Use a Free, 24/7 Parcel Locker").

Return a JSON array only — no explanation, no markdown, no code fences.
Each element must have exactly these fields:
  - "promo_text" : Read and combine ALL visible text from top to bottom on the image into a single complete offer title. Always include top banner titles/headings (e.g. "Online Warehouse Sale") together with the main discount/price text (e.g. "Prices from $12").
  - "category"   : one reporting category from this list only: {categories}. or null.
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
                category_hint = entry.get("category_hint")
            else:
                url = entry
                category = target_config.get("category")
                # Top-level category_hint fallback for plain-string source_url entries
                category_hint = target_config.get("category_hint")
            if url:
                self.source_entries.append({"url": url, "category": category, "category_hint": category_hint})

        self.source_urls = [entry["url"] for entry in self.source_entries]
        self.source_url = self.source_urls[0] if self.source_urls else ""
        self.current_category = self.source_entries[0].get("category") if self.source_entries else target_config.get("category")
        # Per-URL category hint injected into the LLM categorization prompt;
        # updated on each iteration of the run() loop.
        self.current_category_hint: str | None = (
            self.source_entries[0].get("category_hint") if self.source_entries else target_config.get("category_hint")
        )
        self.strategy   = target_config.get("extraction_strategy", "image")
        self.allowed_categories = self._load_allowed_categories()
        self.category_list_text = ", ".join(self.allowed_categories)
        self.vision_prompt = VISION_PROMPT_TEMPLATE.format(categories=self.category_list_text)

        from services.anti_bot_bypass_service import AntiBotBypassService
        self.anti_bot_service = AntiBotBypassService()

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

        # Init API client — needed by all strategies for _categorize_offer_items
        # (joint categorization + dedup LLM pass), not just screenshot/image.
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
            # Update the per-URL hint so _categorize_offer_items sees the right context
            self.current_category_hint = source_entry.get("category_hint") or self.cfg.get("category_hint")
            logger.info(
                "Starting extraction → %s [category=%s, hint=%s, strategy=%s]",
                self.source_url,
                self.current_category or "uncategorized",
                self.current_category_hint or "none",
                self.strategy,
            )

            offer_items: list[dict] = []

            if self.strategy in ("text", "screenshot", "hybrid"):
                offer_items = self._run_playwright_extraction()
            elif self.strategy == "image":
                offer_items = self._run_image()
            else:
                logger.error("Unknown strategy '%s' — skipping", self.strategy)
                continue

            all_offer_items.extend(offer_items)

        # Deduplicate extracted offers within this run.
        # - text_scraper promos: dedup by title only. The same brand-deal tile
        #   (e.g. "Rodd & Gunn: Buy 2 Save $80") appears on every page because it
        #   lives in a shared nav component. We keep the FIRST occurrence, which
        #   carries the correct current_category seeded from the config URL entry.
        # - image_promo / other: keep full (source, brand, source_url, title) key
        #   since banner images are already content-hash deduped inside Playwright
        #   and may legitimately differ per page.
        seen_text_titles: set[str] = set()
        seen_keys: set[tuple] = set()
        deduped_items = []
        for item in all_offer_items:
            title_clean = (item.get("title") or "").strip()
            item["title"] = title_clean
            if item.get("source") == "text_scraper":
                norm = self._norm_key(title_clean)
                if norm in seen_text_titles:
                    continue
                seen_text_titles.add(norm)
            else:
                key = (item.get("source"), item.get("brand"), item.get("source_url"), title_clean.lower())
                if key in seen_keys:
                    continue
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

    def _record_skip(self, reason: str, details: str = "") -> None:
        self._images_skipped += 1
        self._skip_reasons[reason] = self._skip_reasons.get(reason, 0) + 1
        if details:
            logger.info("  SKIP [%s]: %s", reason, details)
        else:
            logger.debug("  SKIP [%s]", reason)

    def _create_stealth_page(self, pw, engine: str | None = None) -> tuple[Any, Any]:
        """Launch browser and context with stealth settings.

        Args:
            engine: Browser engine to use. If None, reads config["browser_type"]
                    (defaulting to "firefox").
                    - "firefox": Avoids Chromium's internal ``local_rate_limited`` stub
                      in batch runs (default for all sites).
                    - "chrome": Real Google Chrome — authentic JA3/TLS + Client Hints.
                      Auto-used as fallback when Firefox gets a WAF rejection.
                    - "chromium": Playwright-bundled Chromium with Chrome args.
        """
        engine = (engine or self.cfg.get("browser_type", "firefox")).lower()

        if engine in ("chrome", "chromium"):
            channel: str | None = (
                self.cfg.get("browser_channel")
                or os.getenv("PLAYWRIGHT_BROWSER_CHANNEL")
                or ("chrome" if engine == "chrome" else None)
            )
            launch_kwargs = dict(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--window-size=1440,900",
                ],
            )
            try:
                browser = pw.chromium.launch(channel=channel, **launch_kwargs)
            except Exception as err:
                if channel and "not found" in str(err).lower():
                    logger.warning(
                        "Browser channel '%s' not available (%s) — falling back to Playwright bundled Chromium.",
                        channel, err,
                    )
                    try:
                        browser = pw.chromium.launch(**launch_kwargs)
                    except Exception as err2:
                        logger.warning("Failed bundled Chromium (%s) — falling back to firefox", err2)
                        browser = pw.firefox.launch(headless=True)
                else:
                    logger.warning("Failed to launch %s (%s) — falling back to firefox", engine, err)
                    browser = pw.firefox.launch(headless=True)

            headers = {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "accept-language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        else:
            browser = pw.firefox.launch(headless=True)
            headers = {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
            }

        context_kwargs = {
            "user_agent": _UA,
            "viewport": {"width": 1440, "height": 900},
        }
        if self.cfg.get("use_stealth_headers", True):
            context_kwargs["extra_http_headers"] = headers

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        if self.cfg.get("use_webdriver_patch", True):
            page.add_init_script("delete navigator.__proto__.webdriver;")
        return browser, page

    @staticmethod
    def _is_waf_firefox_rejection(nav_status: int, page_html: str) -> bool:
        """Return True when the WAF rejected our Firefox connection.

        Distinguishes a WAF-level block from Chromium's own internal rate-limiter:
        - We only call this when the active engine is Firefox, so the stub cannot
          be Chromium's internal limiter.
        - Akamai / similar WAFs return HTTP 429 with a tiny ``local_rate_limited``
          body when they refuse Firefox on TLS/Client-Hints grounds.
        """
        return (
            nav_status == 429
            and len(page_html) < 300
            and "local_rate_limited" in page_html
        )

    def _navigate_with_chrome_fallback(
        self, pw
    ) -> tuple[Any, Any, int]:
        """Launch browser, navigate to source_url, and auto-retry with Chrome
        if Firefox is rejected at the WAF level (Akamai local_rate_limited 429).

        Returns:
            (browser, page, nav_status) — always the active browser/page pair
            after any retries.
        """
        configured_engine = self.cfg.get("browser_type", "firefox").lower()
        browser, page = self._create_stealth_page(pw, engine=configured_engine)
        nav_status = self._goto_with_retry(page, self.source_url)

        if configured_engine == "firefox" and self._is_waf_firefox_rejection(
            nav_status, page.content()
        ):
            logger.info(
                "[%s] Firefox rejected by WAF (local_rate_limited 429). "
                "Auto-retrying with Chrome...",
                self.brand,
            )
            browser.close()
            browser, page = self._create_stealth_page(pw, engine="chrome")
            nav_status = self._goto_with_retry(page, self.source_url)

        return browser, page, nav_status

    def _goto_with_retry(self, page, url: str) -> int:
        """
        Navigate with a small retry budget on timeout. A transient bot-check
        or slow first paint previously meant a single failed page.goto killed
        the entire source with zero offers; this gives it one more shot.

        Returns:
            The HTTP status code of the final successful navigation (200 if
            the response object is unavailable), so callers can forward it to
            resolve_page_content() for early 403/429 bot-block detection.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        last_exc: Exception | None = None
        for attempt in range(1, self.nav_retry_attempts + 1):
            try:
                response = page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                return response.status if response else 200
            except PWTimeout as e:
                last_exc = e
                logger.warning("Navigation timeout (attempt %d/%d) for %s", attempt, self.nav_retry_attempts, url)
                if attempt < self.nav_retry_attempts:
                    page.wait_for_timeout(3_000)
        if last_exc:
            raise last_exc
        return 200

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
            browser, page, nav_status = self._navigate_with_chrome_fallback(pw)
            try:
                bypass_res = self.anti_bot_service.resolve_page_content(self.source_url, page, status_code=nav_status)
                logger.info(
                    "[%s] Page resolved via %s. Title: '%s', Content Length: %d",
                    self.brand, bypass_res.strategy_used, bypass_res.title, len(page.content())
                )
                if not bypass_res.is_blocked:
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
                                    # text = el.inner_text().strip()
                                    tag = el.evaluate("el => el.tagName.toLowerCase()")

                                    if tag == "img":
                                        text = (
                                            el.get_attribute("alt")
                                            or el.get_attribute("title")
                                            or ""
                                        ).strip()
                                    else:
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
                        logger.info("[%s] No screenshot_selectors specified in config — skipping screenshot extraction.", self.brand)
                    
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
                                        self._record_skip("excluded_pattern", f"URL matched exclude pattern: {img_src[:80]}")
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
                                            self._record_skip("fetch_failed", f"Failed frame fetch for {img_src[:80]}")
                                            continue
                                else:
                                    # For non-img elements (containers, divs), they MUST be visible
                                    if not box or box['width'] < 100 or box['height'] < 50:
                                        continue
                                    try:
                                        ss_bytes = el.screenshot()
                                    except Exception:
                                        self._record_skip("fetch_failed", f"Screenshot failed for element {selector}")
                                        continue

                                if not ss_bytes:
                                    continue

                                # Deduplicate identical screenshots
                                ss_hash = hashlib.sha256(ss_bytes).hexdigest()
                                if ss_hash in seen_hashes:
                                    logger.debug("Skipping duplicate image hash: %s", ss_hash[:12])
                                    continue
                                seen_hashes.add(ss_hash)

                                # Check dimensions
                                try:
                                    img = Image.open(BytesIO(ss_bytes))
                                    if img.width < self.min_width or img.height < self.min_height:
                                        self._record_skip("too_small", f"Dimensions {img.width}x{img.height} below min {self.min_width}x{self.min_height} ({selector})")
                                        continue
                                    if img.width > 0 and (img.width / max(img.height, 1)) < self.min_aspect:
                                        self._record_skip("bad_aspect", f"Aspect ratio {img.width/max(img.height,1):.2f} below min {self.min_aspect} ({selector})")
                                        continue
                                except Exception:
                                    self._record_skip("unreadable_image", f"Unreadable image bytes ({selector})")
                                    continue

                                self._images_found += 1
                                label = f"screenshot:{selector}"

                                if self._gemini_api_calls > 0:
                                    time.sleep(self.delay)

                                offers = self._vision_extract_cached(ss_bytes, "image/png", label, content_hash=ss_hash)
                                if offers is None:
                                    self._record_skip("unparsable_response", f"Vision LLM returned unparsable response ({label})")
                                elif offers:
                                    items = self._build_offer_items(offers, label)
                                    offer_items.extend(items)
                                    self._images_processed += 1
                                    logger.info("  SHOT  %d offers from selector '%s'", len(items), selector)
                                else:
                                    self._record_skip("no_offers_found", f"Vision LLM returned 0 offers for image ({selector})")

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
        self._images_found += len(image_urls)
        logger.info("Image strategy: %d candidate URLs found for %s", len(image_urls), self.source_url)

        offer_items: list[dict] = []

        # Reuse a single HTTP client for all downloads — enables connection
        # pooling and HTTP/2 multiplexing instead of a fresh TCP+TLS
        # handshake per image.
        with httpx.Client(timeout=15, follow_redirects=True, http2=True) as http_client:
            for url in image_urls:
                if self._gemini_api_calls > 0:
                    time.sleep(self.delay)

                img_bytes, mime = self._download_image(url, client=http_client)
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
            browser, page, nav_status = self._navigate_with_chrome_fallback(pw)
            try:
                bypass_res = self.anti_bot_service.resolve_page_content(self.source_url, page, status_code=nav_status)
                logger.info(
                    "[%s] Image URL collection resolved via %s. Title: '%s'",
                    self.brand, bypass_res.strategy_used, bypass_res.title,
                )
                if not bypass_res.is_blocked:
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

        The lock protects ONLY the timestamp read/write (a microsecond
        operation).  Each thread claims the next available dispatch slot by
        advancing _last_vision_api_time inside the lock, then sleeps
        OUTSIDE the lock until its slot arrives.  This way concurrent
        threads are staggered by VISION_API_MIN_DELAY without serializing
        behind each other's sleep calls.
        """
        global _last_vision_api_time
        sleep_time = 0.0
        with _vision_api_lock:
            now = time.time()
            # The earliest this thread may fire is the later of "now" and
            # "last dispatch + minimum delay".
            earliest = max(now, _last_vision_api_time + VISION_API_MIN_DELAY)
            sleep_time = earliest - now
            # Claim this slot so the next thread sees it.
            _last_vision_api_time = earliest

        if sleep_time > 0:
            logger.debug("Vision API rate gate: sleeping %.2fs", sleep_time)
            time.sleep(sleep_time)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=5, max=70),
        retry=retry_if_exception(_is_rate_limit_or_transient_error),
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

        call_type = "vision_extraction" if image_bytes is not None else "text_categorization"
        actual_cost = extract_and_log_metrics(
            response=response,
            use_litellm=self.use_litellm,
            call_type=call_type,
            default_cost=cost_usd
        )
        self._estimated_cost_usd += actual_cost
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

    # ═══════════════════════════════════════════════════════════════════════
    # Joint Category Classification & Semantic Deduplication
    # ═══════════════════════════════════════════════════════════════════════

    def _categorize_offer_items(self, offer_items: list[dict]) -> list[dict]:
        """
        Processes all extracted offers for this brand in a single LLM pass to:
        1. Filter out non-promotional/loyalty/newsletter/shipping items.
        2. Semantically group and deduplicate offers that refer to the same campaign,
           selecting/synthesizing the single cleanest canonical title.
        3. Assign an official reporting category to each canonical promotion.
        """
        if not offer_items or not self._client:
            return offer_items

        allowed = set(self.allowed_categories)
        records = [
            {
                "id": idx,
                "brand": item.get("brand"),
                "source_url": item.get("source_url"),
                "promo_text": item.get("title"),
                "current_category": item.get("category"),
            }
            for idx, item in enumerate(offer_items)
        ]

        # Build the optional category hint line separately to keep prompt assembly clean
        _hint_line = (
            f"   - Brand/URL context hint: {self.current_category_hint}\n"
            "     Apply this as a STRONG PRIOR — when promo text is a storewide sale, warehouse sale, "
            "or a bare discount with no product detail, prefer the hinted category over 'Others'.\n"
            if self.current_category_hint else ""
        )

        prompt = (
            f"You analyze, categorize, and deduplicate retail promotions for brand '{self.brand}'.\n"
            "Perform the following 3 tasks carefully:\n\n"
            "1. FILTERING: Set 'keep': false for non-promotional or unwanted items:\n"
            "   - First-purchase / first-order / new customer welcome incentives (e.g., 'Enjoy 15% off your first purchase', '10% off your first order', welcome coupons)\n"
            "   - Loyalty/rewards program point accumulations or milestone achievements (e.g., 'Collect 750 more ICONS', '1000 ICONS = $10 REWARD', 'EARN 2x ICONS')\n"
            "   - Newsletter signup incentives (e.g., 'Sign up to THE ICONIC News for your $20 voucher')\n"
            "   - General shipping/locker rules or logistics notices (e.g., 'FREE Express Delivery When You Use a Free, 24/7 Parcel Locker')\n"
            "   - Standalone delivery/shipping threshold notices with NO attached product deal "
            "(e.g., 'FREE DELIVERY ON ORDERS OVER $150', 'FAST & FREE DELIVERY', 'FREE EXPRESS SHIPPING ON ALL ORDERS'). "
            "EXCEPTION: keep if free delivery is explicitly bundled into a specific product offer (e.g., 'Buy X + Get Free Delivery').\n\n"
            "2. SEMANTIC DEDUPLICATION & CANONICALIZATION:\n"
            "   - Several extracted texts may refer to the identical underlying offer, discount, or campaign\n"
            "     (e.g., 'Clearance New Styles Added Up to 50% off selected clearance clothing, footwear and home' vs 'Up to 50% off selected clearance!* Shop now').\n"
            "   - Group items that describe the same promotion under 'merged_ids'.\n"
            "   - Provide a clean, complete, and standard 'canonical_title' for the merged promotion:\n"
            "     * Preserve all key promotion components present in the text: discount/badge (e.g., '$500 OFF', 'EPIC DEAL'), product/category name (e.g., 'Samsung Galaxy S25 256GB Navy'), and final sale price / original price (e.g., '$887', 'Was $1387').\n"
            "     * Format product deal offers clearly, e.g.: '$500 OFF Samsung Galaxy S25 256GB Navy - $887 (Was $1387)' or 'EPIC DEAL Lenovo IdeaPad Slim 3 14\" i5 8GB 512GB Laptop - $799'.\n"
            "     * Remove CTA clutter like 'Shop now', '*Terms apply', 'While stocks last', ticket symbols (\u00a4), or raw UI button text.\n\n"
            "3. CATEGORIZATION:\n"
            f"   - Assign exactly one category from this list only: {self.category_list_text}.\n"
            "   - Use promo text, brand, and source_url as strong evidence.\n"
            f"{_hint_line}"
            "Return JSON only as a list of objects:\n"
            "[\n"
            "  {\n"
            "    \"canonical_title\": \"Clean descriptive offer text\",\n"
            "    \"category\": \"Category from list\",\n"
            "    \"keep\": true,\n"
            "    \"merged_ids\": [0, 1]\n"
            "  }\n"
            "]\n"
            "Do not add markdown formatting outside the JSON array.\n\n"
            f"Extracted Promotions:\n{json.dumps(records, ensure_ascii=False)}"
        )

        try:
            raw = self._call_llm(
                text_prompt=prompt,
                json_mode=True,
                cost_usd=self._estimate_text_input_cost(prompt),
            )
            clusters = self._parse_json_array(raw)
        except Exception as e:
            logger.warning("Brand categorization/deduplication call failed; keeping raw items: %s", e)
            return [item for item in offer_items if not item.get("exclude")]

        if not clusters:
            logger.warning("Categorization returned unparsable response; keeping raw items")
            return [item for item in offer_items if not item.get("exclude")]

        final_items: list[dict] = []
        processed_ids = set()

        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue

            merged_ids = cluster.get("merged_ids", [])
            if not isinstance(merged_ids, list) or not merged_ids:
                continue

            valid_ids = [idx for idx in merged_ids if isinstance(idx, int) and 0 <= idx < len(offer_items)]
            if not valid_ids:
                continue

            # Always mark IDs as processed by the LLM, even if excluded (keep=False)
            processed_ids.update(valid_ids)

            keep = cluster.get("keep")
            raw_titles = [offer_items[i].get("title", "") for i in valid_ids if i < len(offer_items)]
            if keep is False:
                logger.info("  FILTERED OUT (LLM policy keep=False): %s -> Cluster Title: '%s'", raw_titles, cluster.get("canonical_title"))
                continue

            canonical_title = cluster.get("canonical_title")
            category = cluster.get("category")

            # Fan-out: emit one row per unique source_url in this cluster.
            #
            # This is now safe because:
            #   - text_scraper promos are pre-collapsed to 1 item per title
            #     in run() before reaching here — fan-out is a no-op for them.
            #   - image_promo items from /men/ and /kids/ are genuinely different
            #     page appearances and each get their own DB row (same behaviour
            #     as ASOS storing the same banner under Womens and Menswear).
            seen_urls_in_cluster: set[str] = set()
            cluster_items_added = 0
            for item_idx in valid_ids:
                source_item = dict(offer_items[item_idx])
                item_url = source_item.get("source_url") or ""

                if item_url in seen_urls_in_cluster:
                    continue  # same URL already emitted for this cluster
                seen_urls_in_cluster.add(item_url)

                # Apply canonical title
                if canonical_title and isinstance(canonical_title, str) and canonical_title.strip():
                    source_item["title"] = canonical_title.strip()

                # Apply LLM category; if LLM said "Others" but this item's
                # per-URL seeded category is more specific, keep the specific one.
                if category in allowed:
                    if category == "Others" and source_item.get("category") in allowed and source_item.get("category") != "Others":
                        pass  # preserve per-URL hint (e.g. Kids, Menswear)
                    else:
                        source_item["category"] = category
                elif not source_item.get("category") or source_item.get("category") not in allowed:
                    source_item["category"] = self.cfg.get("category") or "Others"

                # Hard override: if the brand config declares an explicit category,
                # it always takes precedence over whatever the LLM assigned.
                # e.g. config category="Beauty" beats LLM-assigned "Menswear".
                config_category = self.cfg.get("category")
                if config_category and config_category in allowed:
                    if source_item.get("category") != config_category:
                        logger.info("  Category hard-override: LLM chose '%s' but config declares '%s' → using '%s'", source_item.get("category"), config_category, config_category)
                    source_item["category"] = config_category

                logger.info("  KEPT & CANONICALIZED: '%s' (Category: %s) [Merged %d raw offer(s): %s]", source_item["title"], source_item["category"], len(valid_ids), raw_titles)
                final_items.append(source_item)

        # Append any items that were left out of the LLM JSON (safety fallback)
        for idx, item in enumerate(offer_items):
            if idx not in processed_ids and not item.get("exclude"):
                if not item.get("category") or item.get("category") not in allowed:
                    item["category"] = self.cfg.get("category") or "Others"
                final_items.append(item)

        # Final safety guarantee: no item ever has category=None or unlisted category
        config_category = self.cfg.get("category")
        for item in final_items:
            if not item.get("category") or item.get("category") not in allowed:
                item["category"] = config_category or "Others"
            # Also override "Others" if config declares a specific category
            if item.get("category") == "Others" and config_category and config_category in allowed:
                item["category"] = config_category

        logger.info(
            "Brand semantic deduplication: %d raw offers merged into %d canonical promotions",
            len(offer_items), len(final_items)
        )
        return final_items

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
                return [{"promo_text": stripped[:400], "category": None, "confidence": "low"}]
            return None
        return offers

    # ═══════════════════════════════════════════════════════════════════════
    # Image download (image strategy only)
    # ═══════════════════════════════════════════════════════════════════════

    def _download_image(self, url: str, *, client: httpx.Client | None = None) -> tuple[bytes | None, str]:
        """Download image bytes. Returns (bytes, mime) or (None, '')."""
        try:
            if client is not None:
                r = client.get(url, headers={"User-Agent": _UA, "Referer": self.source_url})
            else:
                # Fallback: one-off client for standalone calls
                with httpx.Client(timeout=15, follow_redirects=True) as fallback:
                    r = fallback.get(url, headers={"User-Agent": _UA, "Referer": self.source_url})
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
        """Build an offer dict from scraped text."""
        return {
            "source":       "text_scraper",
            "brand":        self.brand,
            "source_url":   self.source_url,
            "title":        text[:200],
            # Seed with per-URL category hint from config so the downstream
            # categorization LLM has page-level context as a starting point.
            "category":     self.current_category,
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

            if GENERIC_DISCOUNT_PATTERN.match(text):
                continue

            # Reject low-signal labels vision sometimes returns for generic
            # category/nav tiles (e.g. bare "SALE") that carry no actual
            # offer detail (%, $, or a concrete benefit like free shipping)
            has_number = re.search(r"\d", text)
            has_benefit = re.search(r"free\s+(delivery|shipping|gift)|buy\s+\d|\bBOGO\b", text, re.I)
            if not has_number and not has_benefit:
                continue

            items.append({
                "source":       "image_promo",
                "brand":        self.brand,
                "source_url":   self.source_url,
                "title":        text,
                # Vision LLM category takes priority; fall back to per-URL
                # config hint (current_category) so a banner from /kids/ at
                # least starts with "Kids" before the dedup/categorize pass.
                "category":     offer.get("category") or self.current_category,
                "confidence":   offer.get("confidence", "medium"),
                "scraped_at":   datetime.now(timezone.utc).isoformat(),
            })
        return items