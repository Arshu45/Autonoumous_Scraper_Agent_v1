import streamlit as st
import pandas as pd
import datetime
import plotly.express as px
import os
import sys

from html import escape

# Add root folder to python path for importing connection
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.db import get_promotions
from utils.styles import apply_css, page_header, section_label
from utils.exporter import export_to_excel
from services.team_policy_engine import TeamPolicyEngine

st.set_page_config(
    page_title="Myer Competitor Analysis",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_css()

# ── Load Team Engine ──────────────────────────────────────────────────
policy_engine = TeamPolicyEngine()
team_map = {t["team_id"]: t["team_name"] for t in policy_engine.teams}
team_map["unassigned"] = "Unassigned / General"

all_team_ids = list(team_map.keys())

# Build once: team_id → configured allowed brands from teams.json
# Used in both the matrix renderer and Excel exporter.
allowed_brands_by_team = {
    t["team_id"]: [b for b in (t.get("allowed_brands") or []) if b]
    for t in policy_engine.teams
}

# ── Helpers ──────────────────────────────────────────────────────────
def render_weekly_matrix(matrix: pd.DataFrame, brand_column: str) -> str:
    """Render the weekly pivot table as a styled HTML table."""
    days = [c for c in matrix.columns if c != brand_column]
    header_cells = f'<th class="wm-brand-th">{escape(brand_column)}</th>'
    for day in days:
        header_cells += f'<th class="wm-day-th">{escape(day)}</th>'

    rows_html = ""
    for _, row in matrix.iterrows():
        brand_val = escape(str(row[brand_column]))
        cells = f'<td class="wm-brand-td">{brand_val}</td>'
        for day in days:
            raw = row[day]
            if pd.isna(raw) or str(raw).strip() == "":
                cells += '<td class="wm-promo-td wm-empty">—</td>'
            else:
                lines = [escape(l) for l in str(raw).split("\n") if l.strip()]
                bullets = "".join(f'<div class="wm-bullet">{l}</div>' for l in lines)
                cells += f'<td class="wm-promo-td">{bullets}</td>'
        rows_html += f"<tr>{cells}</tr>"

    return f"""
    <div class="wm-scroll">
      <table class="wm-table">
        <thead><tr>{header_cells}</tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>"""

# ── Sidebar Filters ──────────────────────────────────────────────────
st.sidebar.markdown("""
<div style="padding: 0.5rem 0 1rem 0;">
    <div style="font-size: 0.7rem; font-weight: 600; color: #A0A0B0; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.5rem;">Platform</div>
    <div style="font-size: 1rem; font-weight: 600; color: #1A1A2E; letter-spacing: -0.01em;">Market Intelligence</div>
</div>
<hr style="border-color: #E0E0EA; margin: 0 0 1.5rem 0;">
""", unsafe_allow_html=True)

st.sidebar.markdown('<div class="section-label">Filters</div>', unsafe_allow_html=True)

# Fetch promotions DataFrame
df = get_promotions()

if df.empty:
    page_header("Promotional Intelligence", "Real-time competitor promotion monitoring.")
    st.warning("⚠️ No promotions found in the database. Please initialize the DB and run the scraper first!")
    st.info("Run `python scripts/init_db.py` then `python flows/master_pipeline.py` to get started.")
    st.stop()

# Normalize category labels
df["category"] = df["category"].fillna("Uncategorized").replace("", "Uncategorized")

# Helper function for team display names
def get_team_names_display(team_ids_str):
    if not team_ids_str or pd.isna(team_ids_str) or not str(team_ids_str).strip():
        return "Unassigned / General"
    tids = [t.strip() for t in str(team_ids_str).split(",") if t.strip()]
    return ", ".join([team_map.get(tid, tid) for tid in tids])

df["team_names_str"] = df["team_ids_str"].apply(get_team_names_display)

# 1. Business Team Filter
selected_teams = st.sidebar.multiselect(
    "Select Business Teams",
    options=all_team_ids,
    default=all_team_ids,
    format_func=lambda tid: team_map.get(tid, tid) if tid == "unassigned" else f"{team_map.get(tid, tid)} Team"
)

# 2. Category Filter
all_categories = sorted(df["category"].dropna().unique().tolist())
selected_categories = st.sidebar.multiselect("Select Categories", all_categories, default=all_categories)

# 3. Brand Filter
all_brands = sorted(df["brand"].dropna().unique().tolist())
selected_brands = st.sidebar.multiselect("Select Brands", all_brands, default=all_brands)

# 4. Date Filter
min_date = df["scraped_date"].min()
max_date = df["scraped_date"].max()

start_date = st.sidebar.date_input("Start Date", min_date)
end_date = st.sidebar.date_input("End Date", max_date)

# 5. Source Filter
all_sources = sorted(df["source_name"].dropna().unique().tolist())
selected_sources = st.sidebar.multiselect("Select Extraction Sources", all_sources, default=all_sources)

# Apply team filter condition
def matches_selected_teams(team_ids_str):
    if not team_ids_str or pd.isna(team_ids_str) or not str(team_ids_str).strip():
        return "unassigned" in selected_teams
    tids = [t.strip() for t in str(team_ids_str).split(",") if t.strip()]
    return any(tid in selected_teams for tid in tids)

team_mask = df["team_ids_str"].apply(matches_selected_teams)

# Apply all combined filters
filtered_df = df[
    (team_mask) &
    (df["category"].isin(selected_categories)) &
    (df["brand"].isin(selected_brands)) &
    (df["scraped_date"] >= start_date) &
    (df["scraped_date"] <= end_date) &
    (df["source_name"].isin(selected_sources))
]

# ── Main Content ──
page_header("Myer Competitor Analysis", "Team-wise & category-wise promotional feeds and competitive intelligence.")

# Metrics
st.markdown('<div class="section-label">Feed Metrics</div>', unsafe_allow_html=True)
col1, col2, col3 = st.columns(3)

with col1:
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Total Extracted Promotions</div>
        <div class="kpi-value">{len(filtered_df)}</div>
        <div class="kpi-sub">matching active filters</div>
    </div>
    """, unsafe_allow_html=True)

with col2:
    active_brands_count = filtered_df["brand"].nunique()
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Active Competitor Brands</div>
        <div class="kpi-value">{active_brands_count}</div>
        <div class="kpi-sub">monitored in active feeds</div>
    </div>
    """, unsafe_allow_html=True)

