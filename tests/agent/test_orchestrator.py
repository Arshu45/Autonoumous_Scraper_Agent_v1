# tests/agent/test_orchestrator.py

import logging
import sys
import os
import json
from unittest.mock import patch

# Ensure the root of the project is in the python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent import build_agent_graph, AgentState, ValidationReport
from agent.models import SiteAnalysis, GeneratedArtifacts

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create a mock explore_site function that doesn't hit external APIs
def get_mock_site_analysis(url, brand):
    return SiteAnalysis(
        url=url,
        brand=brand,
        screenshot_path="mock_screenshot.png",
        dom_html="<html><body>Mock DOM</body></html>",
        extraction_strategy="text",
        anti_bot_signals={},
        anti_bot_risk="low",
        text_selectors=[".mock-text"],
        screenshot_selectors=[".mock-ss"]
    )

def get_mock_generated_artifacts(state):
    state.generated_artifacts = GeneratedArtifacts(
        brand=state.brand,
        config_json={"brand": state.brand, "source_url": state.url, "spider": "image_promo", "enabled": True},
        estimated_offer_count=5,
        generation_notes="Mocked generation"
    )
    state.status = "generation"
    return state

@patch('agent.exploration_agent.explore_site', side_effect=get_mock_site_analysis)
@patch('agent.generation_agent.generate_scraper_config', side_effect=get_mock_generated_artifacts)
@patch('agent.registration_agent.run_registration', side_effect=lambda s: (setattr(s, 'status', 'registered') or s))
def test_auto_approve(mock_reg, mock_gen, mock_explore):
    """Verifies the auto-approve routing path reaches the registration node.
    DB persistence is tested separately in test_registration.py."""
    logger.info("--- Testing Auto-Approve Route (Score: 95, no violations) ---")
    graph = build_agent_graph()
    
    state = AgentState(
        url="https://example.com",
        brand="Test Brand",
        requirements="Test requirements"
    )
    
    state.validation_report = ValidationReport(
        brand="Test Brand",
        scraper_ran=True,
        offers_extracted=10,
        schema_valid=True,
        confidence_score=95,
        recommendation="auto_approve",
        sandbox_violations=[]
    )
    
    final_state = graph.invoke(state)
    logger.info("Final status: %s", final_state["status"])
    assert final_state["status"] == "registered", f"Expected 'registered', got {final_state['status']}"
    logger.info("✓ Auto-approve test passed!")

@patch('agent.exploration_agent.explore_site', side_effect=get_mock_site_analysis)
@patch('agent.generation_agent.generate_scraper_config', side_effect=get_mock_generated_artifacts)
def test_pending_review(mock_gen, mock_explore):
    logger.info("--- Testing Pending Review Route (Score: 80, no violations) ---")
    graph = build_agent_graph()
    
    state = AgentState(
        url="https://example.com",
        brand="Test Brand",
        requirements="Test requirements"
    )
    
    state.validation_report = ValidationReport(
        brand="Test Brand",
        scraper_ran=True,
        offers_extracted=5,
        schema_valid=True,
        confidence_score=80,
        recommendation="pending",
        sandbox_violations=[]
    )
    
    final_state = graph.invoke(state)
    logger.info("Final status: %s", final_state["status"])
    assert final_state["status"] == "validation", f"Expected 'validation', got {final_state['status']}"
    logger.info("✓ Pending review test passed!")

@patch('agent.exploration_agent.explore_site', side_effect=get_mock_site_analysis)
@patch('agent.generation_agent.generate_scraper_config', side_effect=get_mock_generated_artifacts)
def test_reject_low_score(mock_gen, mock_explore):
    logger.info("--- Testing Reject Route (Score: 50, no violations) ---")
    graph = build_agent_graph()
    
    state = AgentState(
        url="https://example.com",
        brand="Test Brand",
        requirements="Test requirements"
    )
    
    state.validation_report = ValidationReport(
        brand="Test Brand",
        scraper_ran=True,
        offers_extracted=2,
        schema_valid=False,
        confidence_score=50,
        recommendation="reject",
        sandbox_violations=[]
    )
    
    final_state = graph.invoke(state)
    logger.info("Final status: %s", final_state["status"])
    assert final_state["status"] == "validation", f"Expected 'validation', got {final_state['status']}"
    logger.info("✓ Reject low score test passed!")

