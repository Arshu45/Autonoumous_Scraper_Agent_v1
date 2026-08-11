# tests/agent/test_config_defaults.py
"""
Unit tests verifying exclusion defaults (exclude_selectors and exclude_url_patterns) in generation_agent.py.
"""

import sys
import os
import logging
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.generation_agent import generate_scraper_config
from agent.models import AgentState, SiteAnalysis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_config_exclusion_defaults():
    logger.info("--- Testing Scraper Config Exclusion Defaults ---")

    site_analysis = SiteAnalysis(
        brand="Exclusion Test Brand",
        url="https://example.com",
        screenshot_path="/tmp/test.png",
        dom_html="<div>Test</div>",
        extraction_strategy="hybrid",
        visual_summary="Summary",
        promo_areas=[],
        text_selectors=[".promo-text"],
        screenshot_selectors=[".promo-banner"],
        anti_bot_risk="low",
        notes="Notes"
    )

    state = AgentState(
        brand="Exclusion Test Brand",
        url="https://example.com",
        site_analysis=site_analysis
    )

    mock_response = """
    {
        "brand": "Exclusion Test Brand",
        "source_url": [{"url": "https://example.com", "category_hint": "Footwear"}],
        "spider": "image_promo",
        "extraction_strategy": "hybrid",
        "text_selectors": [".promo-text"],
        "screenshot_selectors": [".promo-banner"],
        "estimated_offer_count": 5,
        "notes": "Generated config test"
    }
    """

    with patch("agent.generation_agent.call_generation_llm", return_value=mock_response):
        new_state = generate_scraper_config(state)

    config = new_state.generated_artifacts.config_json

    assert "exclude_selectors" in config, "exclude_selectors missing from config_json"
    assert "exclude_url_patterns" in config, "exclude_url_patterns missing from config_json"
    
    assert "nav" in config["exclude_selectors"]
    assert "footer" in config["exclude_selectors"]
    assert "/logo" in config["exclude_url_patterns"]
    assert "social" in config["exclude_url_patterns"]

    logger.info("✓ Config exclusion defaults test passed!")

if __name__ == "__main__":
    test_config_exclusion_defaults()
    print("🎉 All config exclusion default tests passed!")
