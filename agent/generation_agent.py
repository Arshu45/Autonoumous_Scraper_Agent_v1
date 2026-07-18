# agent/generation_agent.py

import os
import re
import json
import logging
import time
import litellm
import google.genai as genai
from google.genai import types as genai_types
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from agent.models import AgentState, GeneratedArtifacts
from agent.prompts import CONFIG_GENERATION_PROMPT
from agent.exploration_agent import parse_json_object

logger = logging.getLogger(__name__)

def call_generation_llm(prompt_text: str) -> str:
    """
    Calls the LLM using the LiteLLM client with direct Gemini fallback.
    Tracks and logs token usage and USD costs.
    """
    api_key = os.getenv("LITELLM_API_KEY")
    api_base = os.getenv("LITELLM_API_BASE")
    model_name = os.getenv("LLM_MODEL") or "openai/claude-haiku-4.5"
    
    messages = [{"role": "user", "content": prompt_text}]
    litellm.suppress_debug_info = True

    # 1. Try primary LiteLLM model
    try:
        logger.info("Attempting LiteLLM Generation call using model=%s", model_name)
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
            "LiteLLM Generation SUCCESS: model=%s | prompt_tokens=%d | completion_tokens=%d | total_tokens=%d | cost=$%.6f",
            model_name, prompt_tokens, completion_tokens, total_tokens, cost
        )
        return reply

    except Exception as e:
        logger.warning("LiteLLM Generation call failed: %s. Falling back to direct Gemini.", e)
        
        # 2. Fallback to direct Gemini
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set in environment. Cannot fallback to Gemini.")

        client = genai.Client(api_key=gemini_api_key)
        gemini_model = "gemini-2.5-flash"

        for attempt in range(1, 4):
            try:
                logger.info("Calling Gemini Generation Fallback (model=%s) (attempt %d/3)", gemini_model, attempt)
                response = client.models.generate_content(
                    model=gemini_model,
                    contents=prompt_text,
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
                    "Gemini Generation Fallback SUCCESS: model=%s | prompt_tokens=%d | completion_tokens=%d | total_tokens=%d | cost=$%.6f",
                    gemini_model, prompt_tokens, completion_tokens, total_tokens, cost
                )
                return response.text
            except Exception as gemini_err:
                logger.warning("Gemini Generation attempt %d failed: %s", attempt, gemini_err)
                if attempt == 3:
                    raise gemini_err
                time.sleep(2 ** attempt)
        return ""

