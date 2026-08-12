# tests/agent/test_exploration_candidates.py
"""
Unit tests for candidate promo element extraction and DOM_ANALYSIS_PROMPT formatting in exploration_agent.py.
"""

import sys
import os
import json
import logging
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.prompts import DOM_ANALYSIS_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_dom_analysis_prompt_formatting():
    logger.info("--- Testing DOM_ANALYSIS_PROMPT Formatting with Candidate Elements ---")
    
    visual_summary = "Hero banner with 50% off sale offer"
    candidate_elements = json.dumps([
        {"tag": "div", "id": "banner-1", "classes": "promo-banner active", "text": "50% OFF ALL ITEMS"},
        {"tag": "span", "id": "", "classes": "discount-badge", "text": "LIMITED TIME DEAL"}
    ], indent=2)
    dom_html = "<div>Mock HTML</div>"

    formatted = DOM_ANALYSIS_PROMPT.format(
        visual_summary=visual_summary,
        candidate_elements=candidate_elements,
        dom_html=dom_html
    )

    assert "Candidate Promotional Elements" in formatted
    assert "promo-banner active" in formatted
    assert "50% OFF ALL ITEMS" in formatted
    logger.info("✓ DOM_ANALYSIS_PROMPT formatting test passed!")

if __name__ == "__main__":
    test_dom_analysis_prompt_formatting()
    print("🎉 Exploration candidate element prompt tests passed!")
