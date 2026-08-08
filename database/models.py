from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text,
    Boolean, DateTime, ForeignKey
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class Competitor(Base):
    """
    Represents the retail brands we are tracking.
    e.g., David Jones, The Iconic, Forever New
    """
    __tablename__ = 'competitors'

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(100), unique=True, nullable=False)
    enabled     = Column(Boolean, default=True, nullable=False)
    added_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    modified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Agent-generated columns (Migration C)
    extraction_strategy = Column(String(20), default='hybrid')
    agent_generated     = Column(Boolean, default=False)
    agent_confidence    = Column(Integer, nullable=True)
    agent_notes         = Column(Text, nullable=True)
    source_url          = Column(Text, nullable=True)

    # Relationships
    promotions  = relationship("Promotion", back_populates="competitor", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Competitor(id={self.id}, name='{self.name}')>"


class Promotion(Base):
    """
    Core table. Every scraped offer with raw fields.
    The offer_hash (SHA-256) ensures deduplication across runs.
    """
    __tablename__ = 'promotions'

    id              = Column(Integer, primary_key=True, autoincrement=True)
    competitor_id   = Column(Integer, ForeignKey('competitors.id', ondelete='CASCADE'), nullable=False)
    brand           = Column(String(100), nullable=False)

    # Raw scraped content
    offer_title     = Column(Text, nullable=False)
    category        = Column(String(100))

    # Source tracking
    source_name            = Column(String(50), nullable=False)
    source_url             = Column(Text)
    extraction_confidence  = Column(String(10))   # "high" | "medium" | "low" — set by Vision LLM

    # Deduplication fingerprint: SHA-256 of (source_name + competitor.name + offer_title)
    offer_hash      = Column(String(64), unique=True, nullable=False)

    # Timestamps
    scraped_at      = Column(DateTime, nullable=False)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    competitor       = relationship("Competitor", back_populates="promotions")
    team_assignments = relationship("PromotionTeamAssignment", back_populates="promotion", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Promotion(id={self.id}, brand='{self.brand}', title='{self.offer_title[:40]}...')>"


class AgentRunOutcome(Base):
    """
    Records the outcome of each agent pipeline run (initial validation and health checks).
    run_type: 'initial_validation' | 'health_check'
    still_healthy_at_check is NULL until a health check fills it in.
    """
    __tablename__ = 'agent_run_outcomes'

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    brand                   = Column(String(255), nullable=True)
    run_type                = Column(String(20), nullable=True)      # 'initial_validation' | 'health_check'
    confidence_score        = Column(Integer, nullable=True)
    score_breakdown         = Column(JSONB, nullable=True)
    recommendation          = Column(String(20), nullable=True)
    offers_extracted        = Column(Integer, nullable=True)
    was_auto_approved       = Column(Boolean, nullable=True)
    days_since_registration = Column(Integer, nullable=True)
    still_healthy_at_check  = Column(Boolean, nullable=True)         # NULL until health check fills in
    checked_at              = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AgentRunOutcome(id={self.id}, brand='{self.brand}', run_type='{self.run_type}')>"


class AgentAuditLog(Base):
    """
    Immutable audit trail for all agent actions.
    action values: 'trigger_run' | 'approve' | 'reject' | 'edit_config'
    details: config diff, rejection reason, etc. (JSONB)
    """
    __tablename__ = 'agent_audit_log'

    id         = Column(Integer, primary_key=True, autoincrement=True)
    brand      = Column(String(255), nullable=False)
    user_id    = Column(String(255), nullable=False)
    action     = Column(String(50), nullable=False)
    details    = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AgentAuditLog(id={self.id}, brand='{self.brand}', action='{self.action}')>"


class PrefectTargetRegistry(Base):
    """
    Registry of brands that are actively registered with Prefect for scraping.
    registered_by references the user_id from the audit log.
    """
    __tablename__ = 'prefect_target_registry'

    id            = Column(Integer, primary_key=True, autoincrement=True)
    brand         = Column(String(255), unique=True, nullable=True)
    config_path   = Column(String(500), nullable=True)
    enabled       = Column(Boolean, default=True)
    registered_at = Column(DateTime, default=datetime.utcnow)
    registered_by = Column(String(255), nullable=True)

    def __repr__(self):
        return f"<PrefectTargetRegistry(id={self.id}, brand='{self.brand}', enabled={self.enabled})>"


class PromotionTeamAssignment(Base):
    """
    Junction table recording team visibility for promotions.
    A promotion can be assigned to multiple business teams based on policy rules.
    """
    __tablename__ = 'promotion_team_assignments'

    id             = Column(Integer, primary_key=True, autoincrement=True)
    promotion_id   = Column(Integer, ForeignKey('promotions.id', ondelete='CASCADE'), nullable=False)
    team_id        = Column(String(50), nullable=False)
    assigned_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationship
    promotion      = relationship("Promotion", back_populates="team_assignments")

    def __repr__(self):
        return f"<PromotionTeamAssignment(promotion_id={self.promotion_id}, team_id='{self.team_id}')>"

