# tests/agent/test_selector_validation.py
"""
Unit tests for the live selector validation feature in generation_agent.py.
"""

import sys
import os
import logging
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.generation_agent import validate_selectors_live

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_selector_validation_pruning():
    logger.info("--- Testing Selector Validation Pruning Logic ---")
    
    config = {
        "text_selectors": [
            ".valid-text",     # count = 5 -> keep
            ".zero-text",      # count = 0 -> prune
            ".broad-text",     # count = 100 -> prune (>50)
        ],
        "screenshot_selectors": [
            ".valid-ss",       # count = 3 -> keep
            ".zero-ss",        # count = 0 -> prune
            ".broad-ss",       # count = 30 -> prune (>20)
        ]
    }

    # Mock Playwright locator count responses
    counts = {
        ".valid-text": 5,
        ".zero-text": 0,
        ".broad-text": 100,
        ".valid-ss": 3,
        ".zero-ss": 0,
        ".broad-ss": 30,
    }

    mock_locator = MagicMock()
    mock_locator.count.side_effect = lambda: counts.get(mock_locator._selector, 0)

    def get_locator(selector):
        loc = MagicMock()
        loc._selector = selector
        loc.count.side_effect = lambda: counts.get(selector, 0)
        return loc

    mock_page = MagicMock()
    mock_page.locator.side_effect = get_locator

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_browser = MagicMock()
    mock_browser.new_context.return_value = mock_context

    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser

    mock_pw_cm = MagicMock()
    mock_pw_cm.__enter__.return_value = mock_pw_instance

    with patch("playwright.sync_api.sync_playwright", return_value=mock_pw_cm):
        result = validate_selectors_live("https://live-target-store.com", config)

    assert result["text_selectors"] == [".valid-text"], f"Expected ['.valid-text'], got {result['text_selectors']}"
    assert result["screenshot_selectors"] == [".valid-ss"], f"Expected ['.valid-ss'], got {result['screenshot_selectors']}"
    logger.info("✓ Selector validation pruning test passed!")

def test_selector_validation_skip_env():
    logger.info("--- Testing Selector Validation Skip via Environment Variable ---")
    config = {
        "text_selectors": [".any-text"],
        "screenshot_selectors": [".any-ss"]
    }
    with patch.dict(os.environ, {"SKIP_LIVE_SELECTOR_VALIDATION": "true"}):
        result = validate_selectors_live("https://live-target-store.com", config)
    assert result["text_selectors"] == [".any-text"]
    assert result["screenshot_selectors"] == [".any-ss"]
    logger.info("✓ Selector validation skip test passed!")

if __name__ == "__main__":
    test_selector_validation_pruning()
    test_selector_validation_skip_env()
    print("🎉 All selector validation tests passed!")
