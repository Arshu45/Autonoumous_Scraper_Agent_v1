# agent/exploration_agent.py

import os
import re
import json
import logging
import base64
from PIL import Image
from io import BytesIO
from typing import Dict, Any, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import google.genai as genai
from google.genai import types as genai_types
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from agent.models import SiteAnalysis
from agent.prompts import EXPLORATION_VISUAL_PROMPT, DOM_ANALYSIS_PROMPT

logger = logging.getLogger(__name__)

# Common browser setup
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ANTI_BOT_CHECKS = {
    "cloudflare_challenge": lambda resp, dom: (
        "Just a moment" in dom or (resp is not None and resp.headers.get("cf-mitigated") == "challenge")
    ),
    "captcha_present": lambda resp, dom: any(
        marker in dom.lower() for marker in ["recaptcha", "hcaptcha", "turnstile"]
    ),
    "blocked_status_code": lambda resp, dom: resp is not None and resp.status in (403, 429, 503),
    "suspously_short_dom": lambda resp, dom: len(dom) < 2000,
    "bot_detection_script": lambda resp, dom: any(
        marker in dom for marker in ["datadome", "perimeterx", "akamai-bot"]
    ),
}

def score_anti_bot_risk(resp, dom: str) -> Tuple[str, Dict[str, bool]]:
    triggered = {}
    for name, check in ANTI_BOT_CHECKS.items():
        try:
            triggered[name] = bool(check(resp, dom))
        except Exception as e:
            logger.warning("Error running anti-bot check %s: %s", name, e)
            triggered[name] = False

    hits = sum(triggered.values())
    if hits >= 2:
        risk = "high"
    elif hits == 1:
        risk = "medium"
    else:
        risk = "low"
    return risk, triggered

