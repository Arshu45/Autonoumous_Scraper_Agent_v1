"""
Dashboard design system — minimal, clean, light theme.
"""

# ── Brand palette ───────────────────────────────────────────────────────
BRAND_COLORS = {
    "Our Store":   "#5B50E8",   # Indigo
    "Forever New": "#D04A6A",   # Rose
    "Vero Moda":   "#1FAF8A",   # Teal
}

BRAND_LIST = ["Our Store", "Forever New", "Vero Moda"]

# ── Shared chart constants ──────────────────────────────────────────────
GRID_COLOR = "#EBEBF2"

# ── Plotly light defaults ───────────────────────────────────────────────
PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="'Inter', sans-serif", color="#707080", size=12),
    margin=dict(l=16, r=16, t=36, b=16),
)


def apply_css():
    import streamlit as st
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', system-ui, sans-serif;
        -webkit-font-smoothing: antialiased;
    }

    /* ── Base ─────────────────────────────────────────────────────── */
    .stApp { background-color: #FAFAFA; }
    .block-container { padding-top: 3.5rem !important; padding-bottom: 3rem; max-width: 1280px; }

    /* ── Sidebar ─────────────────────────────────────────────────── */
    section[data-testid="stSidebar"] {
        background-color: #F4F4F8;
        border-right: 1px solid #E0E0EA;
    }
    section[data-testid="stSidebar"] * { color: #505060 !important; }

    /* ── Dividers ────────────────────────────────────────────────── */
    hr { border-color: #E5E5EE !important; margin: 1.5rem 0 !important; }

    /* ── Page title ──────────────────────────────────────────────── */
    .page-title {
        font-size: 1.5rem;
        font-weight: 600;
        color: #1A1A2E;
        letter-spacing: -0.02em;
        margin-bottom: 0.15rem;
    }
    .page-subtitle {
        font-size: 0.85rem;
        color: #909098;
        margin-bottom: 0;
    }

    /* ── Section label ───────────────────────────────────────────── */
    .section-label {
        font-size: 0.7rem;
        font-weight: 600;
        color: #A0A0B0;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        margin: 2rem 0 1rem 0;
    }

    /* ── KPI cards ───────────────────────────────────────────────── */
    .kpi-card {
        background: #FFFFFF;
        border: 1px solid #E5E5EE;
        border-radius: 10px;
        padding: 18px 20px;
    }
    .kpi-label {
        font-size: 0.72rem;
        font-weight: 500;
        color: #A0A0B0;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 8px;
    }
    .kpi-value {
        font-size: 1.9rem;
        font-weight: 600;
        color: #1A1A2E;
        letter-spacing: -0.03em;
        line-height: 1;
    }
    .kpi-sub {
        font-size: 0.75rem;
        color: #C0C0CC;
        margin-top: 6px;
    }

    /* ── Gap badges ─────────────────────────────────────────────── */
    .badge-red    { color: #C0354A; background: rgba(208,74,106,0.08); border: 1px solid rgba(208,74,106,0.25); border-radius: 5px; padding: 2px 8px; font-size: 0.78rem; font-weight: 500; }
    .badge-yellow { color: #9A7020; background: rgba(180,130,40,0.08);  border: 1px solid rgba(180,130,40,0.25);  border-radius: 5px; padding: 2px 8px; font-size: 0.78rem; font-weight: 500; }
    .badge-green  { color: #1A8A68; background: rgba(31,175,138,0.08);  border: 1px solid rgba(31,175,138,0.25);  border-radius: 5px; padding: 2px 8px; font-size: 0.78rem; font-weight: 500; }

    /* ── Tables ──────────────────────────────────────────────────── */
    table { width: 100%; border-collapse: collapse; }
    thead tr { border-bottom: 1px solid #E5E5EE; }
    thead th { font-size: 0.72rem; font-weight: 600; color: #A0A0B0; text-transform: uppercase; letter-spacing: 0.08em; padding: 8px 12px; text-align: left; background: transparent; }
    tbody tr { border-bottom: 1px solid #F2F2F6; }
    tbody tr:hover { background: #F6F6FA; }
    tbody td { font-size: 0.85rem; color: #505060; padding: 9px 12px; }

    /* ── Weekly Competitor Matrix ────────────────────────────────── */

    /* Category header row */
    .wm-cat-header {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 0 6px 0;
        margin-top: 1.5rem;
    }
    .wm-cat-title {
        font-size: 0.95rem;
        font-weight: 600;
        color: #1A1A2E;
        letter-spacing: -0.01em;
    }
    .wm-badge {
        display: inline-block;
        font-size: 0.72rem;
        font-weight: 500;
        padding: 2px 9px;
        border-radius: 20px;
        white-space: nowrap;
    }
    .wm-badge-blue   { background: rgba(91,80,232,0.08); color: #4840C0; border: 1px solid rgba(91,80,232,0.2); }
    .wm-badge-purple { background: rgba(150,70,200,0.08); color: #7030A8; border: 1px solid rgba(150,70,200,0.2); }

    /* Scrollable table wrapper */
    .wm-scroll {
        overflow-x: auto;
        border: 1px solid #E5E5EE;
        border-radius: 10px;
        background: #FFFFFF;
        margin-bottom: 0.5rem;
    }

    /* Table shell */
    .wm-table {
        width: 100%;
        table-layout: fixed;
        border-collapse: collapse;
        min-width: 900px;
    }

    /* Header cells */
    .wm-brand-th {
        width: 160px;
        min-width: 140px;
        background: #F4F4F8;
        color: #505060;
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        padding: 10px 14px;
        text-align: left;
        border-bottom: 1px solid #E5E5EE;
        border-right: 1px solid #E5E5EE;
        position: sticky;
        left: 0;
        z-index: 2;
    }
    .wm-day-th {
        background: #F4F4F8;
        color: #505060;
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        padding: 10px 14px;
        text-align: left;
        border-bottom: 1px solid #E5E5EE;
        border-right: 1px solid #E5E5EE;
        min-width: 160px;
    }

    /* Brand name cell — sticky */
    .wm-brand-td {
        font-size: 0.85rem;
        font-weight: 600;
        color: #1A1A2E;
        padding: 12px 14px;
        vertical-align: top;
        border-bottom: 1px solid #F2F2F6;
        border-right: 2px solid #E5E5EE;
        background: #FAFAFA;
        white-space: nowrap;
        position: sticky;
        left: 0;
        z-index: 1;
    }

    /* Promo content cell */
    .wm-promo-td {
        font-size: 0.82rem;
        color: #505060;
        padding: 10px 14px;
        vertical-align: top;
        border-bottom: 1px solid #F2F2F6;
        border-right: 1px solid #EBEBF2;
        line-height: 1.5;
    }
    .wm-promo-td:hover { background: #F6F6FA; }

    /* Empty day dash */
    .wm-empty { color: #D0D0DC; text-align: center; }

    /* Bullet item per promotion */
    .wm-bullet {
        padding: 3px 0 3px 10px;
        border-left: 2px solid #E0E0EA;
        margin-bottom: 5px;
        line-height: 1.45;
    }
    .wm-bullet:last-child { margin-bottom: 0; }

    /* Row hover */
    .wm-table tbody tr:hover .wm-promo-td { background: #F6F6FA; }
    .wm-table tbody tr:hover .wm-brand-td { background: #F2F2F8; }

    /* ── Streamlit widget tweaks ──────────────────────────────────── */
    .stSelectbox > div > div, .stMultiSelect > div > div {
        background: #FFFFFF !important;
        border-color: #E5E5EE !important;
        color: #1A1A2E !important;
        border-radius: 8px !important;
    }

    .stMultiSelect [data-baseweb="tag"] {
        background-color: #EEF0F8 !important;
        border: 1px solid #D9DDEA !important;
        border-radius: 6px !important;
        color: #303247 !important;
        box-shadow: none !important;
    }
    .stMultiSelect [data-baseweb="tag"] span {
        color: #303247 !important;
        font-weight: 500 !important;
    }
    .stMultiSelect [data-baseweb="tag"] svg {
        fill: #6F7285 !important;
    }
    .stMultiSelect [data-baseweb="tag"]:hover {
        background-color: #E5E8F3 !important;
        border-color: #C9CEDF !important;
    }

    /* ── Chat ────────────────────────────────────────────────────── */
    .stChatMessage { background: #FFFFFF !important; border: 1px solid #E5E5EE !important; border-radius: 10px !important; margin-bottom: 0.5rem; }
    .stChatInputContainer { border-top: 1px solid #E5E5EE !important; background: #FAFAFA !important; }

    /* ── Hide branding ───────────────────────────────────────────── */
    #MainMenu, footer { visibility: hidden; }
    </style>
    """, unsafe_allow_html=True)


def page_header(title: str, subtitle: str = ""):
    import streamlit as st
    st.markdown(f"<div class='page-title'>{title}</div>", unsafe_allow_html=True)
    if subtitle:
        st.markdown(f"<div class='page-subtitle'>{subtitle}</div>", unsafe_allow_html=True)
    st.markdown("<hr>", unsafe_allow_html=True)


def section_label(text: str):
    import streamlit as st
    st.markdown(f"<div class='section-label'>{text}</div>", unsafe_allow_html=True)


def kpi_card(label: str, value: str, sub: str = "") -> str:
    return f"""
    <div class="kpi-card">
        <div class="kpi-label">{label}</div>
        <div class="kpi-value">{value}</div>
        {"<div class='kpi-sub'>"+sub+"</div>" if sub else ""}
    </div>"""


def gap_badge(gap: float) -> str:
    if gap > 30:
        return '<span class="badge-red">Critical Gap</span>'
    elif gap > 15:
        return '<span class="badge-yellow">Monitor</span>'
    else:
        return '<span class="badge-green">Competitive</span>'