@patch('agent.exploration_agent.explore_site', side_effect=get_mock_site_analysis)
@patch('agent.generation_agent.generate_scraper_config', side_effect=get_mock_generated_artifacts)
def test_reject_sandbox_violations(mock_gen, mock_explore):
    logger.info("--- Testing Reject Route (Score: 95, with sandbox violations) ---")
    graph = build_agent_graph()
    
    state = AgentState(
        url="https://example.com",
        brand="Test Brand",
        requirements="Test requirements"
    )
    
    state.validation_report = ValidationReport(
        brand="Test Brand",
        scraper_ran=True,
        offers_extracted=10,
        schema_valid=True,
        confidence_score=95,
        recommendation="reject",
        sandbox_violations=["attempted_write_to_etc"]
    )
    
    final_state = graph.invoke(state)
    logger.info("Final status: %s", final_state["status"])
    assert final_state["status"] == "validation", f"Expected 'validation', got {final_state['status']}"
    logger.info("✓ Reject sandbox violations test passed!")

def test_generation_agent_node():
    logger.info("--- Testing Generation Agent Node Logic ---")
    from agent.generation_agent import generate_scraper_config
    
    state = AgentState(
        url="https://example.com",
        brand="Test Brand Unit",
        requirements="Custom requirements"
    )
    state.site_analysis = get_mock_site_analysis(state.url, state.brand)
    
    mock_llm_response = {
        "brand": "Test Brand Unit",
        "source_url": "https://example.com",
        "spider": "image_promo",
        "extraction_strategy": "text",
        "text_selectors": [".mock-text", ".extra-promo"],
        "screenshot_selectors": [".mock-ss"],
        "min_image_width": 400,
        "min_image_height": 150,
        "min_aspect_ratio": 1.2,
        "request_delay_seconds": 4,
        "scroll_depth": 2,
        "enabled": True,
        "estimated_offer_count": 8,
        "generation_notes": "Valid config generated by mock LLM"
    }
    
    # Save snake case filename for verification/cleanup anchored to project root
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    expected_config_file = os.path.join(project_root, "config", "targets", "test_brand_unit.json")
    if os.path.exists(expected_config_file):
        os.remove(expected_config_file)
        
    try:
        with patch('agent.generation_agent.call_generation_llm', return_value=json.dumps(mock_llm_response)):
            final_state = generate_scraper_config(state)
            
        assert final_state.status == "generated", f"Expected status 'generated', got {final_state.status}"
        assert final_state.generated_artifacts is not None, "generated_artifacts should not be None"
        
        artifacts = final_state.generated_artifacts
        assert artifacts.brand == "Test Brand Unit"
        assert artifacts.estimated_offer_count == 8
        assert artifacts.generation_notes == "Valid config generated by mock LLM"
        
        # Check config_json structure
        cfg = artifacts.config_json
        assert cfg["brand"] == "Test Brand Unit"
        assert cfg["spider"] == "image_promo"
        assert cfg["extraction_strategy"] == "text"
        assert ".extra-promo" in cfg["text_selectors"]
        assert cfg["enabled"] is True
        
        # Verify file is written
        assert os.path.exists(expected_config_file), f"Expected config file {expected_config_file} to exist"
        
        with open(expected_config_file, "r") as f:
            saved_cfg = json.load(f)
        assert saved_cfg["brand"] == "Test Brand Unit"
    finally:
        # Cleanup
        if os.path.exists(expected_config_file):
            os.remove(expected_config_file)
            
    logger.info("✓ Generation agent node unit test passed!")

if __name__ == "__main__":
    try:
        test_auto_approve()
        test_pending_review()
        test_reject_low_score()
        test_reject_sandbox_violations()
        test_generation_agent_node()
        logger.info("\n🎉 All orchestrator and generation agent tests passed successfully!")
    except AssertionError as e:
        logger.error("❌ Test failed: %s", e)
        sys.exit(1)