def clean_dom_regex(dom_html: str) -> str:
    """Safely cleans the HTML DOM by decomposing unneeded tags (head, script, style, svg, iframe, noscript)."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(dom_html, "html.parser")
        for tag in soup(["head", "script", "style", "svg", "iframe", "noscript", "path"]):
            tag.decompose()
        dom_cleaned = str(soup)
    except (ImportError, Exception) as exc:
        logger.debug("BeautifulSoup DOM parsing not available or failed (%s), using regex fallback", exc)
        dom_cleaned = re.sub(r"<(head|script|style|svg|iframe|noscript)\b[^>]*>.*?</\1>", "", dom_html, flags=re.S | re.I)

    # Remove empty lines and leading/trailing whitespace
    lines = [line.strip() for line in dom_cleaned.splitlines() if line.strip()]
    dom_cleaned = "\n".join(lines)
    if len(dom_cleaned) > 50000:
        dom_cleaned = dom_cleaned[:50000] + "\n... [truncated]"
    return dom_cleaned

def parse_json_object(raw: str) -> Dict[str, Any] | None:
    if not raw:
        return None
    # Strip markdown code blocks if present
    raw_clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(raw_clean)
    except json.JSONDecodeError:
        # Try finding the first '{' and last '}'
        match = re.search(r"\{.*\}", raw_clean, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return None

def call_exploration_vision(initial_ss_bytes: bytes, post_scroll_ss_bytes: bytes) -> str:
    api_key = os.getenv("LITELLM_API_KEY")
    api_base = os.getenv("LITELLM_API_BASE")
    model_name = os.getenv("VISION_LLM_MODEL") or "openai/claude-haiku-4.5"
    
    # Resize images before sending to Vision API.
    # full_page=True screenshots can be 5000-8000px tall on retail homepages.
    # Cap at 1600×4000 to stay within token limits while keeping enough detail
    # for the LLM to read text and identify promotional elements.
    MAX_WIDTH  = 1600
    MAX_HEIGHT = 4000

    def resize_img(data: bytes) -> bytes:
        try:
            img = Image.open(BytesIO(data))
            w, h = img.width, img.height
            if w > MAX_WIDTH or h > MAX_HEIGHT:
                ratio = min(MAX_WIDTH / w, MAX_HEIGHT / h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            logger.warning("Image resize failed: %s", e)
            return data

    resized_initial = resize_img(initial_ss_bytes)
    resized_post = resize_img(post_scroll_ss_bytes)

    b64_initial = base64.b64encode(resized_initial).decode("utf-8")
    b64_post = base64.b64encode(resized_post).decode("utf-8")

    content = [
        {"type": "text", "text": EXPLORATION_VISUAL_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_initial}"}},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_post}"}},
    ]
    messages = [{"role": "user", "content": content}]

    # 1. Try primary LiteLLM Claude model
    import litellm
    litellm.suppress_debug_info = True

    try:
        logger.info("Attempting LiteLLM Vision call using model=%s", model_name)
        kwargs = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.0,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base
        
        # Try JSON mode
        kwargs["response_format"] = {"type": "json_object"}
        try:
            response = litellm.completion(**kwargs)
        except Exception as json_err:
            logger.warning("JSON mode not supported or failed on LiteLLM: %s. Retrying in normal mode.", json_err)
            kwargs.pop("response_format", None)
            response = litellm.completion(**kwargs)
            
        reply = response.choices[0].message.content
        
        # Extract token usage and log cost
        usage = response.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        total_tokens = getattr(usage, "total_tokens", 0)
        
        # Estimate cost ($1.00 / 1M input, $5.00 / 1M output)
        cost = (prompt_tokens * 1.00 / 1e6) + (completion_tokens * 5.00 / 1e6)
        
        logger.info(
            "LiteLLM Vision SUCCESS: model=%s | prompt_tokens=%d | completion_tokens=%d | total_tokens=%d | cost=$%.6f",
            model_name, prompt_tokens, completion_tokens, total_tokens, cost
        )
        return reply

    except Exception as e:
        logger.warning("LiteLLM Vision call failed: %s. Falling back to direct Gemini.", e)
        
        # 2. Fallback to direct Gemini
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set in environment. Cannot fallback to Gemini.")

        client = genai.Client(api_key=gemini_api_key)
        gemini_model = "gemini-2.5-flash"

        gemini_contents = [
            EXPLORATION_VISUAL_PROMPT,
            genai_types.Part.from_bytes(data=resized_initial, mime_type="image/png"),
            genai_types.Part.from_bytes(data=resized_post, mime_type="image/png"),
        ]

        import time
        for attempt in range(1, 4):
            try:
                logger.info("Calling Gemini Vision Fallback (model=%s) (attempt %d/3)", gemini_model, attempt)
                response = client.models.generate_content(
                    model=gemini_model,
                    contents=gemini_contents,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json"
                    )
                )
                
                # Get usage metadata
                usage = response.usage_metadata
                prompt_tokens = getattr(usage, "prompt_token_count", 0)
                completion_tokens = getattr(usage, "candidates_token_count", 0)
                total_tokens = getattr(usage, "total_token_count", 0)
                
                # Gemini 2.5 Flash pricing: $0.075 / 1M input, $0.30 / 1M output
                cost = (prompt_tokens * 0.075 / 1e6) + (completion_tokens * 0.30 / 1e6)
                
                logger.info(
                    "Gemini Vision Fallback SUCCESS: model=%s | prompt_tokens=%d | completion_tokens=%d | total_tokens=%d | cost=$%.6f",
                    gemini_model, prompt_tokens, completion_tokens, total_tokens, cost
                )
                return response.text
            except Exception as gemini_err:
                logger.warning("Gemini Vision attempt %d failed: %s", attempt, gemini_err)
                if attempt == 3:
                    raise gemini_err
                time.sleep(2 ** attempt)
        return ""

def call_exploration_reasoning(dom_analysis_prompt_text: str) -> str:
    api_key = os.getenv("LITELLM_API_KEY")
    api_base = os.getenv("LITELLM_API_BASE")
    model_name = os.getenv("LLM_MODEL") or "openai/claude-haiku-4.5"
    
    messages = [{"role": "user", "content": dom_analysis_prompt_text}]

    # 1. Try primary LiteLLM model
    import litellm
    litellm.suppress_debug_info = True

    try:
        logger.info("Attempting LiteLLM Reasoning call using model=%s", model_name)
        kwargs = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.0,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base
        
        # Try JSON mode
        kwargs["response_format"] = {"type": "json_object"}
        try:
            response = litellm.completion(**kwargs)
        except Exception as json_err:
            logger.warning("JSON mode not supported or failed on LiteLLM: %s. Retrying in normal mode.", json_err)
            kwargs.pop("response_format", None)
            response = litellm.completion(**kwargs)
            
        reply = response.choices[0].message.content
        
        # Extract token usage and log cost
        usage = response.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        total_tokens = getattr(usage, "total_tokens", 0)
        
        # Estimate cost ($1.00 / 1M input, $5.00 / 1M output)
        cost = (prompt_tokens * 1.00 / 1e6) + (completion_tokens * 5.00 / 1e6)
        
        logger.info(
            "LiteLLM Reasoning SUCCESS: model=%s | prompt_tokens=%d | completion_tokens=%d | total_tokens=%d | cost=$%.6f",
            model_name, prompt_tokens, completion_tokens, total_tokens, cost
        )
        return reply

    except Exception as e:
        logger.warning("LiteLLM Reasoning call failed: %s. Falling back to direct Gemini.", e)
        
        # 2. Fallback to direct Gemini
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set in environment. Cannot fallback to Gemini.")

        client = genai.Client(api_key=gemini_api_key)
        gemini_model = "gemini-2.5-flash"

        import time
        for attempt in range(1, 4):
            try:
                logger.info("Calling Gemini Reasoning Fallback (model=%s) (attempt %d/3)", gemini_model, attempt)
                response = client.models.generate_content(
                    model=gemini_model,
                    contents=dom_analysis_prompt_text,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json"
                    )
                )
                
                # Get usage metadata
                usage = response.usage_metadata
                prompt_tokens = getattr(usage, "prompt_token_count", 0)
                completion_tokens = getattr(usage, "candidates_token_count", 0)
                total_tokens = getattr(usage, "total_token_count", 0)
                
                # Gemini 2.5 Flash pricing: $0.075 / 1M input, $0.30 / 1M output
                cost = (prompt_tokens * 0.075 / 1e6) + (completion_tokens * 0.30 / 1e6)
                
                logger.info(
                    "Gemini Reasoning Fallback SUCCESS: model=%s | prompt_tokens=%d | completion_tokens=%d | total_tokens=%d | cost=$%.6f",
                    gemini_model, prompt_tokens, completion_tokens, total_tokens, cost
                )
                return response.text
            except Exception as gemini_err:
                logger.warning("Gemini Reasoning attempt %d failed: %s", attempt, gemini_err)
                if attempt == 3:
                    raise gemini_err
                time.sleep(2 ** attempt)
        return ""

def explore_site(url: str, brand: str) -> SiteAnalysis:
    logger.info("Starting site exploration for url=%s, brand=%s", url, brand)

    # 1. Playwright Site Visit and Screenshot Capture
    initial_ss_bytes = None
    post_scroll_ss_bytes = None
    dom_html = ""
    resp_status = 200
    resp_headers = {}
    last_exc = None
    
    screenshot_dir = os.path.join(os.getcwd(), "agent_screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    initial_ss_path = os.path.join(screenshot_dir, f"{brand.lower().replace(' ', '_')}_initial.png")
    post_scroll_ss_path = os.path.join(screenshot_dir, f"{brand.lower().replace(' ', '_')}_post_scroll.png")

    with sync_playwright() as pw:
        # Create stealth browser context
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

        # Navigation with retries
        success = False
        response = None
        for attempt in range(1, 4):
            try:
                logger.info("Navigating to %s (attempt %d/3)", url, attempt)
                response = page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                success = True
                break
            except Exception as e:
                logger.warning("Navigation failed on attempt %d: %s", attempt, e)
                last_exc = e
                page.wait_for_timeout(10_000)

        if not success:
            browser.close()
            raise RuntimeError(f"Failed to navigate to {url} after 3 attempts: {last_exc}")

        # Wait for network idle or 8 seconds
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            logger.debug("Network idle wait timed out, continuing...")

        # ── Screenshot 1: Full-page capture immediately after load ──────────────
        # full_page=True captures the ENTIRE page height (not just the viewport).
        # This gives Gemini the complete layout: hero banners, product grids,
        # promotional strips, and footer offers — all in a single image.
        logger.info("Capturing full-page initial screenshot for %s", brand)
        initial_ss_bytes = page.screenshot(full_page=True, timeout=60_000)
        with open(initial_ss_path, "wb") as f:
            f.write(initial_ss_bytes)

        dom_html = page.content()

        if response is not None:
            resp_status = response.status
            resp_headers = dict(response.headers)

        # ── Scroll to trigger lazy-load ──────────────────────────────────────────
        scroll_depth = 3
        for i in range(scroll_depth):
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(2000)

        # Scroll back to top so carousels are in the viewport for cycling.
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)

        # ── Carousel cycling ─────────────────────────────────────────────────────
        # Rotating announcement bars and hero sliders show one slide at a time.
        # We wait 4 seconds between 4 cycles so every slide gets a chance to
        # become the active/visible one before we take the second screenshot.
        # This captures promo text that only appears in later carousel positions.
        logger.info("Cycling carousels for %s (4 cycles × 4s)", brand)
        CAROUSEL_CYCLES = 4
        CAROUSEL_WAIT_MS = 4000
        for cycle in range(CAROUSEL_CYCLES):
            page.wait_for_timeout(CAROUSEL_WAIT_MS)
            # Also try clicking the "next" arrow if it exists — some carousels
            # pause autoplay and only advance on user interaction.
            for next_selector in [
                "[aria-label='Next']",
                "[aria-label='next']",
                ".slick-next",
                ".carousel__next",
                ".owl-next",
                "[class*='carousel'][class*='next']",
                "[class*='slider'][class*='next']",
                "[class*='ann'][class*='next']",
            ]:
                try:
                    btn = page.query_selector(next_selector)
                    if btn and btn.is_visible():
                        btn.click(timeout=1000)
                        logger.debug("Carousel: clicked next arrow (%s) cycle %d", next_selector, cycle + 1)
                        break
                except Exception:
                    pass

        # ── Screenshot 2: Full-page after carousel cycling ────────────────────────
        # By now all carousel slides have been active at least once.
        # The full-page shot shows the page with the last-cycled slide visible.
        logger.info("Capturing full-page post-carousel screenshot for %s", brand)
        post_scroll_ss_bytes = page.screenshot(full_page=True, timeout=60_000)
        with open(post_scroll_ss_path, "wb") as f:
            f.write(post_scroll_ss_bytes)

        # ── Extract Candidate Promotional & Image Banner DOM Elements ─────────
        logger.info("Extracting candidate promotional & image banner DOM elements for %s", brand)
        promo_candidates = []
        try:
            promo_candidates = page.evaluate("""
            () => {
                // Generic structural & numeric deal signals (ZERO hardcoded product or brand words)
                const numeric_deal_regex = /[\\$\\£\\€\\¥]|\\b\\d+[\\s-]*%|\\b(buy|save|get|off|from|with)\\s*(\\d+|\\$\\d+|au\\$\\d+)?\\b/i;
                const ui_structure_regex = /\\b(hero|banner|slide|slider|carousel|card|tile|promo|offer|deal|campaign|announcement|header-bar)\\b/i;
                
                const skip_tags = new Set(['html', 'body', 'main', 'header', 'footer', 'nav', 'script', 'style', 'svg', 'path']);
                
                const allElements = Array.from(document.querySelectorAll('*'));
                const matches = [];

                for (const el of allElements) {
                    const tagName = el.tagName.toLowerCase();
                    if (skip_tags.has(tagName)) continue;

                    // Target leaf/component nodes (ignore large structural wrappers with many children)
                    if (el.children.length > 8) continue;

                    const classStr = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
                    const idStr = el.id || '';
                    const altText = el.getAttribute('alt') || '';
                    const ariaLabel = el.getAttribute('aria-label') || '';
                    const srcStr = el.getAttribute('src') || el.getAttribute('data-src') || '';
                    const innerText = el.innerText?.trim() || '';

                    // Ignore large boilerplate text containers (> 300 chars)
                    if (innerText.length > 300) continue;

                    const combinedSignal = `${innerText} ${altText} ${ariaLabel} ${classStr} ${idStr} ${srcStr}`;

                    const hasDealSignal = numeric_deal_regex.test(combinedSignal);
                    const hasUiStructure = ui_structure_regex.test(classStr + ' ' + idStr);

                    if (!hasDealSignal && !hasUiStructure) continue;

                    const isImageBanner = (tagName === 'img' || tagName === 'picture' || !!altText || !!srcStr || ui_structure_regex.test(classStr + idStr));
                    
                    // Priority scoring based purely on UI/content characteristics (no hardcoded category words)
                    let priorityScore = (innerText.length >= 5 && innerText.length <= 150) ? 10 : 2;
                    if (hasDealSignal) priorityScore += 15;
                    if (isImageBanner) priorityScore += 8;

                    matches.push({
                        tag: tagName,
                        id: idStr,
                        classes: classStr.trim(),
                        type: isImageBanner ? "image_banner" : "text_element",
                        alt_or_aria: (altText || ariaLabel).slice(0, 100),
                        src_snippet: srcStr ? srcStr.split('/').pop().slice(0, 50) : '',
                        text_snippet: innerText.slice(0, 120),
                        score: priorityScore
                    });
                }

                // Sort candidates so the most distinct component elements and banners rank first
                matches.sort((a, b) => b.score - a.score);

                return matches.slice(0, 60);
            }
            """)
        except Exception as cand_err:
            logger.warning("Failed to extract candidate promo elements: %s", cand_err)

        browser.close()

    # 2. Score anti-bot risk
    class MockResponse:
        def __init__(self, status, headers):
            self.status = status
            self.headers = headers
    
    mock_resp = MockResponse(resp_status, resp_headers)
    anti_bot_risk, anti_bot_signals = score_anti_bot_risk(mock_resp, dom_html)
    logger.info("Anti-bot risk: %s (signals: %s)", anti_bot_risk, anti_bot_signals)

    # 3. Vision Call
    visual_raw = call_exploration_vision(initial_ss_bytes, post_scroll_ss_bytes)
    visual_data = parse_json_object(visual_raw)
    if visual_data is None:
        logger.warning("Failed to parse Vision JSON. Response was: %s", visual_raw)
        visual_data = {
            "promotional_areas": [],
            "total_promo_areas_found": 0,
            "dominant_promo_type": "mixed",
            "summary": "Failed to parse visual summary from Vision model"
        }

    # 4. LLM DOM Analysis
    cleaned_dom = clean_dom_regex(dom_html)
    candidate_str = json.dumps(promo_candidates, indent=2) if promo_candidates else "None identified"
    
    dom_analysis_prompt_text = DOM_ANALYSIS_PROMPT.format(
        visual_summary=visual_data.get("summary", ""),
        candidate_elements=candidate_str,
        dom_html=cleaned_dom
    )
    
    dom_analysis_raw = call_exploration_reasoning(dom_analysis_prompt_text)
    dom_analysis_data = parse_json_object(dom_analysis_raw)
    if dom_analysis_data is None:
        logger.warning("Failed to parse DOM analysis JSON. Response was: %s", dom_analysis_raw)
        dom_analysis_data = {
            "extraction_strategy": "hybrid",
            "text_selectors": [],
            "screenshot_selectors": [],
            "notes": "Failed to parse DOM selectors from LLM"
        }

    # 5. Build and return SiteAnalysis
    category_hint = visual_data.get("category_hint", "")
    return SiteAnalysis(
        url=url,
        brand=brand,
        screenshot_path=initial_ss_path,
        dom_html=cleaned_dom,
        extraction_strategy=dom_analysis_data.get("extraction_strategy", "hybrid"),
        promo_areas_identified=visual_data.get("promotional_areas", []),
        has_js_rendering=True,
        has_image_banners=any(area.get("type") == "image" for area in visual_data.get("promotional_areas", [])),
        has_pagination=False,
        anti_bot_signals=anti_bot_signals,
        anti_bot_risk=anti_bot_risk,
        confidence_in_analysis=0.8 if dom_analysis_data.get("text_selectors") or dom_analysis_data.get("screenshot_selectors") else 0.4,
        gemini_visual_summary=visual_data.get("summary", ""),
        notes=dom_analysis_data.get("notes", ""),
        text_selectors=dom_analysis_data.get("text_selectors", []),
        screenshot_selectors=dom_analysis_data.get("screenshot_selectors", []),
        category_hint=category_hint,
    )