with col3:
    today = datetime.date.today()
    promos_today = len(filtered_df[filtered_df["scraped_date"] == today])
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Scraped Today</div>
        <div class="kpi-value">{promos_today}</div>
        <div class="kpi-sub">new offers scraped today</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<hr>", unsafe_allow_html=True)

# ── Visualizations ──
if not filtered_df.empty:
    viz_col1, viz_col2 = st.columns(2)
    
    with viz_col1:
        st.markdown('<div class="section-label">Promotions by Brand</div>', unsafe_allow_html=True)
        brand_counts = filtered_df["brand"].value_counts().reset_index()
        brand_counts.columns = ["Brand", "Promotions"]
        fig_brand = px.bar(
            brand_counts,
            x="Promotions",
            y="Brand",
            orientation="h",
            color="Brand",
            color_discrete_sequence=px.colors.qualitative.Safe,
            text_auto=True
        )
        fig_brand.update_layout(
            showlegend=False,
            margin=dict(l=20, r=20, t=10, b=20),
            height=280,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=True, gridcolor="#EBEBF2"),
            yaxis=dict(showgrid=False)
        )
        st.plotly_chart(fig_brand, width="stretch")
        
    with viz_col2:
        st.markdown('<div class="section-label">Extraction Timeline</div>', unsafe_allow_html=True)
        timeline = filtered_df.groupby("scraped_date").size().reset_index()
        timeline.columns = ["Date", "Offers"]
        timeline["Date"] = timeline["Date"].astype(str)
        fig_time = px.line(
            timeline,
            x="Date",
            y="Offers",
            markers=True
        )
        fig_time.update_traces(line_color="#5B50E8", line_width=2.5, marker=dict(size=6))
        fig_time.update_layout(
            margin=dict(l=20, r=20, t=10, b=20),
            height=280,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=True, gridcolor="#EBEBF2"),
            yaxis=dict(showgrid=True, gridcolor="#EBEBF2")
        )
        st.plotly_chart(fig_time, width="stretch")

st.markdown("<hr>", unsafe_allow_html=True)

# ── Team-wise Weekly Competitor Matrix ──
if filtered_df.empty:
    st.markdown('<div class="section-label">Weekly Competitor Matrix</div>', unsafe_allow_html=True)
    st.info("No promotions available for the selected filters.")
