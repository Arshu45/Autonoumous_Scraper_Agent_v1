"""
Gold Layer — Database Query Functions
Simplified to only fetch extracted promotions.
"""
import os
import sys
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from database.connection import engine

# Rolling window for dashboard queries — prevents unbounded growth.
# Override via .env:  DASHBOARD_LOOKBACK_DAYS=180
DASHBOARD_LOOKBACK_DAYS = int(os.getenv("DASHBOARD_LOOKBACK_DAYS", "90"))


@st.cache_resource(ttl=60)
def get_promotions() -> pd.DataFrame:
    """Fetch recent promotions joined with competitor name and assigned team IDs."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DASHBOARD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    query = text("""
        SELECT 
            p.id,
            p.brand,
            p.offer_title,
            p.category,
            p.source_name,
            p.source_url,
            p.extraction_confidence,
            p.scraped_at,
            p.created_at,
            COALESCE(STRING_AGG(pta.team_id, ','), '') AS team_ids_str
        FROM promotions p
        LEFT JOIN promotion_team_assignments pta ON p.id = pta.promotion_id
        WHERE p.scraped_at >= :cutoff
        GROUP BY p.id
        ORDER BY p.scraped_at DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"cutoff": cutoff})
    if not df.empty:
        df["scraped_at"] = pd.to_datetime(df["scraped_at"])
        df["scraped_date"] = df["scraped_at"].dt.date
        df["team_ids_str"] = df["team_ids_str"].fillna("")
    else:
        df["scraped_date"] = pd.Series(dtype="object")
        df["team_ids_str"] = pd.Series(dtype="str")
    return df