def to_snake_case(name: str) -> str:
    """Helper to convert brand names to snake_case."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

def is_valid_css_selector(selector: str) -> bool:
    """Filter out jQuery-style pseudo-classes or others that cause syntax errors in Playwright."""
    if not selector or not isinstance(selector, str):
        return False
    invalid_patterns = [":contains", ":has", ":first", ":last", ":eq", ":nth"]
    for pat in invalid_patterns:
        if pat in selector:
            return False
    return True

def clean_selectors(selectors) -> list[str]:
    if not selectors or not isinstance(selectors, list):
        return []
    cleaned = []
    for sel in selectors:
        if isinstance(sel, str):
            sel_strip = sel.strip()
            if is_valid_css_selector(sel_strip):
                cleaned.append(sel_strip)
    return cleaned

def generate_scraper_config(state: AgentState) -> AgentState:
    """
    Generation Agent node: Takes exploration results and generates a scraper
    configuration JSON using LLM with fallback, saving it to config/targets/.
    """
    logger.info("Starting scraper configuration generation for brand=%s", state.brand)
    state.status = "generation"
    
    site_analysis = state.site_analysis
    if not site_analysis:
        error_msg = "Cannot run generation agent: site_analysis is missing."
        logger.error(error_msg)
        state.status = "failed"
        state.error = error_msg
        return state

    # Format the prompt using SiteAnalysis details
    # Combine text and screenshot selectors into a readable JSON string
    selectors_info = {
        "text_selectors": getattr(site_analysis, "text_selectors", []),
        "screenshot_selectors": getattr(site_analysis, "screenshot_selectors", [])
    }
    
    prompt_text = CONFIG_GENERATION_PROMPT.format(
        url=site_analysis.url,
        brand=site_analysis.brand,
        extraction_strategy=site_analysis.extraction_strategy,
        visual_summary=site_analysis.gemini_visual_summary,
        promo_areas=json.dumps(site_analysis.promo_areas_identified, indent=2),
        selectors=json.dumps(selectors_info, indent=2),
        anti_bot_risk=site_analysis.anti_bot_risk,
        notes=site_analysis.notes,
        requirements=state.requirements or "None provided"
    )

    try:
        raw_response = call_generation_llm(prompt_text)
        parsed_data = parse_json_object(raw_response)
        
        if not parsed_data:
            raise ValueError(f"Failed to parse LLM generation response: {raw_response}")
        
        # Robustly extract the config JSON and metadata
        config_json = {}
        # If the LLM returned the config inside a nested key:
        if "config" in parsed_data and isinstance(parsed_data["config"], dict):
            config_json = parsed_data["config"]
        elif "config_json" in parsed_data and isinstance(parsed_data["config_json"], dict):
            config_json = parsed_data["config_json"]
        else:
            # Assume the top level is the config itself
            config_json = parsed_data.copy()
            # Clean up non-config keys from config_json if present
            config_json.pop("estimated_offer_count", None)
            config_json.pop("generation_notes", None)
            config_json.pop("extraction_risks", None)
            config_json.pop("notes", None)

        # Extract extra fields
        estimated_offer_count = parsed_data.get("estimated_offer_count") or parsed_data.get("estimated_offers") or 0
        try:
            estimated_offer_count = int(estimated_offer_count)
        except (ValueError, TypeError):
            estimated_offer_count = 0
            
        generation_notes = parsed_data.get("generation_notes") or parsed_data.get("extraction_risks") or parsed_data.get("notes") or ""
        if isinstance(generation_notes, list):
            generation_notes = " | ".join(generation_notes)

        # Ensure all standard config fields are present and cleaned
        config_json["brand"] = config_json.get("brand") or site_analysis.brand
        config_json["source_url"] = config_json.get("source_url") or site_analysis.url
        config_json["spider"] = "image_promo"
        config_json["extraction_strategy"] = config_json.get("extraction_strategy") or site_analysis.extraction_strategy or "hybrid"
        
        # Clean text and screenshot selectors
        text_sels = config_json.get("text_selectors") or selectors_info["text_selectors"]
        config_json["text_selectors"] = clean_selectors(text_sels)
        
        ss_sels = config_json.get("screenshot_selectors") or selectors_info["screenshot_selectors"]
        config_json["screenshot_selectors"] = clean_selectors(ss_sels)

        config_json.setdefault("min_image_width", 400)
        config_json.setdefault("min_image_height", 150)
        config_json.setdefault("min_aspect_ratio", 1.2)
        config_json.setdefault("request_delay_seconds", 4)
        config_json.setdefault("scroll_depth", 2)
        config_json.setdefault("enabled", True)

        # Create GeneratedArtifacts
        artifacts = GeneratedArtifacts(
            brand=site_analysis.brand,
            config_json=config_json,
            estimated_offer_count=estimated_offer_count,
            generation_notes=str(generation_notes)
        )
        state.generated_artifacts = artifacts
        
        # Save to config/targets/<brand_name_snake_case>.json
        brand_snake = to_snake_case(site_analysis.brand)
        config_dir = os.path.join(os.getcwd(), "config", "targets")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, f"{brand_snake}.json")
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_json, f, indent=2)
            
        logger.info("Successfully generated and saved scraper configuration to %s", config_path)
        
    except Exception as e:
        logger.exception("Generation agent failed on brand=%s", state.brand)
        state.status = "failed"
        state.error = str(e)

    return state
