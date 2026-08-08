"""
services/anti_bot_bypass_service.py
====================================
Standalone Anti-Bot Bypass Service module.

Executes a multi-tier bypass cascade to fetch web pages protected by:
- PerimeterX / HUMAN Security
- Cloudflare WAF Bot Management & Turnstile Challenges
- Chromium local_rate_limited stubs

Cascade Tiers:
  1. Playwright Stealth Navigation
  2. Bot Challenge / Stub Detection Check
  3. HTTP Fallback via httpx
  4. TLS Browser Impersonation via curl_cffi (Chrome124 / Firefox TLS Client Hello)
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

KNOWN_BOT_CHALLENGE_TITLES = (
    "access to this page has been denied",
    "verifying your connection",
    "just a moment...",
    "attention required! | cloudflare",
    "security check",
    "ddos-guard",
    "pardon our interruption",       # Incapsula / Imperva WAF
    "please wait...",
    "checking your browser",
    "browser check",
)

KNOWN_BOT_CHALLENGE_TEXTS = (
    "local_rate_limited",
    "please enable cookies",
    "px-captcha",
    "cf-browser-verification",
    "ray id:",
    "cf-chl-widget",                  # Cloudflare Turnstile widget
    "_cf_chl_opt",                    # Cloudflare challenge options object
    "incapsula incident id",          # Incapsula block reference
    "visitorid",                      # PerimeterX telemetry
)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Return *items* with duplicates removed, preserving first-occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


@dataclass
class BypassResult:
    html: str
    status_code: int
    strategy_used: str
    is_blocked: bool
    title: str = ""
    error_message: str | None = None


class AntiBotBypassService:
    """
    Decoupled anti-bot bypass service.
    """

    def __init__(self, user_agent: str = _DEFAULT_UA, cffi_impersonate: str = "chrome124"):
        self.user_agent = user_agent
        # Browser profile for curl_cffi TLS impersonation. Newer profiles
        # (e.g. "chrome131", "safari17_2") may have better bypass rates against
        # modern WAFs; pass the desired value at construction time to switch.
        self.cffi_impersonate = cffi_impersonate

    def is_bot_blocked(self, html: str, title: str = "", status_code: int = 200) -> bool:
        """
        Evaluates whether a response represents a bot challenge / block screen.
        """
        if status_code in (403, 429, 503):
            return True

        if len(html.strip()) < 500:
            return True

        title_lower = title.lower().strip()
        if any(bad_title in title_lower for bad_title in KNOWN_BOT_CHALLENGE_TITLES):
            return True

        html_lower = html[:4000].lower()
        if any(bad_text in html_lower for bad_text in KNOWN_BOT_CHALLENGE_TEXTS):
            return True

        return False

    def _build_headers(self) -> dict:
        """Builds the common request headers shared by all HTTP fallback tiers."""
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def fetch_with_cffi(self, url: str, impersonate: str | None = None) -> BypassResult:
        """
        Fetches page content using curl_cffi with authentic TLS Client Hello fingerprinting.
        """
        if not HAS_CURL_CFFI:
            return BypassResult(
                html="",
                status_code=0,
                strategy_used="curl_cffi",
                is_blocked=True,
                error_message="curl_cffi is not installed in environment",
            )

        try:
            profile = impersonate or self.cffi_impersonate
            logger.info("Executing curl_cffi TLS impersonation (%s) → %s", profile, url)
            resp = cffi_requests.get(
                url,
                impersonate=profile,
                headers=self._build_headers(),
                timeout=20,
            )
            title = self._extract_title(resp.text)
            blocked = self.is_bot_blocked(resp.text, title=title, status_code=resp.status_code)
            
            return BypassResult(
                html=resp.text,
                status_code=resp.status_code,
                strategy_used="curl_cffi",
                is_blocked=blocked,
                title=title,
            )
        except Exception as exc:
            logger.warning("curl_cffi fetch failed for %s: %s", url, exc)
            return BypassResult(
                html="",
                status_code=0,
                strategy_used="curl_cffi",
                is_blocked=True,
                error_message=str(exc),
            )

    def fetch_with_httpx(self, url: str) -> BypassResult:
        """
        Standard HTTP fallback using httpx.
        """
        try:
            logger.info("Executing httpx HTTP fallback → %s", url)
            headers = self._build_headers()
            resp = httpx.get(url, follow_redirects=True, headers=headers, timeout=20.0)
            title = self._extract_title(resp.text)
            blocked = self.is_bot_blocked(resp.text, title=title, status_code=resp.status_code)
            return BypassResult(
                html=resp.text,
                status_code=resp.status_code,
                strategy_used="httpx_fallback",
                is_blocked=blocked,
                title=title,
            )
        except Exception as exc:
            logger.warning("httpx fetch failed for %s: %s", url, exc)
            return BypassResult(
                html="",
                status_code=0,
                strategy_used="httpx_fallback",
                is_blocked=True,
                error_message=str(exc),
            )

    def fetch_with_bright_data(self, url: str, zone: str | None = None) -> BypassResult:
        """
        Tier 4 fallback: Bright Data Web Unlocker API.
        Sends request via https://api.brightdata.com/request using BRIGHT_DATA_API_KEY.
        """
        import os
        api_key = os.getenv("BRIGHT_DATA_API_KEY")
        if not api_key:
            return BypassResult(
                html="",
                status_code=0,
                strategy_used="bright_data_web_unlocker",
                is_blocked=True,
                error_message="BRIGHT_DATA_API_KEY is not set in environment",
            )

        zone_name = zone or os.getenv("BRIGHT_DATA_ZONE", "web_unlocker")
        logger.info("Executing Bright Data Web Unlocker API (zone='%s') → %s", zone_name, url)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "zone": zone_name,
            "url": url,
            "format": "raw",
        }

        try:
            resp = httpx.post("https://api.brightdata.com/request", json=payload, headers=headers, timeout=45.0)
            if resp.status_code == 200:
                html = resp.text
                title = self._extract_title(html)
                blocked = self.is_bot_blocked(html, title=title, status_code=resp.status_code)
                return BypassResult(
                    html=html,
                    status_code=resp.status_code,
                    strategy_used="bright_data_web_unlocker",
                    is_blocked=blocked,
                    title=title,
                )
            else:
                logger.warning(
                    "Bright Data Web Unlocker API returned status %d for %s: %s",
                    resp.status_code, url, resp.text[:200]
                )
                return BypassResult(
                    html="",
                    status_code=resp.status_code,
                    strategy_used="bright_data_web_unlocker",
                    is_blocked=True,
                    error_message=f"Bright Data API status {resp.status_code}: {resp.text[:200]}",
                )
        except Exception as exc:
            logger.warning("Bright Data Web Unlocker fetch failed for %s: %s", url, exc)
            return BypassResult(
                html="",
                status_code=0,
                strategy_used="bright_data_web_unlocker",
                is_blocked=True,
                error_message=str(exc),
            )

    def resolve_page_content(self, url: str, page: Any, status_code: int = 200) -> BypassResult:
        """
        Evaluates current Playwright page state, and executes fallback cascade if bot-blocked.
        If fallback succeeds, injects full HTML back into Playwright page context via page.set_content().

        Args:
            url:         The URL that was navigated to.
            page:        The Playwright page object after navigation.
            status_code: The HTTP status returned by the navigation (pass response.status
                         from page.goto() so 403/429 responses are detected early).
        """
        html = page.content()
        title = page.title()
        
        if not self.is_bot_blocked(html, title=title, status_code=status_code):
            return BypassResult(
                html=html,
                status_code=status_code,
                strategy_used="playwright_stealth",
                is_blocked=False,
                title=title,
            )

        logger.info(
            "Bot challenge / block detected on Playwright load (title='%s', length=%d). Initiating bypass cascade...",
            title, len(html)
        )

        # Tier 1 (Playwright stealth) is blocked. Try Tier 2: httpx fallback.
        httpx_res = self.fetch_with_httpx(url)
        if not httpx_res.is_blocked and len(httpx_res.html) >= 500:
            logger.info("httpx fallback succeeded (%d bytes). Setting Playwright content.", len(httpx_res.html))
            try:
                page.set_content(httpx_res.html)
            except Exception as exc:
                logger.warning("page.set_content failed during httpx fallback: %s", exc)
            return httpx_res

        # Tier 3: curl_cffi TLS impersonation — try multiple browser profiles
        # before giving up. Newer profiles (chrome131) and cross-engine profiles
        # (firefox135) defeat WAFs that blocklist stale chrome124 JA3 signatures.
        _cffi_profiles = _dedupe_preserve_order(
            [self.cffi_impersonate, "chrome131", "firefox135"]
        )
        for _profile in _cffi_profiles:
            cffi_res = self.fetch_with_cffi(url, impersonate=_profile)
            if not cffi_res.is_blocked and len(cffi_res.html) >= 500:
                logger.info(
                    "curl_cffi TLS impersonation succeeded [%s] (%d bytes). Setting Playwright content.",
                    _profile, len(cffi_res.html),
                )
                try:
                    page.set_content(cffi_res.html)
                except Exception as exc:
                    logger.warning("page.set_content failed during curl_cffi fallback: %s", exc)
                return cffi_res
            logger.debug("curl_cffi profile '%s' still blocked for %s", _profile, url)

        # Tier 4: Bright Data Web Unlocker API
        bright_data_res = self.fetch_with_bright_data(url)
        if not bright_data_res.is_blocked and len(bright_data_res.html) >= 500:
            logger.info("Bright Data Web Unlocker succeeded (%d bytes). Setting Playwright content.", len(bright_data_res.html))
            try:
                page.set_content(bright_data_res.html)
            except Exception as exc:
                logger.warning("page.set_content failed during Bright Data fallback: %s", exc)
            return bright_data_res

        logger.error("All anti-bot bypass tiers failed for %s", url)
        return BypassResult(
            html=html,
            status_code=403,
            strategy_used="cascade_exhausted",
            is_blocked=True,
            title=title,
            error_message="All anti-bot bypass tiers failed to retrieve valid HTML",
        )

    def _extract_title(self, html: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""
