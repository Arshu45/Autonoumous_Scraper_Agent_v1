# agent/models.py

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class SiteAnalysis(BaseModel):
    url: str
    brand: str
    screenshot_path: str
    dom_html: str
    extraction_strategy: str  # text, screenshot, image, hybrid
    promo_areas_identified: List[Dict[str, Any]] = Field(default_factory=list)
    has_js_rendering: bool = False
    has_image_banners: bool = False
    has_pagination: bool = False
    anti_bot_signals: Dict[str, Any] = Field(default_factory=dict)
    anti_bot_risk: str = "low"  # low, medium, high
    confidence_in_analysis: float = 0.0
    gemini_visual_summary: str = ""
    notes: str = ""
    text_selectors: List[str] = Field(default_factory=list)
    screenshot_selectors: List[str] = Field(default_factory=list)

class GeneratedArtifacts(BaseModel):
    brand: str
    config_json: Dict[str, Any] = Field(default_factory=dict)
    scraper_code: Optional[str] = None
    test_assertions: List[str] = Field(default_factory=list)
    estimated_offer_count: int = 0
    generation_notes: str = ""

class ValidationReport(BaseModel):
    brand: str
    scraper_ran: bool = False
    offers_extracted: int = 0
    schema_valid: bool = False
    schema_errors: List[str] = Field(default_factory=list)
    confidence_score: int = 0
    score_breakdown: Dict[str, Any] = Field(default_factory=dict)
    issues: List[str] = Field(default_factory=list)
    sample_offers: List[Dict[str, Any]] = Field(default_factory=list)
    recommendation: str = "reject"  # auto_approve, pending, reject
    sandbox_violations: List[str] = Field(default_factory=list)

class AgentState(BaseModel):
    url: str
    brand: str
    requirements: str = ""
    site_analysis: Optional[SiteAnalysis] = None
    generated_artifacts: Optional[GeneratedArtifacts] = None
    validation_report: Optional[ValidationReport] = None
    status: str = "init"  # init, exploration, generation, validation, registered, failed, rejected
    error: Optional[str] = None
    is_repair_run: bool = False