else:
    col_lbl, col_btn = st.columns([3, 1], vertical_alignment="center")
    with col_lbl:
        st.markdown('<div class="section-label" style="margin-top:0; margin-bottom:0;">Weekly Competitor Matrix (Team View)</div>', unsafe_allow_html=True)
    with col_btn:
        excel_data = export_to_excel(filtered_df, selected_teams, team_map, allowed_brands_by_team)
        st.download_button(
            label="📥 Export Matrix to Excel",
            data=excel_data,
            file_name=f"weekly_team_matrix_{datetime.date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch"
        )
    st.markdown('<div style="margin-top: 0.5rem;"></div>', unsafe_allow_html=True)

    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Render Matrix per Business Team
    for team_id in selected_teams:
        team_name = team_map.get(team_id, team_id)

        if team_id == "unassigned":
            team_df = filtered_df[
                filtered_df["team_ids_str"].apply(lambda s: not s or pd.isna(s) or not str(s).strip())
            ].copy()
            configured_brands = []  # unassigned has no fixed brand list
        else:
            team_df = filtered_df[
                filtered_df["team_ids_str"].apply(lambda s: team_id in [t.strip() for t in str(s).split(",") if t.strip()])
            ].copy()
            configured_brands = allowed_brands_by_team.get(team_id, [])

        # Always render — even if team_df is empty, we still show configured brands
        brand_column = f"{team_name} Brand"

        if team_df.empty:
            # Build an all-empty matrix from configured brands only
            empty_rows = [{brand_column: brand, **{day: "" for day in weekday_order}} for brand in configured_brands]
            matrix = pd.DataFrame(empty_rows) if empty_rows else pd.DataFrame(columns=[brand_column] + weekday_order)
        else:
            team_df["Day"] = team_df["scraped_at"].dt.day_name()

            grouped = (
                team_df.groupby(["brand", "Day"])["offer_title"]
                .apply(lambda values: "\n".join(dict.fromkeys(v for v in values if isinstance(v, str) and v.strip())))
                .reset_index()
            )
            matrix = grouped.pivot(index="brand", columns="Day", values="offer_title")
            matrix = matrix.reindex(columns=weekday_order).fillna("")
            matrix = matrix.reset_index().rename(columns={"brand": brand_column})

            # Inject any configured brands that had zero scraped offers this period
            scraped_brands = set(matrix[brand_column].tolist())
            missing_brands = [b for b in configured_brands if b not in scraped_brands]
            if missing_brands:
                missing_rows = pd.DataFrame(
                    [{brand_column: brand, **{day: "" for day in weekday_order}} for brand in missing_brands]
                )
                matrix = pd.concat([matrix, missing_rows], ignore_index=True)

        # Sort brands alphabetically for consistent display
        matrix = matrix.sort_values(brand_column).reset_index(drop=True)

        n_brands = matrix.shape[0]
        n_promos = team_df["offer_title"].nunique() if not team_df.empty else 0

        # Team feed header badge
        st.markdown(f"""
        <div class="wm-cat-header">
            <span class="wm-cat-title">🏬 {escape(team_name)}</span>
            <span class="wm-badge wm-badge-blue">{n_brands} brand{'s' if n_brands != 1 else ''}</span>
            <span class="wm-badge wm-badge-purple">{n_promos} unique offer{'s' if n_promos != 1 else ''}</span>
        </div>
        """, unsafe_allow_html=True)

        with st.expander(f"View {team_name} Matrix", expanded=True):
            st.markdown(render_weekly_matrix(matrix, brand_column), unsafe_allow_html=True)

st.markdown("<hr>", unsafe_allow_html=True)

# ── Extracted Promotions Table ──
st.markdown('<div class="section-label">Extracted Promotions Table</div>', unsafe_allow_html=True)

if filtered_df.empty:
    st.info("No promotions match the selected filters.")
else:
    # Format table for display with assigned team feeds and AI categories
    display_df = filtered_df[[
        "brand", "team_names_str", "category", "source_name", "offer_title", "source_url", "extraction_confidence", "scraped_at"
    ]].copy()

    def format_confidence(val):
        """Safely format confidence values — handles both numeric ('0.95') and text ('high') forms."""
        if pd.isna(val) or val == "" or val is None:
            return "—"
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val).capitalize()

    display_df["extraction_confidence"] = display_df["extraction_confidence"].apply(format_confidence)
    display_df.columns = ["Brand", "Assigned Team Feeds", "AI Category", "Source Strategy", "Offer Title", "Source URL", "Confidence", "Scraped Timestamp"]

    st.dataframe(
        display_df,
        column_config={
            "Source URL": st.column_config.LinkColumn("Source URL"),
            "Scraped Timestamp": st.column_config.DatetimeColumn("Scraped Timestamp", format="YYYY-MM-DD HH:mm:ss"),
        },
        width="stretch",
        hide_index=True
    )
