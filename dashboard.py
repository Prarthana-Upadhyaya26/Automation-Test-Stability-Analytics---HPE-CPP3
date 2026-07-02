"""
Test Stability Analytics Dashboard
====================================
Streamlit dashboard for CI test run analytics backed by schema.sql.

Usage:
  streamlit run dashboard.py
  streamlit run dashboard.py -- --db ./analytics.db
  streamlit run dashboard.py -- --db ./analytics.db ./teambravo.db
"""

import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, cast

import pandas as pd
import plotly.graph_objects as go
import streamlit as st



try:
    from pipeline import (
        load_multi_db,
        load_defect_mappings,
        load_jira_defects_df,
        get_defect_coverage_stats,
        ingest_jira_defects,
        load_jira_defects_from_list,
        map_defects_to_test_results,
        create_database,
        compute_and_cache_embeddings,
        DEFECT_WINDOW_DAYS,
        DEFECT_MIN_CONFIDENCE,
        DEFAULT_EMBED_MODEL,
        _ALPHA_RULE,
        _BETA_SEMANTIC,
    )
    PIPELINE2_AVAILABLE = True
except ImportError:
    try:
        from pipeline import load_multi_db
        PIPELINE2_AVAILABLE = True
        load_defect_mappings          = None  # type: ignore[assignment]
        load_jira_defects_df          = None  # type: ignore[assignment]
        get_defect_coverage_stats     = None  # type: ignore[assignment]
        ingest_jira_defects           = None  # type: ignore[assignment]
        load_jira_defects_from_list   = None  # type: ignore[assignment]
        map_defects_to_test_results   = None  # type: ignore[assignment]
        create_database               = None  # type: ignore[assignment]
        compute_and_cache_embeddings  = None  # type: ignore[assignment]
        DEFECT_WINDOW_DAYS            = 7
        DEFECT_MIN_CONFIDENCE         = 0.25
        DEFAULT_EMBED_MODEL           = "BAAI/bge-small-en-v1.5"
        _ALPHA_RULE                   = 1.0
        _BETA_SEMANTIC                = 0.0
    except ImportError:
        PIPELINE2_AVAILABLE           = False
        load_defect_mappings          = None  # type: ignore[assignment]
        load_jira_defects_df          = None  # type: ignore[assignment]
        get_defect_coverage_stats     = None  # type: ignore[assignment]
        ingest_jira_defects           = None  # type: ignore[assignment]
        load_jira_defects_from_list   = None  # type: ignore[assignment]
        map_defects_to_test_results   = None  # type: ignore[assignment]
        create_database               = None  # type: ignore[assignment]
        compute_and_cache_embeddings  = None  # type: ignore[assignment]
        DEFECT_WINDOW_DAYS            = 7
        DEFECT_MIN_CONFIDENCE         = 0.25
        DEFAULT_EMBED_MODEL           = "BAAI/bge-small-en-v1.5"
        _ALPHA_RULE                   = 1.0
        _BETA_SEMANTIC                = 0.0

st.set_page_config(
    page_title="Test Stability Analytics",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="auto",
)

DEFAULT_DB = "./analytics.db"
GREEN_THRESHOLD = 80.0
AMBER_THRESHOLD = 60.0
ROLLING_WINDOW = 10
ANOMALY_SIGMA = 2.0

DURATION_TESTS = [
    "TC_User_BulkImport",
    "TC_Dashboard_ExportChart",
    "TC_Login_ValidCredentials",
]

C = {
    "bg": "#0D1117",
    "bg2": "#161B22",
    "card": "#1C2128",
    "border": "#30363D",
    "txt": "#E6EDF3",
    "muted": "#8B949E",
    "green": "#3FB950",
    "red": "#F85149",
    "amber": "#D29922",
    "blue": "#58A6FF",
    "purple": "#BC8CFF",
    "orange": "#FFA657",
    "teal": "#39D353",
}

FAILURE_COLOR = {
    "timeout": C["amber"],
    "element": C["blue"],
    "assertion": C["purple"],
    "data": C["orange"],
    "environment": C["teal"],
    "unknown": C["muted"],
}


def inject_css() -> None:
    st.markdown(
        f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,400;0,600;0,700;1,400&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');

    html, body, [class*="css"] {{
        font-family: 'IBM Plex Sans', sans-serif !important;
        background-color: {C["bg"]} !important;
        color: {C["txt"]} !important;
    }}
    #MainMenu, footer {{ visibility: hidden; }}
    [data-testid="collapsedControl"] {{
        display: flex !important; opacity: 1 !important;
        visibility: visible !important; pointer-events: auto !important;
    }}
    .block-container {{ padding: 2rem 2.8rem 5rem !important; max-width: 1440px; }}
    .element-container {{ margin-bottom: 0 !important; }}
    a {{ color: {C["blue"]} !important; text-decoration: none; }}

    ::-webkit-scrollbar {{ width: 5px; height: 5px; }}
    ::-webkit-scrollbar-track {{ background: {C["bg"]}; }}
    ::-webkit-scrollbar-thumb {{ background: {C["border"]}; border-radius: 4px; }}

    section[data-testid="stSidebar"] > div:first-child {{
        background-color: {C["bg2"]} !important;
        border-right: 1px solid {C["border"]} !important;
        padding-top: 1.4rem !important;
    }}
    [data-testid="stSidebar"] * {{ color: {C["txt"]} !important; }}

    .sec-wrap {{
        display: flex; align-items: center; gap: 10px;
        margin: 2.6rem 0 1.2rem;
        padding-bottom: 0.6rem;
        border-bottom: 1px solid {C["border"]};
    }}
    .sec-tag {{
        background: {C["blue"]}18; color: {C["blue"]};
        font: 700 0.62rem/1 'JetBrains Mono', monospace;
        letter-spacing: .14em; text-transform: uppercase;
        padding: 4px 10px; border-radius: 5px;
        border: 1px solid {C["blue"]}33; white-space: nowrap;
        flex-shrink: 0;
    }}
    .sec-title {{ font: 600 1.05rem/1 'IBM Plex Sans', sans-serif; margin: 0; color: {C["txt"]}; }}
    .sec-sub {{
        font: 400 0.75rem/1 'JetBrains Mono', monospace;
        color: {C["muted"]}; margin-left: auto; white-space: nowrap;
        flex-shrink: 0;
    }}

    .dash-header {{
        background: linear-gradient(135deg, {C["bg2"]} 0%, {C["card"]} 100%);
        border: 1px solid {C["border"]}; border-radius: 14px;
        padding: 1.5rem 2rem; margin-bottom: 2.2rem;
        display: flex; justify-content: space-between; align-items: center;
        gap: 1.5rem;
    }}
    .dash-wordmark {{
        font: 700 1.6rem/1 'IBM Plex Sans', sans-serif;
        letter-spacing: -.03em; color: {C["txt"]};
    }}
    .dash-wordmark span {{ color: {C["blue"]}; }}
    .dash-subtitle {{
        font: 400 0.76rem/1.5 'JetBrains Mono', monospace;
        color: {C["muted"]}; margin-top: .35rem;
    }}
    .dash-right {{
        text-align: right;
        font: 400 0.74rem/1.8 'JetBrains Mono', monospace;
        color: {C["muted"]}; flex-shrink: 0;
    }}
    .dash-right b {{ color: {C["txt"]}; font-weight: 600; }}

    .metric-card {{
        background: {C["card"]}; border: 1px solid {C["border"]};
        border-radius: 12px; padding: 1.4rem 1.6rem;
        height: 100%; position: relative; overflow: hidden;
    }}
    .metric-card::before {{
        content: ''; position: absolute; left: 0; top: 0; bottom: 0;
        width: 3px; border-radius: 12px 0 0 12px;
    }}
    .mc-green::before  {{ background: {C["green"]}; }}
    .mc-red::before    {{ background: {C["red"]}; }}
    .mc-amber::before  {{ background: {C["amber"]}; }}
    .mc-blue::before   {{ background: {C["blue"]}; }}
    .mc-purple::before {{ background: {C["purple"]}; }}
    .mc-label {{
        font: 600 0.66rem/1 'JetBrains Mono', monospace;
        letter-spacing: .12em; text-transform: uppercase;
        color: {C["muted"]}; margin-bottom: .6rem;
    }}
    .mc-value {{ font: 700 2.9rem/1 'JetBrains Mono', monospace; margin-bottom: .4rem; }}
    .mc-value.green  {{ color: {C["green"]}; }}
    .mc-value.red    {{ color: {C["red"]}; }}
    .mc-value.amber  {{ color: {C["amber"]}; }}
    .mc-value.blue   {{ color: {C["blue"]}; }}
    .mc-sub {{ font: 400 0.76rem/1.45 'IBM Plex Sans', sans-serif; color: {C["muted"]}; }}
    .mc-badge {{
        display: inline-flex; align-items: center; gap: 5px;
        padding: 4px 11px 4px 8px; border-radius: 20px;
        font: 600 0.7rem/1 'JetBrains Mono', monospace; margin-top: .6rem;
        letter-spacing: .04em;
    }}
    .badge-green  {{ background:{C["green"]}18; color:{C["green"]}; border:1px solid {C["green"]}44; }}
    .badge-red    {{ background:{C["red"]}18;   color:{C["red"]};   border:1px solid {C["red"]}44;   }}
    .badge-amber  {{ background:{C["amber"]}18; color:{C["amber"]}; border:1px solid {C["amber"]}44; }}
    .badge-blue   {{ background:{C["blue"]}18;  color:{C["blue"]};  border:1px solid {C["blue"]}44;  }}
    .badge-purple {{ background:{C["purple"]}18;color:{C["purple"]};border:1px solid {C["purple"]}44;}}
    .badge-orange {{ background:{C["orange"]}18;color:{C["orange"]};border:1px solid {C["orange"]}44;}}

    .delta-card {{
        background: {C["card"]}; border: 1px solid {C["border"]};
        border-radius: 12px; padding: 1.4rem 1.6rem; text-align: center;
        height: 100%;
    }}
    .delta-num {{ font: 700 2.5rem/1.1 'JetBrains Mono', monospace; }}
    .delta-lbl {{
        font: 500 0.66rem/1.5 'JetBrains Mono', monospace;
        letter-spacing: .1em; text-transform: uppercase;
        color: {C["muted"]}; margin-top: .5rem;
    }}
    .delta-lbl span {{ text-transform: none; letter-spacing: 0; font-weight: 400; }}

    .fail-table {{
        width: 100%; border-collapse: collapse; font-size: 0.83rem;
        background: {C["card"]}; border: 1px solid {C["border"]};
        border-radius: 12px; overflow: hidden;
    }}
    .fail-table th {{
        background: {C["bg2"]}; color: {C["muted"]};
        font: 600 0.64rem/1 'JetBrains Mono', monospace;
        letter-spacing: .12em; text-transform: uppercase;
        padding: 11px 16px; text-align: left;
        border-bottom: 1px solid {C["border"]}; white-space: nowrap;
    }}
    .fail-table td {{
        padding: 11px 16px;
        border-bottom: 1px solid {C["border"]}44;
        vertical-align: middle; line-height: 1.45;
    }}
    .fail-table tr:last-child td {{ border-bottom: none; }}
    .fail-table tr:hover td {{ background: {C["bg2"]}aa; }}
    .tname {{ font: 600 0.79rem/1.3 'JetBrains Mono', monospace; color: {C["blue"]}; }}
    .tmsg  {{ font: 400 0.74rem/1.45 'JetBrains Mono', monospace; color: {C["muted"]}; max-width: 460px; word-break: break-word; }}
    .tkw   {{ font: 400 0.7rem/1 'JetBrains Mono', monospace; color: {C["muted"]}; }}
    .tdur  {{ font: 600 0.77rem/1 'JetBrains Mono', monospace; color: {C["txt"]}; white-space: nowrap; }}

    .info-banner {{
        background: {C["blue"]}10; border: 1px solid {C["blue"]}30;
        border-radius: 8px; padding: .75rem 1.1rem;
        font: 400 0.81rem/1.5 'IBM Plex Sans', sans-serif;
        color: {C["blue"]}; margin-bottom: 1rem;
    }}
    .warn-banner {{
        background: {C["amber"]}10; border: 1px solid {C["amber"]}30;
        border-radius: 8px; padding: .75rem 1.1rem;
        font: 400 0.81rem/1.5 'IBM Plex Sans', sans-serif;
        color: {C["amber"]}; margin-bottom: 1rem;
    }}

    .dash-footer {{
        margin-top: 3.5rem; padding: 1rem 0 .5rem;
        border-top: 1px solid {C["border"]}88;
        font: 400 0.7rem/1.6 'JetBrains Mono', monospace; color: {C["muted"]}99;
        display: flex; justify-content: space-between;
    }}

    /* ── Defect Mapping tab ───────────────────────────────────── */
    .defect-card {{
        background: {C["card"]}; border: 1px solid {C["border"]};
        border-radius: 12px; padding: 1.2rem 1.4rem; margin-bottom: .8rem;
        position: relative; overflow: hidden;
    }}
    .defect-card::before {{
        content: ''; position: absolute; left: 0; top: 0; bottom: 0;
        width: 3px; border-radius: 12px 0 0 12px;
    }}
    .defect-confirmed::before  {{ background: {C["green"]}; }}
    .defect-candidate::before  {{ background: {C["amber"]}; }}
    .defect-unmatched::before  {{ background: {C["muted"]}; }}
    .defect-key {{
        font: 700 0.78rem/1 'JetBrains Mono', monospace;
        color: {C["blue"]}; letter-spacing: .05em;
    }}
    .defect-summary {{
        font: 500 0.85rem/1.4 'IBM Plex Sans', sans-serif;
        color: {C["txt"]}; margin: .35rem 0 .5rem;
    }}
    .defect-meta {{
        font: 400 0.71rem/1.6 'JetBrains Mono', monospace;
        color: {C["muted"]};
    }}
    .defect-meta b {{ color: {C["txt"]}; font-weight: 600; }}
    .conf-bar-wrap {{
        height: 5px; background: {C["border"]}44;
        border-radius: 3px; margin: .5rem 0 .3rem; overflow: hidden;
    }}
    .conf-bar {{
        height: 100%; border-radius: 3px;
        transition: width .3s ease;
    }}
    .conf-label {{
        font: 600 0.66rem/1 'JetBrains Mono', monospace;
        letter-spacing: .1em; text-transform: uppercase;
    }}
    .reason-tag {{
        display: inline-block; padding: 2px 8px; border-radius: 4px; margin: 2px 3px 2px 0;
        font: 400 0.68rem/1.5 'JetBrains Mono', monospace;
        background: {C["blue"]}14; color: {C["blue"]};
        border: 1px solid {C["blue"]}28;
    }}
    .defect-table {{
        width: 100%; border-collapse: collapse; font-size: 0.82rem;
        background: {C["card"]}; border: 1px solid {C["border"]};
        border-radius: 12px; overflow: hidden;
    }}
    .defect-table th {{
        background: {C["bg2"]}; color: {C["muted"]};
        font: 600 0.63rem/1 'JetBrains Mono', monospace;
        letter-spacing: .12em; text-transform: uppercase;
        padding: 10px 14px; text-align: left;
        border-bottom: 1px solid {C["border"]}; white-space: nowrap;
    }}
    .defect-table td {{
        padding: 10px 14px; border-bottom: 1px solid {C["border"]}44;
        vertical-align: middle; line-height: 1.45;
    }}
    .defect-table tr:last-child td {{ border-bottom: none; }}
    .defect-table tr:hover td {{ background: {C["bg2"]}aa; }}
    .status-pill {{
        display: inline-flex; align-items: center;
        padding: 2px 10px; border-radius: 12px;
        font: 600 0.66rem/1.6 'JetBrains Mono', monospace;
        white-space: nowrap;
    }}
    .status-triage    {{ background:{C["amber"]}18; color:{C["amber"]}; border:1px solid {C["amber"]}33; }}
    .status-inprogress{{ background:{C["blue"]}18;  color:{C["blue"]};  border:1px solid {C["blue"]}33;  }}
    .status-labreview {{ background:{C["purple"]}18;color:{C["purple"]};border:1px solid {C["purple"]}33;}}
    .status-closed    {{ background:{C["green"]}18; color:{C["green"]}; border:1px solid {C["green"]}33; }}
    .status-other     {{ background:{C["muted"]}18; color:{C["muted"]}; border:1px solid {C["muted"]}33; }}
    .proj-tag {{
        display: inline-flex; align-items: center;
        padding: 2px 9px; border-radius: 5px;
        font: 700 0.64rem/1.6 'JetBrains Mono', monospace;
        background: {C["purple"]}14; color: {C["purple"]};
        border: 1px solid {C["purple"]}28; white-space: nowrap;
    }}
    .multi-badge {{
        display: inline-flex; align-items: center; gap: 4px;
        padding: 2px 8px; border-radius: 5px;
        font: 600 0.64rem/1.6 'JetBrains Mono', monospace;
        background: {C["orange"]}14; color: {C["orange"]};
        border: 1px solid {C["orange"]}28;
    }}

    div[data-testid="stSelectbox"] label {{
        font: 600 0.65rem/1 'JetBrains Mono', monospace !important;
        letter-spacing: .1em !important;
        text-transform: uppercase !important;
        color: {C["muted"]} !important;
    }}
    div[data-testid="stSelectbox"] > div > div {{
        background-color: {C["card"]} !important;
        border: 1px solid {C["border"]} !important;
        border-radius: 8px !important;
        font: 400 0.82rem/1 'JetBrains Mono', monospace !important;
        color: {C["txt"]} !important;
    }}
    button[kind="primary"] {{
        background: {C["red"]}22 !important;
        color: {C["red"]} !important;
        border: 1px solid {C["red"]}44 !important;
        border-radius: 8px !important;
        font: 600 0.78rem/1 'JetBrains Mono', monospace !important;
        letter-spacing: .04em !important;
    }}
    button[kind="primary"]:hover {{
        background: {C["red"]}38 !important;
        border-color: {C["red"]}66 !important;
    }}
    </style>
    """,
        unsafe_allow_html=True,
    )

def dark_layout(height: int = 360, title: str = "", margin: dict | None = None) -> dict:
    m = margin or dict(l=10, r=20, t=40 if title else 10, b=10)
    return dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="IBM Plex Sans, sans-serif", color=C["txt"], size=11),
        height=height,
        margin=m,
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            bordercolor=C["border"],
            borderwidth=1,
            font=dict(size=10.5),
        ),
        xaxis=dict(
            gridcolor="rgba(48,54,61,0.33)",
            linecolor=C["border"],
            tickfont=dict(color=C["muted"], size=10),
            zeroline=False,
        ),
        yaxis=dict(
            gridcolor="rgba(48,54,61,0.33)",
            linecolor=C["border"],
            tickfont=dict(color=C["muted"], size=10),
            zeroline=False,
        ),
        hoverlabel=dict(
            bgcolor=C["bg2"],
            bordercolor=C["border"],
            font=dict(family="JetBrains Mono, monospace", size=11, color=C["txt"]),
        ),
    )


def standardize_columns(df_runs, df_results):
    """Ensure required columns exist even if schema varies."""
    if "pass_rate" in df_runs.columns and "pass_rate_pct" not in df_runs.columns:
        df_runs["pass_rate_pct"] = df_runs["pass_rate"]
    if "total_tests" in df_runs.columns and "total" not in df_runs.columns:
        df_runs["total"] = df_runs["total_tests"]
    if "duration" in df_results.columns and "duration_s" not in df_results.columns:
        df_results["duration_s"] = df_results["duration"]
    if "message" in df_results.columns and "failure_msg" not in df_results.columns:
        df_results["failure_msg"] = df_results["message"]
    if "run_timestamp" not in df_results.columns:
        if "timestamp" in df_runs.columns:
            df_results = df_results.merge(
                df_runs[["run_id", "timestamp"]], on="run_id", how="left"
            )
            df_results.rename(columns={"timestamp": "run_timestamp"}, inplace=True)
        else:
            df_results["run_timestamp"] = None
    return df_runs, df_results

def safe_sort_runs(df, by="timestamp", ascending=True):
    """Sort a DataFrame by column, falling back gracefully. Handles run_id as numeric string."""
    if isinstance(by, list):
        sort_cols = []
        ascending_list = ascending if isinstance(ascending, list) else [ascending] * len(by)
        for i, col in enumerate(by):
            if col not in df.columns:
                found = False
                for fallback in ["timestamp", "run_id"]:
                    if fallback in df.columns and fallback not in [c for c, _ in sort_cols]:
                        sort_cols.append((fallback, ascending_list[i]))
                        found = True
                        break
                if not found:
                    continue
            elif col == "run_id" and pd.api.types.is_object_dtype(df[col]):
                df = df.copy()
                df["_run_num_temp"] = df["run_id"].str.extract(r'(\d+)$').astype(float)
                sort_cols.append(("_run_num_temp", ascending_list[i]))
            else:
                sort_cols.append((col, ascending_list[i]))
        if not sort_cols:
            return df
        cols, asc = zip(*sort_cols)
        df = df.sort_values(by=list(cols), ascending=list(asc))
        if "_run_num_temp" in df.columns:
            df = df.drop(columns=["_run_num_temp"])
        return df

    if by not in df.columns:
        for fallback in ["timestamp", "run_id", "count"]:
            if fallback in df.columns:
                by = fallback
                break
        else:
            return df
    if by == "run_id" and pd.api.types.is_object_dtype(df[by]):
        df = df.copy()
        df["_run_num"] = df["run_id"].str.extract(r'(\d+)$').astype(float)
        df = df.sort_values("_run_num", ascending=ascending).drop(columns=["_run_num"])
        return df
    return df.sort_values(by, ascending=ascending)

setattr(pd.DataFrame, "safe_sort_runs", lambda self, *args, **kwargs: safe_sort_runs(self, *args, **kwargs))

@st.cache_resource
def get_db_data(db_paths_key: str):
    db_paths = [p.strip() for p in db_paths_key.split("|") if p.strip()]
    existing = [p for p in db_paths if Path(p).exists()]
    if not existing:
        return _build_demo_data()
    if PIPELINE2_AVAILABLE:
        df_runs, df_results = load_multi_db(existing)
    else:
        df_runs, df_results = _fallback_single_read(existing[0])
    df_runs, df_results = standardize_columns(df_runs, df_results)
    df_runs["run_id"] = df_runs["run_id"].astype(str)
    df_results["run_id"] = df_results["run_id"].astype(str)
    return df_runs, df_results, []

def _fallback_single_read(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    df_runs = pd.read_sql_query(
        """
        SELECT run_id, team, suite_name, build_no, timestamp,
               COALESCE(duration_s,0) AS duration_s,
               total, passed, failed, pass_rate_pct,
               environment, executor
        FROM runs ORDER BY timestamp ASC
        """,
        conn,
    )
    df_runs["_source_db"] = Path(db_path).stem
    df_results = pd.read_sql_query(
        """
        SELECT tr.result_id, tr.run_id, r.team, r.suite_name,
               r.timestamp AS run_timestamp,
               r.pass_rate_pct AS run_pass_rate,
               tr.test_name, tr.status, tr.duration_s,
               tr.failure_msg, tr.failure_kw, tr.tags
        FROM test_results tr
        JOIN runs r ON tr.run_id = r.run_id
        ORDER BY r.timestamp ASC, tr.test_name ASC
        """,
        conn,
    )
    df_results["_source_db"] = Path(db_path).stem
    conn.close()
    return df_runs, df_results

def _build_demo_data():
    rng = random.Random(42)
    TESTS_DEMO = [
        ("TC_Login_ValidCredentials", "feature_login", "priority_high", "stable", 0.00),
        ("TC_Login_InvalidPassword", "feature_login", "priority_high", "stable", 0.00),
        ("TC_Login_SessionTimeout", "feature_login", "priority_high", "stable", 0.00),
        ("TC_Login_AccountLockout", "feature_login", "priority_medium", "stable", 0.00),
        ("TC_Dashboard_FilterByDate", "feature_dashboard", "priority_medium", "stable", 0.00),
        ("TC_Dashboard_Pagination", "feature_dashboard", "priority_medium", "stable", 0.00),
        ("TC_Dashboard_ExportChart", "feature_dashboard", "priority_medium", "stable", 0.00),
        ("TC_Dashboard_SearchBar", "feature_dashboard", "priority_medium", "stable", 0.00),
        ("TC_User_CreateAccount", "feature_usermgmt", "priority_high", "stable", 0.00),
        ("TC_User_EditProfile", "feature_usermgmt", "priority_medium", "stable", 0.00),
        ("TC_User_DeleteAccount", "feature_usermgmt", "priority_high", "stable", 0.00),
        ("TC_User_PasswordReset", "feature_usermgmt", "priority_medium", "stable", 0.00),
        ("TC_Login_MFAVerification", "feature_login", "priority_high", "flaky-mild", 0.30),
        ("TC_Login_SSORedirect", "feature_login", "priority_high", "flaky-mild", 0.35),
        ("TC_Dashboard_LoadWidget", "feature_dashboard", "priority_medium", "flaky-moderate", 0.50),
        ("TC_Dashboard_RefreshData", "feature_dashboard", "priority_medium", "flaky-moderate", 0.55),
        ("TC_User_BulkImport", "feature_usermgmt", "priority_medium", "flaky-heavy", 0.65),
        ("TC_User_RoleAssignment", "feature_usermgmt", "priority_high", "consistently_failing", 0.80),
        ("TC_User_BatchExport", "feature_usermgmt", "priority_medium", "consistently_failing", 0.75),
        ("TC_Login_OAuthCallback", "feature_login", "priority_high", "consistently_failing", 0.70),
    ]
    FAIL_CFG = {
        "TC_Login_MFAVerification": ("timeout", "assertion", 0.70),
        "TC_Login_SSORedirect": ("timeout", "element", 0.70),
        "TC_Dashboard_LoadWidget": ("element", "timeout", 0.70),
        "TC_Dashboard_RefreshData": ("assertion", "data", 0.60),
        "TC_User_BulkImport": ("data", "assertion", 0.70),
        "TC_User_RoleAssignment": ("assertion", "data", 0.65),
        "TC_User_BatchExport": ("data", "element", 0.65),
        "TC_Login_OAuthCallback": ("timeout", "element", 0.70),
    }
    def _fail_msg(cat):
        if cat == "timeout":
            e = rng.choice(["loading-spinner","overlay-modal","auth-redirect","session-token"])
            t = rng.choice([15, 20, 30, 45])
            return f"Element '{e}' still visible after {t}s timeout", "Wait Until Element Is Visible"
        elif cat == "element":
            l = rng.choice(["id=widget-container","id=submit-btn","css=.data-grid","id=modal-confirm"])
            r = rng.choice([3, 5, 7])
            return f"Element with locator '{l}' not found after {r} retries", "Click Element"
        elif cat == "assertion":
            exp, got, desc = rng.choice([("200","500","Internal Server Error"),("200","404","Not Found"),("201","400","Bad Request")])
            return f"Expected HTTP status '{exp}' but got '{got}' — {desc}", "Should Be Equal As Numbers"
        else:
            rows2 = rng.choice([0, 1, 2])
            mins  = rng.choice([50, 100, 200])
            rng2  = rng.choice(["Oct 2026", "last 30 days", "Q4 2026"])
            return f"CSV export contained {rows2} rows — expected at least {mins} records for {rng2}", "Verify Row Count"
    def _dur(name, n, status):
        if name == "TC_User_BulkImport":
            base = rng.uniform(10,14) if n<=40 else rng.uniform(18,24) if n<=65 else rng.uniform(28,36)
        elif name == "TC_Dashboard_ExportChart":
            base = rng.uniform(3,5) if n<=50 else rng.uniform(12,15)
        elif name == "TC_Login_ValidCredentials":
            base = rng.uniform(2.0,3.5) if n%2==0 else rng.uniform(4.5,6.5)
        else:
            base = rng.uniform(1.2,8.5)
        if status == "FAIL":
            base += rng.uniform(5,15)
        return round(base, 3)
    ANOMALY_RUNS = {36, 37}
    ANOMALY_FAIL_RATE = 0.80
    start_dt = datetime(2026, 10, 1)
    runs_rows = []
    results_rows = []
    for n in range(1, 101):
        ts = (start_dt + timedelta(hours=24*(n-1))).isoformat()
        anomaly = n in ANOMALY_RUNS
        run_id = f"TeamAlpha_build_{n:03d}"
        row_results = []
        for name, feat, pri, cat, fp in TESTS_DEMO:
            if anomaly:
                eff = max(fp, ANOMALY_FAIL_RATE)
            elif fp == 0.0:
                eff = 0.0
            else:
                env = 0.65 if n<=25 else 0.60 if n<=35 else 0.55 if n<=45 else 0.35 if n<=75 else 0.15
                eff = min(0.95, fp*(1.0+env))
            status = "FAIL" if rng.random() < eff else "PASS"
            dur = _dur(name, n, status)
            failure_msg, failure_kw = None, None
            if status == "FAIL" and name in FAIL_CFG:
                prim, sec, pp = FAIL_CFG[name]
                fcat = prim if rng.random() < pp else sec
                failure_msg, failure_kw = _fail_msg(fcat)
            row_results.append({
                "result_id": f"{run_id}_{name}",
                "run_id": run_id,
                "team": "TeamAlpha",
                "suite_name": "Suite_Regression_TeamAlpha",
                "run_timestamp": ts,
                "run_pass_rate": 0.0,
                "test_name": name,
                "status": status,
                "duration_s": dur,
                "failure_msg": failure_msg,
                "failure_kw": failure_kw,
                "tags": f'["alpha_regression","{feat}","{pri}"]',
                "_source_db": "demo",
            })
        passed = sum(1 for r in row_results if r["status"]=="PASS")
        failed = 20 - passed
        pr = round(passed * 100.0 / 20, 1)
        for r in row_results:
            r["run_pass_rate"] = pr
        results_rows.extend(row_results)
        runs_rows.append({
            "run_id": run_id,
            "team": "TeamAlpha",
            "suite_name": "Suite_Regression_TeamAlpha",
            "build_no": n,
            "timestamp": ts,
            "duration_s": 0.0,
            "total": 20,
            "passed": passed,
            "failed": failed,
            "pass_rate_pct": pr,
            "environment": "staging",
            "executor": f"jenkins-agent-{(n%3)+1:02d}",
            "_source_db": "demo",
        })
    return pd.DataFrame(runs_rows), pd.DataFrame(results_rows), []

def filter_by_source(df: pd.DataFrame, source: Optional[str]) -> pd.DataFrame:
    if source and source != "All" and "_source_db" in df.columns:
        return df[df["_source_db"] == source].copy()
    return df.copy()


def compute_anomalies(df_runs: pd.DataFrame) -> pd.DataFrame:
    df = safe_sort_runs(df_runs.copy(), "run_id").reset_index(drop=True)
    roll = df["pass_rate_pct"].rolling(window=ROLLING_WINDOW, min_periods=3)
    df["roll_mean"] = roll.mean().shift(1)
    df["roll_std"] = roll.std().shift(1).fillna(5.0)
    df["z_score"] = (df["roll_mean"] - df["pass_rate_pct"]) / df["roll_std"].clip(lower=1.0)
    df["anomaly"] = df["z_score"] >= ANOMALY_SIGMA
    return df


def render_ml_insights_cta() -> None:
    st.markdown(
        f"""
        <div style="margin: 0 0 1.2rem; padding: 1rem 1.1rem; border: 1px solid {C['border']}; border-radius: 14px; background: linear-gradient(135deg, {C['card']} 0%, {C['bg2']} 100%);">
          <div style="font-size: 0.95rem; font-weight: 700; color: {C['txt']}; margin-bottom: .35rem;">🤖 ML Insights workspace</div>
          <div style="font-size: 0.9rem; color: {C['muted']}; margin-bottom: .7rem;">
            Open the dedicated ML Insights page for flakiness predictions, duration drift, clustering, and anomaly screening.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if switch_page := getattr(st, "switch_page", None):
        if st.button("Open ML Insights", type="primary", use_container_width=False):
            switch_page("pages/2_ML_Insights.py")
    elif page_link := getattr(st, "page_link", None):
        page_link("pages/2_ML_Insights.py", label="Open ML Insights", icon="🤖")
    else:
        st.info("Open the ML Insights page from the sidebar page menu.")


def classify_failure_category(failure_msg: Optional[str], failure_kw: Optional[str] = None) -> str:
    parts = []
    for value in (failure_msg, failure_kw):
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    text = " ".join(parts).lower()
    if not text:
        return "unknown"

    if any(token in text for token in ("timeout", "timed out", "blocked completion", "exceeded", "wait until")):
        return "timeout"
    if any(token in text for token in ("element", "not found", "click element", "widget-container", "page dom", "absent", "dom changed")):
        return "element"
    if any(token in text for token in ("data validation", "csv export", "export/import", "records", "rows", "should not be empty")):
        return "data"
    if any(token in text for token in ("assert", "assertion", "expected", "but got", "assertionerror")):
        return "assertion"
    if any(token in text for token in ("environment", "unreachable", "deployment", "network", "service unavailable", "connection")):
        return "environment"
    return "unknown"


def get_failures_for_run(df_results: pd.DataFrame, run_id: str) -> pd.DataFrame:
    df = df_results[(df_results["run_id"] == run_id) & (df_results["status"] == "FAIL")].copy()
    df["failure_category"] = df.apply(
        lambda row: classify_failure_category(row.get("failure_msg"), row.get("failure_kw")),
        axis=1,
    )
    df["keyword_name"] = df["failure_kw"].fillna("—")
    return safe_sort_runs(df, "test_name")

def compute_flaky_from_results(df_results: pd.DataFrame) -> pd.DataFrame:
    """Compute per-test flip count and failure rate from df_results."""
    df = df_results.sort_values(["_source_db", "test_name", "run_timestamp"]).copy()
    df["prev_status"] = df.groupby(["_source_db", "test_name"])["status"].shift(1)
    df["flip"] = (df["status"] != df["prev_status"]) & df["prev_status"].notna()
    
    result = (
        df.groupby("test_name")
        .agg(
            flip_count=("flip", "sum"),
            fail_count=("status", lambda s: (s == "FAIL").sum()),
            total_runs=("status", "count"),
        )
        .reset_index()
    )
    result["failure_rate"] = (result["fail_count"] / result["total_runs"] * 100).round(1)
    result["flip_count"] = result["flip_count"].astype(int)
    return result.sort_values("flip_count", ascending=False)

def get_duration_series(df_results: pd.DataFrame, test_name: str) -> pd.DataFrame:
    df = df_results[df_results["test_name"] == test_name].copy()
    df = safe_sort_runs(df, "run_timestamp").reset_index(drop=True)
    df["run_num"] = range(1, len(df) + 1)
    return df

def compute_week_on_week(df_runs: pd.DataFrame, df_results: pd.DataFrame, selected_week: int) -> dict:
    df = df_runs.copy()
    df["build_num"] = df["run_id"].str.extract(r"(\d+)$").astype(float)
    df = df.sort_values("build_num").reset_index(drop=True)
    week_size = 7
    start_build = (selected_week - 1) * week_size + 1
    end_build = selected_week * week_size
    prev_start = start_build - week_size
    prev_end = start_build - 1
    df_this_week = df[(df["build_num"] >= start_build) & (df["build_num"] <= end_build)]
    df_last_week = df[(df["build_num"] >= prev_start) & (df["build_num"] <= prev_end)]
    if df_this_week.empty or df_last_week.empty:
        return dict(pass_rate_delta=0.0, this_avg=0.0, last_avg=0.0, new_failures=0, tests_fixed=0)
    this_avg = df_this_week["pass_rate_pct"].mean()
    last_avg = df_last_week["pass_rate_pct"].mean()
    this_ids = set(df_this_week["run_id"])
    last_ids = set(df_last_week["run_id"])
    def week_status(ids):
        return dict(
            df_results[df_results["run_id"].isin(ids)]
            .groupby("test_name")["status"]
            .agg(lambda x: "FAIL" if "FAIL" in x.values else "PASS")
        )
    this_status = week_status(this_ids)
    last_status = week_status(last_ids)
    all_tests = set(this_status.keys()) | set(last_status.keys())
    new_failures = 0
    tests_fixed = 0
    for t in all_tests:
        prev = last_status.get(t, "PASS")
        curr = this_status.get(t, "PASS")
        if prev == "PASS" and curr == "FAIL":
            new_failures += 1
        elif prev == "FAIL" and curr == "PASS":
            tests_fixed += 1
    return dict(
        pass_rate_delta=this_avg - last_avg,
        this_avg=this_avg,
        last_avg=last_avg,
        new_failures=new_failures,
        tests_fixed=tests_fixed,
    )


def chart_trend(df_runs: pd.DataFrame, show_n: int) -> go.Figure:
    df = compute_anomalies(df_runs)
    display_df = df.tail(show_n).copy()
    fig = go.Figure(layout=dark_layout(height=380, margin=dict(l=10, r=20, t=10, b=30)))
    for lo, hi, col in [
        (0, AMBER_THRESHOLD, "rgba(248,81,73,0.09)"),
        (AMBER_THRESHOLD, GREEN_THRESHOLD, "rgba(210,153,34,0.09)"),
        (GREEN_THRESHOLD, 100, "rgba(63,185,80,0.06)"),
    ]:
        fig.add_hrect(y0=lo, y1=hi, fillcolor=col, line_width=0, layer="below")
    for y, col, label in [
        (GREEN_THRESHOLD, "rgba(63,185,80,0.53)", "80% target"),
        (AMBER_THRESHOLD, "rgba(210,153,34,0.4)", "60% warning"),
    ]:
        fig.add_hline(
            y=y,
            line=dict(color=col, width=1, dash="dot"),
            annotation_text=label,
            annotation=dict(font=dict(color=col, size=9.5), xanchor="right", x=1),
        )
    fig.add_trace(
        go.Scatter(
            x=display_df["run_id"],
            y=display_df["roll_mean"],
            mode="lines",
            line=dict(color="rgba(139,148,158,0.53)", width=1.5, dash="dot"),
            name=f"Rolling mean ({ROLLING_WINDOW} runs)",
            hovertemplate="Rolling mean: %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=display_df["run_id"],
            y=display_df["pass_rate_pct"],
            mode="lines+markers",
            line=dict(color=C["blue"], width=2.5),
            fill="tozeroy",
            fillcolor="rgba(88,166,255,0.09)",
            marker=dict(size=4, color=C["blue"]),
            name="Pass rate %",
            hovertemplate="<b>Run %{x}</b><br>Pass rate: <b>%{y:.1f}%</b><extra></extra>",
        )
    )
    anom = display_df[display_df["anomaly"]]
    if len(anom):
        fig.add_trace(
            go.Scatter(
                x=anom["run_id"],
                y=anom["pass_rate_pct"],
                mode="markers",
                marker=dict(size=11, color=C["red"], symbol="circle", line=dict(color="#fff", width=1.5)),
                name="⚠ Anomaly",
                hovertemplate="<b>⚠ ANOMALY — Run %{x}</b><br>Pass rate: <b>%{y:.1f}%</b><br>Z-score: %{customdata:.2f}σ<extra></extra>",
                customdata=anom["z_score"],
            )
        )
    fig.update_layout(
        xaxis=dict(title=dict(text="Run #", font=dict(size=10, color=C["muted"])), dtick=5),
        yaxis=dict(title=dict(text="Pass Rate (%)", font=dict(size=10, color=C["muted"])), range=[0, 102]),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        hovermode="x unified",
    )
    return fig

def chart_failure_dist(df_failures: pd.DataFrame) -> Optional[go.Figure]:
    if df_failures.empty:
        return None
    counts = df_failures["failure_category"].value_counts().reset_index()
    counts.columns = ["category", "count"]
    fig = go.Figure(layout=dark_layout(height=260, margin=dict(l=0, r=0, t=10, b=0)))
    fig.add_trace(
        go.Pie(
            labels=counts["category"],
            values=counts["count"],
            hole=0.55,
            textinfo="label+percent",
            textfont=dict(family="JetBrains Mono", size=10.5),
            marker=dict(
                colors=[FAILURE_COLOR.get(c, C["muted"]) for c in counts["category"]],
                line=dict(color=C["bg"], width=2),
            ),
            hovertemplate="<b>%{label}</b><br>Count: %{value}<br>%{percent}<extra></extra>",
        )
    )
    return fig

def chart_duration_drift(df_dur: pd.DataFrame, test_name: str) -> go.Figure:
    if df_dur.empty:
        return go.Figure(layout=dark_layout(height=280))
    pass_df = df_dur[df_dur["status"] == "PASS"]
    fail_df = df_dur[df_dur["status"] == "FAIL"]
    fig = go.Figure(layout=dark_layout(height=280, margin=dict(l=10, r=20, t=10, b=30)))
    df_dur["roll_mean"] = df_dur["duration_s"].rolling(window=5, min_periods=2).mean()
    fig.add_trace(
        go.Scatter(
            x=df_dur["run_num"],
            y=df_dur["roll_mean"],
            mode="lines",
            line=dict(color="rgba(139,148,158,0.45)", width=1.5, dash="dot"),
            name="5-run rolling mean",
        )
    )
    if not pass_df.empty:
        fig.add_trace(
            go.Scatter(
                x=pass_df["run_num"],
                y=pass_df["duration_s"],
                mode="markers",
                marker=dict(size=5, color=C["green"], opacity=0.75),
                name="PASS",
                hovertemplate="Run %{x} · PASS · %{y:.2f}s<extra></extra>",
            )
        )
    if not fail_df.empty:
        fig.add_trace(
            go.Scatter(
                x=fail_df["run_num"],
                y=fail_df["duration_s"],
                mode="markers",
                marker=dict(size=6, color=C["red"], opacity=0.85, symbol="x"),
                name="FAIL",
                hovertemplate="Run %{x} · FAIL · %{y:.2f}s<extra></extra>",
            )
        )
    fig.update_layout(
        xaxis=dict(title=dict(text="Run #", font=dict(size=10, color=C["muted"])), dtick=10),
        yaxis=dict(title=dict(text="Duration (s)", font=dict(size=10, color=C["muted"]))),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        hovermode="x unified",
    )
    return fig

def chart_heatmap(df_results: pd.DataFrame, source_db: Optional[str] = None) -> go.Figure:
    df = df_results.copy()
    if source_db and source_db != "All" and "_source_db" in df.columns:
        df = df[df["_source_db"] == source_db]
    if df.empty:
        return go.Figure(layout=dark_layout(height=500))
    run_order = (
        df[["run_id", "run_timestamp"]]
        .drop_duplicates()
        .sort_values("run_timestamp")["run_id"]
        .tolist()
    )
    df["pass_int"] = (df["status"] == "PASS").astype(int)
    pivot = (
        df.pivot_table(index="test_name", columns="run_id", values="pass_int", aggfunc="first")
        .reindex(columns=[r for r in run_order if r in df["run_id"].unique()])
    )
    pivot = pivot.sort_index()
    dur_pivot = (
        df.pivot_table(index="test_name", columns="run_id", values="duration_s", aggfunc="first")
        .reindex(columns=pivot.columns)
    )
    test_names = list(pivot.index)
    run_ids = list(pivot.columns)
    def short_run(rid):
        try:
            return str(int(str(rid).rsplit("_", 1)[-1]))
        except (ValueError, IndexError):
            return str(rid)
    x_labels = [short_run(r) for r in run_ids]
    z = pivot.values.tolist()
    text = []
    for t_idx, test in enumerate(test_names):
        row_text = []
        for r_idx, run in enumerate(run_ids):
            try:
                v = pivot.iloc[t_idx, r_idx]
                dur = dur_pivot.iloc[t_idx, r_idx]
                st_ = "PASS" if v == 1 else "FAIL"
                dur_s = f"{dur:.2f}s" if pd.notna(dur) else "—"
                row_text.append(
                    f"<b>{test}</b><br>Run: {short_run(run)}<br>Status: <b>{st_}</b><br>Duration: {dur_s}"
                )
            except Exception:
                row_text.append("")
        text.append(row_text)
    fig = go.Figure(
        layout=dark_layout(
            height=max(420, len(test_names) * 24 + 80),
            margin=dict(l=10, r=20, t=40, b=60),
            title="",
        )
    )
    fig.add_trace(
        go.Heatmap(
            z=z,
            x=x_labels,
            y=test_names,
            text=text,
            hovertemplate="%{text}<extra></extra>",
            colorscale=[
                [0.0, "#3d0c0c"],
                [0.45, C["red"]],
                [0.55, C["green"]],
                [1.0, "#0d3b1e"],
            ],
            zmin=0,
            zmax=1,
            showscale=False,
            xgap=1,
            ygap=1,
        )
    )
    for rid in run_ids:
        try:
            n = int(str(rid).rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            continue
        if n in (36, 37):
            fig.add_vline(
                x=x_labels.index(short_run(rid)),
                line=dict(color=C["amber"], width=2, dash="dot"),
                annotation_text=f"⚠ Run {n}",
                annotation=dict(font=dict(color=C["amber"], size=9), yanchor="bottom"),
            )
    fig.update_layout(
        xaxis=dict(
            title=dict(text="Run number →", font=dict(size=10, color=C["muted"])),
            tickfont=dict(family="JetBrains Mono", size=9, color=C["muted"]),
            tickangle=0,
            tickvals=[x_labels[i] for i in range(0, len(x_labels), 10)],
        ),
        yaxis=dict(
            tickfont=dict(family="JetBrains Mono", size=9.5, color=C["txt"]),
            autorange="reversed",
        ),
    )
    return fig

def hex_to_rgba(hex_color, alpha=0.5):
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"

def chart_sankey(df_results: pd.DataFrame, phase_filter: str = "All phases") -> go.Figure:
    df = df_results.copy()
    failures = df[df["status"] == "FAIL"].copy()
    if failures.empty:
        fig = go.Figure(layout=dark_layout(height=500))
        fig.add_annotation(
            text="No failures to display", x=0.5, y=0.5, font=dict(color=C["muted"], size=14), showarrow=False
        )
        return fig

    def run_phase(rid):
        try:
            n = int(str(rid).rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return "Phase 4 (76–100)"
        if n <= 25:
            return "Phase 1 (1–25)"
        elif n <= 45:
            return "Phase 2 (26–45)"
        elif n <= 75:
            return "Phase 3 (46–75)"
        else:
            return "Phase 4 (76–100)"
    failures["fail_cat"] = failures.apply(
        lambda row: classify_failure_category(row.get("failure_msg"), row.get("failure_kw")),
        axis=1,
    )
    failures["phase"] = failures["run_id"].apply(run_phase)
    failures["test_short"] = failures["test_name"].str.replace("TC_", "", regex=False)
    if phase_filter != "All phases":
        failures = failures[failures["phase"] == phase_filter]
    cats = sorted(failures["fail_cat"].unique())
    tests_s = sorted(failures["test_short"].unique())
    phases = [p for p in ["Phase 1 (1–25)", "Phase 2 (26–45)", "Phase 3 (46–75)", "Phase 4 (76–100)"] if p in failures["phase"].values]
    all_nodes = cats + tests_s + phases
    node_idx = {name: i for i, name in enumerate(all_nodes)}
    CAT_COLORS = {
        "timeout": C["amber"],
        "element": C["blue"],
        "assertion": C["purple"],
        "data": C["orange"],
        "environment": C["teal"],
        "unknown": C["muted"],
    }
    PHASE_COLORS = {
        "Phase 1 (1–25)": "#58A6FF",
        "Phase 2 (26–45)": "#D29922",
        "Phase 3 (46–75)": "#3FB950",
        "Phase 4 (76–100)": "#BC8CFF",
    }
    node_colors = []
    for name in all_nodes:
        if name in CAT_COLORS:
            node_colors.append(CAT_COLORS[name])
        elif name in PHASE_COLORS:
            node_colors.append(PHASE_COLORS[name])
        else:
            node_colors.append(C["border"])
    source_idx, target_idx, values, link_colors = [], [], [], []
    ct = failures.groupby(["fail_cat", "test_short"]).size().reset_index(name="cnt")
    for _, row in ct.iterrows():
        if row["fail_cat"] in node_idx and row["test_short"] in node_idx:
            source_idx.append(node_idx[row["fail_cat"]])
            target_idx.append(node_idx[row["test_short"]])
            values.append(int(row["cnt"]))
            base = CAT_COLORS.get(row["fail_cat"], C["muted"])
            link_colors.append(hex_to_rgba(base, 0.5))
    tp = failures.groupby(["test_short", "phase"]).size().reset_index(name="cnt")
    for _, row in tp.iterrows():
        if row["test_short"] in node_idx and row["phase"] in node_idx:
            source_idx.append(node_idx[row["test_short"]])
            target_idx.append(node_idx[row["phase"]])
            values.append(int(row["cnt"]))
            base = PHASE_COLORS.get(row["phase"], C["border"])
            link_colors.append(hex_to_rgba(base, 0.3))
    fig = go.Figure(layout=dark_layout(height=560, margin=dict(l=10, r=10, t=50, b=10)))
    fig.add_trace(
        go.Sankey(
            arrangement="snap",
            node=dict(
                pad=18,
                thickness=22,
                line=dict(color=C["border"], width=0.5),
                label=all_nodes,
                color=node_colors,
                hovertemplate="<b>%{label}</b><br>Total failures: %{value}<extra></extra>",
            ),
            link=dict(
                source=source_idx,
                target=target_idx,
                value=values,
                color=link_colors,
                hovertemplate="%{source.label} → %{target.label}<br>Failures: <b>%{value}</b><extra></extra>",
            ),
        )
    )
    for x_pos, label in [(0.0, "Failure Category"), (0.45, "Test"), (1.0, "Run Phase")]:
        fig.add_annotation(
            x=x_pos,
            y=1.04,
            xref="paper",
            yref="paper",
            text=f'<span style="font-family:JetBrains Mono;font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:{C["muted"]}">{label}</span>',
            showarrow=False,
            align="center",
        )
    return fig


def _badge(text, kind):
    return f'<span class="mc-badge badge-{kind}">{text}</span>'

def _failure_badge(fcat):
    color_map = {"timeout":"amber","element":"blue","assertion":"purple","data":"orange","environment":"blue","unknown":"blue"}
    return _badge(fcat.upper(), color_map.get(fcat, "blue"))

def _section(tag, title, sub=""):
    sub_html = f'<span class="sec-sub">{sub}</span>' if sub else ""
    st.markdown(
        f'<div class="sec-wrap"><span class="sec-tag">{tag}</span><span class="sec-title">{title}</span>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def render_sidebar(df_runs: pd.DataFrame, db_paths: list[str], db_sources: list[str]):
    with st.sidebar:
        st.markdown(
            f'<div style="font:700 1.1rem/1 \'IBM Plex Sans\',sans-serif; margin-bottom:.3rem;">⚡ Test Stability</div>'
            f'<div style="font:400 0.72rem/1 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-bottom:1.2rem;">CI Analytics · schema.sql v2</div>',
            unsafe_allow_html=True,
        )
        if st.button("🔄 Reset All Filters", use_container_width=True, type="primary"):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.rerun()

        selected_source = "All"
        if len(db_sources) > 1:
            st.markdown(f'<div class="sb-label">Database Source</div>', unsafe_allow_html=True)
            selected_source = st.selectbox("Filter by database", options=["All"] + db_sources)
        st.markdown("---")

        total_runs = len(df_runs)
        max_weeks = max(1, total_runs // 7)
        selected_week = st.selectbox(
            "Week to analyze",
            options=list(range(1, max_weeks + 1)),
            format_func=lambda w: f"Week {w} (runs {(w-1)*7 + 1}–{min(w*7, total_runs)})",
            index=max_weeks - 1,
            help="Select which week to show health metrics for",
        )
        st.markdown("---")

        st.markdown(
            f'<div style="font:600 0.65rem/1 \'JetBrains Mono\',monospace; letter-spacing:.1em; text-transform:uppercase; color:{C["muted"]}; margin-bottom:.6rem;">Visible Sections</div>',
            unsafe_allow_html=True,
        )
        show_trend = st.checkbox("Trend · Historical Pass Rate", value = False)
        show_run_inspector = st.checkbox("Run Inspector", value=False)
        show_duration_drift = st.checkbox("Duration Drift", value=False)
        show_health_matrix = st.checkbox("Test Health Matrix", value=False)
        show_defect_mapping = st.checkbox("Defect Mapping", value=False)
        st.markdown("---")
        
        st.markdown(f'<div class="sb-label">Run Inspector</div>', unsafe_allow_html=True)
        # safe_sort_runs may be masked by a dataframe column with the same name
        sorter = getattr(df_runs, "safe_sort_runs", None)
        if callable(sorter):
            sorted_df = cast(pd.DataFrame, sorter("timestamp", ascending=False))
        else:
            # fallback to pandas sort_values
            sorted_df = cast(pd.DataFrame, df_runs.sort_values("timestamp", ascending=False))
        run_ids_sorted = sorted_df["run_id"].tolist()
        run_labels = [
            f"{rid} ({str(df_runs[df_runs['run_id']==rid]['timestamp'].values[0])[:10]} {df_runs[df_runs['run_id']==rid]['pass_rate_pct'].values[0]:.1f}%)"
            for rid in run_ids_sorted
        ]
        sel_idx = st.selectbox(
            "Inspect run", range(len(run_labels)), format_func=lambda i: run_labels[i], index=0
        )
        selected_run_id = run_ids_sorted[sel_idx] if run_ids_sorted else None
        st.markdown("---")

        st.markdown(f'<div class="sb-label">Duration Drift</div>', unsafe_allow_html=True)
        drift_test = st.selectbox("Test to analyze", DURATION_TESTS, index=0)
        st.markdown("---")

        st.markdown(f'<div class="sb-label">Database(s)</div>', unsafe_allow_html=True)
        for p in db_paths:
            st.code(p, language=None)
        st.markdown(
            f'<div style="font:400 0.72rem/1.7 \'JetBrains Mono\',monospace; color:{C["muted"]};">'
            f'Runs: <b style="color:{C["txt"]}">{len(df_runs)}</b><br>'
            f'Sources: <b style="color:{C["txt"]}">{", ".join(db_sources)}</b></div>',
            unsafe_allow_html=True,
        )

    return selected_source, selected_week, selected_run_id, drift_test, show_trend, show_run_inspector, show_duration_drift, show_health_matrix, show_defect_mapping


def render_header(df_runs: pd.DataFrame) -> None:
    if df_runs.empty:
        return
    sorter = getattr(df_runs, "safe_sort_runs", None)
    if callable(sorter):
        latest_df = cast(pd.DataFrame, sorter("run_id"))
    else:
        latest_df = cast(pd.DataFrame, df_runs.sort_values("run_id"))
    latest = latest_df.iloc[-1]
    ts_str = str(latest.get("timestamp", ""))[:16].replace("T", " ")
    
    suite_names = df_runs["suite_name"].unique()
    programs = []
    for sn in suite_names:
        sn_lower = sn.lower()
        if "alpha" in sn_lower:
            programs.append(("ALPHA", C["blue"], "🅰️"))
        elif "beta" in sn_lower:
            programs.append(("BETA", C["purple"], "🅱️"))
        elif "gamma" in sn_lower:
            programs.append(("GAMMA", C["orange"], "🅲"))
    if not programs:
        programs = [(suite_names[0].upper(), C["muted"], "❓")]
    programs.sort(key=lambda x: x[0])
    program_badges = " ".join([
        f'<span style="background:{color}15; color:{color}; padding:0.3rem 0.8rem; '
        f'border-radius:6px; margin-right:0.5rem; font:700 1rem/1 \'JetBrains Mono\',monospace; '
        f'border:1px solid {color}44;">{icon} {name}</span>'
        for name, color, icon in programs
    ])
    
    st.markdown(
        f'<div class="dash-header">'
        f'  <div>'
        f'    <div class="dash-wordmark">Test Stability <span>Analytics</span></div>'
        f'    <div class="dash-subtitle">Suite_Regression · Robot Framework · schema.sql v2</div>'
        f'    <div style="margin-top:.5rem;">{program_badges}</div>'
        f'  </div>'
        f'  <div class="dash-right">'
        f'    <div style="margin-bottom:.3rem;"><b>Latest build:</b> {latest["run_id"]}</div>'
        f'    <div style="margin-bottom:.3rem;"><b>Environment:</b> {latest.get("environment","—")} · <b>Executor:</b> {latest.get("executor","—")}</div>'
        f'    <div><b>Timestamp:</b> {ts_str}</div>'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )

def render_weekly_health(df_runs: pd.DataFrame, df_results: pd.DataFrame, selected_week: int) -> None:
    _section("WEEKLY HEALTH", f"Week {selected_week} · Current & Week‑over‑Week", "")
    if df_runs.empty:
        st.markdown('<div class="info-banner">No data loaded.</div>', unsafe_allow_html=True)
        return

    df = df_runs.copy()
    df["build_num"] = df["run_id"].str.extract(r"(\d+)$").astype(float)
    df = df.sort_values("build_num")
    week_size = 7
    start_build = (selected_week - 1) * week_size + 1
    end_build = selected_week * week_size
    df_week = df[(df["build_num"] >= start_build) & (df["build_num"] <= end_build)]
    if df_week.empty:
        st.markdown('<div class="info-banner">No data for selected week.</div>', unsafe_allow_html=True)
        return

    latest = df_week.iloc[-1]
    pr = float(latest.get("pass_rate_pct", 0))
    passed = int(latest.get("passed", 0))
    failed = int(latest.get("failed", 0))
    total = int(latest.get("total", 20))
    run_id = latest.get("run_id", "—")

    if pr >= GREEN_THRESHOLD:
        val_cls, card_cls, badge_kind, status_text = "green", "mc-green", "green", "✅  HEALTHY"
    elif pr >= AMBER_THRESHOLD:
        val_cls, card_cls, badge_kind, status_text = "amber", "mc-amber", "amber", "⚠️  WARNING"
    else:
        val_cls, card_cls, badge_kind, status_text = "red", "mc-red", "red", "🔴  AT RISK"

    recent_avg = df_week["pass_rate_pct"].mean()
    best_recent = df_week["pass_rate_pct"].max()
    worst_recent = df_week["pass_rate_pct"].min()

    wow = compute_week_on_week(df_runs, df_results, selected_week)
    delta_pr = wow["pass_rate_delta"]
    if delta_pr >= 2:
        pr_color, pr_arrow = C["green"], "▲"
    elif delta_pr <= -2:
        pr_color, pr_arrow = C["red"], "▼"
    else:
        pr_color, pr_arrow = C["muted"], "—"
    sign = "+" if delta_pr > 0 else ""

    c1, c2, c3, c4 = st.columns([2.2, 1.4, 1.4, 1.4])
    with c1:
        st.markdown(
            f'<div class="metric-card {card_cls}">'
            f'  <div class="mc-label">Current Pass Rate</div>'
            f'  <div class="mc-value {val_cls}">{pr:.1f}%</div>'
            f'  <div class="mc-sub">{passed} passed · {failed} failed · {total} total  |  {run_id}</div>'
            f'  <div class="mc-badge badge-{badge_kind}">{status_text}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c2:
        avg_cls = "green" if recent_avg >= GREEN_THRESHOLD else "amber" if recent_avg >= AMBER_THRESHOLD else "red"
        st.markdown(
            f'<div class="metric-card mc-{avg_cls}">'
            f'  <div class="mc-label">Week Average</div>'
            f'  <div class="mc-value {avg_cls}">{recent_avg:.1f}%</div>'
            f'  <div class="mc-sub">Across week {selected_week}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div class="metric-card mc-blue">'
            f'  <div class="mc-label">Best (Week)</div>'
            f'  <div class="mc-value blue">{best_recent:.1f}%</div>'
            f'  <div class="mc-sub">Peak pass rate</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c4:
        worst_cls = "red" if worst_recent < AMBER_THRESHOLD else "amber"
        st.markdown(
            f'<div class="metric-card mc-{worst_cls}">'
            f'  <div class="mc-label">Worst (Week)</div>'
            f'  <div class="mc-value {worst_cls}">{worst_recent:.1f}%</div>'
            f'  <div class="mc-sub">Lowest pass rate</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div style="margin-top:1.8rem;"></div>', unsafe_allow_html=True)
    d1, d2, d3 = st.columns(3)
    with d1:
        st.markdown(
            f'<div class="delta-card">'
            f'  <div class="delta-num" style="color:{pr_color};">{pr_arrow} {sign}{delta_pr:.1f}pp</div>'
            f'  <div class="delta-lbl">Pass Rate Change<br><br>'
            f'    <span style="color:{C["txt"]};">{wow.get("this_avg",0):.1f}% this week vs {wow.get("last_avg",0):.1f}% last week</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with d2:
        nf_color = C["red"] if wow["new_failures"] > 0 else C["green"]
        nf_arrow = "▲" if wow["new_failures"] > 0 else "✓"
        st.markdown(
            f'<div class="delta-card">'
            f'  <div class="delta-num" style="color:{nf_color};">{nf_arrow} {wow["new_failures"]}</div>'
            f'  <div class="delta-lbl">New Failures<br><br>'
            f'    <span style="color:{C["txt"]};">Tests failing now that passed last week</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with d3:
        tf_color = C["green"] if wow["tests_fixed"] > 0 else C["muted"]
        tf_arrow = "▼" if wow["tests_fixed"] > 0 else "—"
        st.markdown(
            f'<div class="delta-card">'
            f'  <div class="delta-num" style="color:{tf_color};">{tf_arrow} {wow["tests_fixed"]}</div>'
            f'  <div class="delta-lbl">Tests Fixed<br><br>'
            f'    <span style="color:{C["txt"]};">Tests passing now that failed last week</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    st.markdown(
        f'<div style="font:400 0.76rem/1.6 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-top:1rem;">'
        f'Metrics reflect the latest run in week {selected_week}. Delta cards compare against the previous 7-run window.</div>',
        unsafe_allow_html=True,
    )

def render_trend(df_runs: pd.DataFrame) -> None:
    df_anom = compute_anomalies(df_runs)
    n_anom = int(df_anom["anomaly"].sum())
    anom_lbl = f"{n_anom} anomal{'y' if n_anom==1 else 'ies'} detected" if n_anom else "no anomalies"
    _section("TREND", "Historical Pass Rate", f"Full history · {len(df_runs)} runs · {anom_lbl}")
    fig = chart_trend(df_runs, show_n=100)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.markdown(
        f'<div style="font:400 0.76rem/1.6 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-top:.2rem;">'
        f'Pass rate over all runs. Red markers (⚠) flag statistical anomalies where the drop exceeds {ANOMALY_SIGMA}σ of the rolling mean. '
        f'Green zone ≥ {GREEN_THRESHOLD:.0f}% · amber zone ≥ {AMBER_THRESHOLD:.0f}%.</div>',
        unsafe_allow_html=True,
    )

def render_run_inspector(df_results: pd.DataFrame, run_id: Optional[str]) -> None:
    if not run_id:
        return
    df = get_failures_for_run(df_results, run_id)
    run_pr = (
        df_results[df_results["run_id"] == run_id]["run_pass_rate"].iloc[0]
        if len(df_results[df_results["run_id"] == run_id])
        else "—"
    )
    n = len(df)
    _section(
        "RUN INSPECTOR",
        f"Run {run_id} Failures",
        f"{n} failure{'s' if n!=1 else ''} · pass rate {run_pr:.1f}%" if isinstance(run_pr, float) else f"Run {run_id} · {n} failures",
    )

    if df.empty:
        st.markdown(
            f'<div class="info-banner">✅  No failures in {run_id} — all tests passed!</div>',
            unsafe_allow_html=True,
        )
    else:
        col_table, col_donut = st.columns([2.6, 1])
        with col_table:
            rows = ""
            for _, row in df.iterrows():
                rows += f"""
                <tr>
                  <td><span class="tname">{row['test_name']}</span></td>
                  <td>{_failure_badge(row.get('failure_category','unknown'))}</td>
                  <td><span class="tmsg">{row.get('failure_msg','')}</span><br>
                      <span class="tkw">via {row.get('keyword_name','—')}</span></td>
                  <td><span class="tdur">{row['duration_s']:.2f}s</span></td>
                </tr>"""
            st.markdown(
                f"""
            <table class="fail-table">
              <thead><tr>
                <th>Test Name</th><th>Failure Type</th>
                <th>Failure Message</th><th>Duration</th>
              </tr></thead>
              <tbody>{rows}</tbody>
            </table>""",
                unsafe_allow_html=True,
            )
        with col_donut:
            st.markdown(
                f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; padding-top:.3rem; margin-bottom:.4rem;">Failure Breakdown</div>',
                unsafe_allow_html=True,
            )
            fig_donut = chart_failure_dist(df)
            if fig_donut:
                st.plotly_chart(fig_donut, use_container_width=True, config={"displayModeBar": False})


def render_duration_drift(df_results: pd.DataFrame, drift_test: str, selected_source: str) -> None:
    _section("DURATION DRIFT", f"{drift_test.replace('TC_','')}", "Execution time evolution")
    df_dur = get_duration_series(df_results, drift_test)
    if selected_source != "All":
        df_dur = df_dur[df_dur["_source_db"] == selected_source]
    fig_drift = chart_duration_drift(df_dur, drift_test)
    st.plotly_chart(fig_drift, use_container_width=True, config={"displayModeBar": False})
    drift_insights = {
        "TC_User_BulkImport": "📈  Progressive drift — duration increases across three phases.",
        "TC_Dashboard_ExportChart": "⚡  Step change — duration triples after run 50.",
        "TC_Login_ValidCredentials": "🔄  Seasonal alternation — odd runs ~2× slower.",
    }
    if drift_test in drift_insights:
        st.markdown(
            f'<div style="background:{C["blue"]}12; border:1px solid {C["blue"]}33; border-radius:8px; padding:.65rem 1rem; font-size:.82rem; color:{C["blue"]}; margin-top:.4rem;">{drift_insights[drift_test]}</div>',
            unsafe_allow_html=True,
        )

def render_test_health_matrix(df_results: pd.DataFrame, selected_source: str) -> None:
    _section("TEST HEALTH MATRIX", "Heatmap", "")
    st.markdown(f'<div style="font:700 1rem/1 \'IBM Plex Sans\',sans-serif; color:{C["txt"]}; margin:1.2rem 0 .6rem; display:flex; align-items:center; gap:8px;"><span style="width:14px;height:14px;border-radius:3px;background:{C["green"]};display:inline-block;"></span>Test × Run Heatmap</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div style="font:400 0.76rem/1.6 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-bottom:.8rem; letter-spacing:.02em;">'
        f'<span style="color:{C["green"]}">■ PASS</span> &nbsp; '
        f'<span style="color:{C["red"]}">■ FAIL</span> · '
        f'Flaky = speckled · Consistently failing = solid red rows.</div>',
        unsafe_allow_html=True,
    )
    fig_heat = chart_heatmap(df_results, selected_source if selected_source != "All" else None)
    st.plotly_chart(fig_heat, use_container_width=True, config={"displayModeBar": False})
    st.markdown(
        f'<div style="font:400 0.76rem/1.6 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-top:.2rem;">'
        f'Each cell shows pass (green) or fail (red) for a test × run pair. Speckled rows are flaky; solid red rows are consistently failing.</div>',
        unsafe_allow_html=True,
    )


def _get_cached_db_conn(db_path: str) -> Optional[sqlite3.Connection]:
    """
    Open and cache a SQLite connection for the given path.

    Decorated with @st.cache_resource so Streamlit creates exactly one
    connection per db_path per process lifetime, reusing it across every
    script rerun.  This avoids the leak pattern of opening a new connection
    on every Streamlit interaction.

    WAL mode is set here (in addition to pipeline.py's create_database) so
    that dashboard-only deployments — where the DB was created externally —
    also benefit from concurrent read/write access.

    Thread safety
    ─────────────
    sqlite3 connections are not thread-safe by default.  Streamlit runs each
    session in its own thread.  We pass check_same_thread=False because:
      (a) The dashboard is read-only from this connection.
      (b) WAL mode means readers never block writers and vice-versa.
      (c) Python's GIL prevents true concurrent C-level sqlite3 calls.
    For write operations triggered from the dashboard (ingest, map, writeback),
    a *separate* short-lived connection is opened, used, and closed within the
    button handler — never shared across threads.
    """
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout  = 10000")
    conn.execute("PRAGMA foreign_keys  = ON")
    conn.execute("PRAGMA synchronous   = NORMAL")
    conn.execute("PRAGMA cache_size    = -32768")
    return conn


def _open_db_conn(db_paths: list[str]) -> Optional[sqlite3.Connection]:
    """
    Return a cached read connection for the first existing DB path.

    All reads in render_defect_mapping go through this.  The connection is
    cached by @st.cache_resource on _get_cached_db_conn — only the first
    call for each path actually opens a file descriptor.

    For write operations (ingest, map, writeback) use _open_write_conn()
    which returns a fresh, uncached connection that the caller must close.
    """
    for p in db_paths:
        conn = _get_cached_db_conn(p)
        if conn is not None:
            return conn
    return None


def _open_write_conn(db_paths: list[str]) -> Optional[sqlite3.Connection]:
    """
    Open a fresh, uncached write connection for button-handler mutations.

    Never reuse the cached read connection for writes — SQLite allows one
    writer at a time and holding the write lock on the shared cached
    connection would block all dashboard reads until the write completes.

    Caller is responsible for calling conn.close() after the write is done.
    """
    for p in db_paths:
        if Path(p).exists():
            conn = sqlite3.connect(p, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout  = 10000")
            conn.execute("PRAGMA foreign_keys  = ON")
            conn.execute("PRAGMA synchronous   = NORMAL")
            return conn
    return None


def _status_pill(status: str) -> str:
    s = (status or "").lower().replace(" ", "").replace("-", "")
    if s == "triage":
        css = "status-triage"
    elif s in ("inprogress", "development", "testing"):
        css = "status-inprogress"
    elif s == "labreview":
        css = "status-labreview"
    elif s.startswith("closed"):
        css = "status-closed"
    else:
        css = "status-other"
    return f'<span class="status-pill {css}">{status or "—"}</span>'


def _proj_tag(project: str) -> str:
    return f'<span class="proj-tag">{project or "—"}</span>'


def _conf_color(score: float) -> str:
    if score >= 0.75:
        return C["green"]
    elif score >= 0.5:
        return C["blue"]
    elif score >= 0.25:
        return C["amber"]
    return C["muted"]


def _conf_bar(score: float) -> str:
    color = _conf_color(score)
    pct   = int(score * 100)
    label_color = color
    return (
        f'<div class="conf-bar-wrap">'
        f'  <div class="conf-bar" style="width:{pct}%; background:{color};"></div>'
        f'</div>'
        f'<span class="conf-label" style="color:{label_color};">{score:.2f}</span>'
    )


def _reason_tags(reason: str) -> str:
    parts = [r.strip() for r in reason.split(";") if r.strip()]
    return " ".join(f'<span class="reason-tag">{p}</span>' for p in parts)


def _parse_labels(labels_json: str) -> list[str]:
    try:
        return json.loads(labels_json or "[]")
    except Exception:
        return []


def render_defect_mapping(db_paths: list[str]) -> None:
    """Render the full Defect Mapping tab."""

    _section(
        "DEFECT MAPPING",
        "Automation Failures → Jira Defects",
        "Reporter match · 7-day window · confidence scoring",
    )

    # ── Capability check ─────────────────────────────────────────────────────
    if load_defect_mappings is None:
        st.markdown(
            f'<div class="warn-banner">⚠  pipeline.py not importable — '
            f'defect mapping functions unavailable. '
            f'Ensure <code>pipeline.py</code> is in the same directory as dashboard.py.</div>',
            unsafe_allow_html=True,
        )
        return

    conn = _open_db_conn(db_paths)
    if conn is None:
        st.markdown(
            f'<div class="warn-banner">⚠  No database found at: {", ".join(db_paths)}<br>'
            f'Run <code>python pipeline.py</code> to ingest CI run data first.</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Check whether mapping tables exist ───────────────────────────────────
    tables = {
        r[0] for r in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    has_defects  = "jira_defects"         in tables
    has_mappings = "defect_test_mappings" in tables

    if not has_defects or not has_mappings:
        st.markdown(
            f'<div class="warn-banner">⚠  Defect tables not found in the database.<br>'
            f'The database schema may be from an older version. '
            f'Re-run <code>python pipeline.py</code> with the updated <code>schema.sql</code> '
            f'to create the <code>jira_defects</code> and <code>defect_test_mappings</code> tables.</div>',
            unsafe_allow_html=True,
        )
        return

    if load_jira_defects_df is None or get_defect_coverage_stats is None:
        st.markdown(
            f'<div class="warn-banner">⚠  Defect data helpers are unavailable. '
            f'Ensure <code>pipeline.py</code> is importable and up to date.</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Load data ─────────────────────────────────────────────────────────────
    df_defects  = load_jira_defects_df(conn)
    df_mappings = load_defect_mappings(conn)
    cov         = get_defect_coverage_stats(conn)

    # ── KPI cards ─────────────────────────────────────────────────────────────
    k1, k2, k3, k4, k5 = st.columns(5)
    kpi_data = [
        (k1, "TOTAL DEFECTS",       cov["total_defects"],        "blue",   f"{cov['total_defects']} Jira issues ingested"),
        (k2, "CONFIRMED MAPPINGS",  cov["confirmed_mappings"],   "green",  "Email ✓ + Date ✓ + Score ≥ 0.5"),
        (k3, "TESTS COVERED",       cov["unique_tests_covered"], "purple", "Unique failing tests linked"),
        (k4, "FAILURE COVERAGE",    f"{cov['coverage_pct']:.1f}%", "amber", "% of FAIL rows with a mapped defect"),
        (k5, "AVG CONFIDENCE",      f"{cov['avg_confidence']:.2f}", "blue", "Mean match confidence (0–1)"),
    ]
    for col, label, value, color, sub in kpi_data:
        with col:
            st.markdown(
                f'<div class="metric-card mc-{color}">'
                f'  <div class="mc-label">{label}</div>'
                f'  <div class="mc-value {color}">{value}</div>'
                f'  <div class="mc-sub">{sub}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="margin-top:1.6rem;"></div>', unsafe_allow_html=True)

    # ── Scoring methodology explainer ─────────────────────────────────────────
    with st.expander("ℹ  How the hybrid scoring works", expanded=False):
        alpha_pct = int(_ALPHA_RULE * 100)
        beta_pct  = int(_BETA_SEMANTIC * 100)
        st.markdown(
            f"""
<div class="defect-meta" style="line-height:1.9;">

<b style="color:{C['txt']}; font-size:0.85rem;">Two-stage hybrid pipeline</b><br>

<b>Stage 1 — Gate checks</b> (must both pass for <span style="color:{C['green']};">confirmed = ✓</span>)<br>
&nbsp; (a) <b>Reporter email</b> must match the tester email supplied via <code>--tester-email</code><br>
&nbsp; (b) <b>Date window</b>: defect created within ±{DEFECT_WINDOW_DAYS} days of the run timestamp<br><br>

<b>Stage 2 — Hybrid confidence score</b> = {alpha_pct}% rule + {beta_pct}% semantic<br><br>

<b>Rule-based component</b> (weight {alpha_pct}%)<br>
&nbsp; +0.60 exact TC_* test name found verbatim in defect summary or description<br>
&nbsp; +0.25 test-name stem words (e.g. "bulkimport", "ssredirect") found in defect text<br>
&nbsp; +0.15 ≥1 shared diagnostic token between failure log and defect description<br><br>

<b>Semantic component</b> (weight {beta_pct}%, model: <code>{DEFAULT_EMBED_MODEL}</code>)<br>
&nbsp; Cosine similarity between L2-normalised embeddings of the test <code>failure_msg</code><br>
&nbsp; and the defect <code>summary + description</code>. Embeddings are computed once and<br>
&nbsp; cached as float32 BLOBs in the <code>embeddings</code> table — only new records are re-embedded.<br>
&nbsp; If FlagEmbedding is not installed, weight falls back to 100% rule-based.<br><br>

<b>Confirmed threshold</b>: both gates ✓ AND final score ≥ 0.50<br>
<b>Min stored threshold</b>: score ≥ {DEFECT_MIN_CONFIDENCE} (lower scores are silently discarded)

</div>
            """,
            unsafe_allow_html=True,
        )

    # ── Inline Jira + mapping uploader ──────────────────────────────────────
    with st.expander("⬆  Upload new Jira defect JSON & run mapping", expanded=False):
        st.markdown(
            f'<div class="info-banner">'
            f'Upload a JSON file containing one or more Jira defect records '
            f'(single object, list, or Jira API <code>{{"issues": [...]}}</code> format). '
            f'Defects are ingested and the <b>hybrid rule + semantic</b> mapping is re-run immediately. '
            f'Embeddings are cached — only new defects and results are re-embedded.'
            f'</div>',
            unsafe_allow_html=True,
        )
        col_up, col_opt = st.columns([2, 1])
        with col_up:
            uploaded = st.file_uploader(
                "Jira defects JSON file",
                type=["json", "txt"],
                key="jira_upload",
                label_visibility="collapsed",
            )
        with col_opt:
            tester_email = st.text_input(
                "Tester / reporter email (optional)",
                placeholder="tester@hpe.com",
                key="tester_email_input",
            )
            window = st.number_input(
                "Date window (days)",
                min_value=1, max_value=90, value=7,
                key="window_days_input",
            )
            overwrite_mode  = st.checkbox("Overwrite existing mappings", value=False, key="overwrite_mappings")
            no_semantic     = st.checkbox("Rule-only mode (no embeddings)", value=False, key="no_semantic_toggle",
                                          help="Disable BAAI/bge-small-en-v1.5 — faster but misses paraphrased defects")

        if uploaded is not None:
            if st.button("🔗  Ingest & Map", key="run_mapping_btn", type="primary"):
                import json as _json
                try:
                    raw = _json.loads(uploaded.read().decode("utf-8"))
                    if isinstance(raw, dict) and "issues" in raw:
                        records = raw["issues"]
                    elif isinstance(raw, list):
                        records = raw
                    elif isinstance(raw, dict) and "key" in raw:
                        records = [raw]
                    else:
                        records = list(raw.values())

                    conn2 = _open_write_conn(db_paths)
                    if conn2 is None:
                        st.error("Database not found.")
                    else:
                        if load_jira_defects_from_list is None or ingest_jira_defects is None or map_defects_to_test_results is None:
                            st.error("Jira ingestion is unavailable.")
                        else:
                            parsed = load_jira_defects_from_list(records)
                            istats = ingest_jira_defects(conn2, parsed, overwrite=False)

                            # Disable semantic if toggled
                            from pipeline import _EMBED_MODEL_CACHE
                            if no_semantic:
                                _EMBED_MODEL_CACHE[DEFAULT_EMBED_MODEL] = None

                            mstats = map_defects_to_test_results(
                                conn2,
                                tester_email   = tester_email.strip() or None,
                                window_days    = int(window),
                                overwrite      = overwrite_mode,
                            )
                            conn2.close()
                            mode_label = "rule-only" if no_semantic or not mstats.get("semantic_enabled") else "hybrid rule+semantic"
                            st.success(
                                f"✅  Ingested {istats['inserted']} defects "
                                f"({istats['skipped']} already existed). "
                                f"Wrote {mstats['mappings_written']} mappings "
                                f"({mstats['confirmed']} confirmed) using {mode_label} scoring. "
                                f"Refresh to see updated results."
                            )
                            st.cache_resource.clear()
                            st.cache_data.clear()
                except Exception as exc:
                    st.error(f"Error processing file: {exc}")

    st.markdown('<div style="margin-top:.4rem;"></div>', unsafe_allow_html=True)

    # ── Empty state ───────────────────────────────────────────────────────────
    if df_mappings.empty and df_defects.empty:
        st.markdown(
            f'<div class="info-banner">'
            f'ℹ  No defects ingested yet. Upload a file above, use<br>'
            f'<code>python pipeline.py --fetch-jira --map-defects</code> for live Jira sync, or<br>'
            f'<code>python pipeline.py --ingest-jira ./defects.json --map-defects</code> for file ingestion.'
            f'</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Live Jira fetch button ────────────────────────────────────────────────
    with st.expander("🔄  Live Jira Sync", expanded=False):
        st.markdown(
            f'<div class="info-banner">'
            f'Pull the latest defects directly from your Jira instance via REST API v3. '
            f'Requires <code>JIRA_BASE_URL</code>, <code>JIRA_EMAIL</code>, and '
            f'<code>JIRA_API_TOKEN</code> to be set in your <code>.env</code> file or environment. '
            f'After fetching, mapping is re-run automatically.'
            f'</div>',
            unsafe_allow_html=True,
        )
        lc1, lc2, lc3 = st.columns([1, 1, 1])
        with lc1:
            live_since   = st.number_input("Fetch last N days", min_value=1, max_value=365, value=7, key="live_since")
        with lc2:
            live_email   = st.text_input("Tester email (optional)", placeholder="tester@hpe.com", key="live_email")
        with lc3:
            live_writeback = st.checkbox("Write back to Jira after mapping", value=False, key="live_writeback")
            live_dry_run   = st.checkbox("Dry run (no writes)", value=False, key="live_dry_run")

        if st.button("🔄  Fetch from Jira & Map", key="live_fetch_btn", type="primary"):
            try:
                from jira_client import (
                    load_credentials, fetch_defects, test_connection,
                    write_back_confirmed_mappings,
                )
                creds = load_credentials()
                test  = test_connection(creds)
                if not test["ok"]:
                    st.error(f"Jira connection failed: {test['error']}")
                else:
                    with st.spinner(f"Fetching from {creds.base_url} …"):
                        conn2 = _open_write_conn(db_paths)
                        if conn2 is None:
                            st.error("Failed to open writeable database connection.")
                            st.stop()
                        live_records = []
                        for raw in fetch_defects(
                            creds,
                            since_days     = int(live_since),
                            reporter_email = live_email.strip() or creds.email,
                        ):
                            loader = load_jira_defects_from_list
                            if loader is None:
                                raise ImportError("load_jira_defects_from_list is unavailable")
                            parsed = loader([raw])
                            live_records.extend(parsed)
                        ingest_fn = ingest_jira_defects
                        if ingest_fn is None:
                            raise ImportError("ingest_jira_defects is unavailable")
                        istats = ingest_fn(conn2, live_records, overwrite=False)
                    st.info(f"Fetched {len(live_records)} issues — {istats['inserted']} new, {istats['skipped']} already present.")

                    with st.spinner("Running hybrid mapping …"):
                        mapper_fn = map_defects_to_test_results
                        if mapper_fn is None:
                            raise ImportError("map_defects_to_test_results is unavailable")
                        mstats = mapper_fn(
                            conn2,
                            tester_email = live_email.strip() or creds.email or None,
                            window_days  = int(live_since),
                        )
                    mode = "hybrid rule+semantic" if mstats.get("semantic_enabled") else "rule-only"
                    st.info(f"Mapping done ({mode}): {mstats['mappings_written']} written, {mstats['confirmed']} confirmed.")

                    if live_writeback:
                        with st.spinner("Writing back to Jira …"):
                            wstats = write_back_confirmed_mappings(
                                conn2, creds, dry_run=live_dry_run
                            )
                        label = "[DRY RUN] " if live_dry_run else ""
                        st.success(
                            f"✅ {label}Write-back: {wstats['comments_posted']} comments posted, "
                            f"{wstats['fields_updated']} fields updated, "
                            f"{wstats['skipped']} already synced."
                        )
                    conn2.close()
                    st.cache_resource.clear()
                    st.cache_data.clear()
                    st.rerun()

            except EnvironmentError as exc:
                st.error(
                    f"Missing Jira credentials: {exc}\n\n"
                    "Create a `.env` file with JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."
                )
            except ImportError:
                st.error(
                    "jira_client.py not found or `requests` not installed.\n"
                    "Run: `pip install requests python-dotenv`"
                )
            except Exception as exc:
                st.error(f"Unexpected error: {exc}")

    st.markdown('<div style="margin-top:.4rem;"></div>', unsafe_allow_html=True)

    # ── Tab layout within the section ────────────────────────────────────────
    dt1, dt2, dt3, dt4, dt5 = st.tabs([
        "🔗  Confirmed Mappings",
        "🔍  All Candidates",
        "📋  Defect Registry",
        "📡  Jira Sync Log",
        "⚠️  Duplicate Defects",
    ])

    # ════════════════════════════════════════════════════════════════════════
    # Tab 1 — Confirmed Mappings
    # ════════════════════════════════════════════════════════════════════════
    with dt1:
        df_conf = df_mappings[df_mappings["confirmed"] == 1].copy() if not df_mappings.empty else pd.DataFrame()

        if df_conf.empty:
            st.markdown(
                f'<div class="info-banner">'
                f'ℹ  No confirmed mappings yet.<br>'
                f'A mapping is confirmed when: reporter email matches the tester, '
                f'the defect was created within {DEFECT_WINDOW_DAYS} days of the run, '
                f'and the confidence score ≥ 0.5.<br>'
                f'Supply the <code>--tester-email</code> flag when running the pipeline, '
                f'or upload defects using the form above.'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            # ── Filters ──────────────────────────────────────────────────────
            f1, f2, f3 = st.columns([1.8, 1.4, 1.4])
            with f1:
                proj_opts = ["All projects"] + sorted(df_conf["defect_project"].dropna().unique().tolist())
                sel_proj  = st.selectbox("Project", proj_opts, key="conf_proj")
            with f2:
                test_opts = ["All tests"] + sorted(df_conf["test_name"].dropna().unique().tolist())
                sel_test  = st.selectbox("Test", test_opts, key="conf_test")
            with f3:
                status_opts = ["All statuses"] + sorted(df_conf["defect_status"].dropna().unique().tolist())
                sel_status  = st.selectbox("Defect status", status_opts, key="conf_status")

            df_show = df_conf.copy()
            if sel_proj   != "All projects":  df_show = df_show[df_show["defect_project"] == sel_proj]
            if sel_test   != "All tests":     df_show = df_show[df_show["test_name"]      == sel_test]
            if sel_status != "All statuses":  df_show = df_show[df_show["defect_status"]  == sel_status]

            st.markdown(
                f'<div style="font:400 0.75rem/1.6 \'JetBrains Mono\',monospace; '
                f'color:{C["muted"]}; margin:.6rem 0 1rem;">'
                f'Showing <b style="color:{C["txt"]};">{len(df_show)}</b> confirmed mapping'
                f'{"s" if len(df_show) != 1 else ""} · '
                f'Sorted by confidence ↓</div>',
                unsafe_allow_html=True,
            )

            # ── Detect multi-test defects ──────────────────────────────────
            defect_test_counts = df_show.groupby("defect_id")["test_name"].nunique()
            multi_defect_ids   = set(defect_test_counts[defect_test_counts > 1].index)

            # ── Render defect cards ────────────────────────────────────────
            for _, row in df_show.head(50).iterrows():                
                score       = float(row["confidence_score"])
                color       = _conf_color(score)
                is_multi    = row["defect_id"] in multi_defect_ids
                multi_html  = (
                    f'<span class="multi-badge">⛓ consolidated '
                    f'({defect_test_counts[row["defect_id"]]} tests)</span> '
                    if is_multi else ""
                )
                diff_days   = float(row["date_diff_days"])
                diff_label  = f"{diff_days:.1f}d apart"

                st.markdown(
                    f'<div class="defect-card defect-confirmed">'
                    f'  <div style="display:flex; align-items:center; gap:.7rem; margin-bottom:.2rem;">'
                    f'    <span class="defect-key">{row["defect_id"]}</span>'
                    f'    {_proj_tag(row["defect_project"])}'
                    f'    {_status_pill(row.get("defect_status",""))}'
                    f'    {multi_html}'
                    f'    <span style="margin-left:auto; font:400 0.7rem/1 \'JetBrains Mono\',monospace; '
                    f'color:{C["muted"]};">{diff_label}</span>'
                    f'  </div>'
                    f'  <div class="defect-summary">{row["defect_summary"]}</div>'
                    f'  <div class="defect-meta">'
                    f'    <b>Test:</b> {row["test_name"]} &nbsp;·&nbsp; '
                    f'    <b>Run:</b> {row["run_id"]} &nbsp;·&nbsp; '
                    f'    <b>Reporter:</b> {row.get("reporter_email","—")}'
                    f'  </div>'
                    f'  <div style="margin-top:.6rem;">'
                    f'    {_conf_bar(score)}'
                    f'  </div>'
                    f'  <div style="margin-top:.4rem;">{_reason_tags(row.get("match_reason",""))}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ════════════════════════════════════════════════════════════════════════
    # Tab 2 — All Candidates (confirmed + unconfirmed, with pre-condition flags)
    # ════════════════════════════════════════════════════════════════════════
    with dt2:
        if df_mappings.empty:
            st.markdown(
                f'<div class="info-banner">ℹ  No mapping candidates found. '
                f'Ingest defects and run the mapping step first.</div>',
                unsafe_allow_html=True,
            )
        else:
            # ── Filters ──────────────────────────────────────────────────────
            fa, fb, fc = st.columns([1.5, 1.5, 1])
            with fa:
                cand_proj_opts = ["All projects"] + sorted(df_mappings["defect_project"].dropna().unique().tolist())
                sel_cand_proj  = st.selectbox("Project", cand_proj_opts, key="cand_proj")
            with fb:
                cand_test_opts = ["All tests"] + sorted(df_mappings["test_name"].dropna().unique().tolist())
                sel_cand_test  = st.selectbox("Test", cand_test_opts, key="cand_test")
            with fc:
                conf_filter = st.selectbox(
                    "Confirmed",
                    ["All", "Confirmed only", "Unconfirmed only"],
                    key="cand_conf_filter",
                )

            df_cand = df_mappings.copy()
            if sel_cand_proj != "All projects": df_cand = df_cand[df_cand["defect_project"] == sel_cand_proj]
            if sel_cand_test != "All tests":    df_cand = df_cand[df_cand["test_name"]      == sel_cand_test]
            if conf_filter == "Confirmed only":   df_cand = df_cand[df_cand["confirmed"] == 1]
            elif conf_filter == "Unconfirmed only": df_cand = df_cand[df_cand["confirmed"] == 0]

            # ── Table ─────────────────────────────────────────────────────────
            rows_html = ""
            for _, row in df_cand.head(100).iterrows():
                score     = float(row["confidence_score"])
                color     = _conf_color(score)
                confirmed = int(row.get("confirmed", 0))
                em        = int(row.get("email_match", 0))
                dw        = int(row.get("date_within_window", 0))
                conf_mark = f'<span style="color:{C["green"]}; font-weight:700;">✓ Confirmed</span>' if confirmed else f'<span style="color:{C["muted"]};">Candidate</span>'
                em_mark   = f'<span style="color:{C["green"]};">✓</span>' if em else f'<span style="color:{C["amber"]};">–</span>'
                dw_mark   = f'<span style="color:{C["green"]};">✓</span>' if dw else f'<span style="color:{C["amber"]};">–</span>'
                rows_html += f"""
                <tr>
                  <td><span class="defect-key">{row['defect_id']}</span></td>
                  <td>{_proj_tag(row['defect_project'])}</td>
                  <td><span class="tname">{row['test_name']}</span></td>
                  <td>{_status_pill(row.get('defect_status',''))}</td>
                  <td>{em_mark}</td>
                  <td>{dw_mark} <span style="font:400 0.68rem/1 'JetBrains Mono',monospace; color:{C['muted']};">{float(row['date_diff_days']):.1f}d</span></td>
                  <td><span style="font:700 0.75rem/1 'JetBrains Mono',monospace; color:{color};">{score:.2f}</span></td>
                  <td>{conf_mark}</td>
                </tr>"""

            st.markdown(
                f'<table class="defect-table">'
                f'<thead><tr>'
                f'<th>Defect ID</th><th>Project</th><th>Test Name</th>'
                f'<th>Status</th><th>Email ✓</th><th>Date Window</th>'
                f'<th>Confidence</th><th>Confirmed</th>'
                f'</tr></thead>'
                f'<tbody>{rows_html}</tbody>'
                f'</table>',
                unsafe_allow_html=True,
            )

            st.markdown(
                f'<div style="font:400 0.74rem/1.6 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-top:.7rem;">'
                f'<b style="color:{C["txt"]}">Email ✓</b> — reporter_email matched the tester email supplied via <code>--tester-email</code>. '
                f'<b style="color:{C["txt"]}">Date Window</b> — defect was created within ±{DEFECT_WINDOW_DAYS} days of the run timestamp. '
                f'Both must hold for a mapping to be <b style="color:{C["green"]}">Confirmed</b>.</div>',
                unsafe_allow_html=True,
            )

    # ════════════════════════════════════════════════════════════════════════
    # Tab 3 — Defect Registry  (all ingested Jira issues)
    # ════════════════════════════════════════════════════════════════════════
    with dt3:
        if df_defects.empty:
            st.markdown(
                f'<div class="info-banner">ℹ  No Jira defects ingested. '
                f'Use the uploader above or run:<br>'
                f'<code>python pipeline.py --ingest-jira ./defects.json</code></div>',
                unsafe_allow_html=True,
            )
        else:
            # Summary by project
            proj_summary = df_defects.groupby("project").size().reset_index(name="count")
            pcols = st.columns(min(len(proj_summary), 5))
            for i, (_, row) in enumerate(proj_summary.iterrows()):
                with pcols[i % len(pcols)]:
                    st.markdown(
                        f'<div class="metric-card mc-purple">'
                        f'  <div class="mc-label">{row["project"]}</div>'
                        f'  <div class="mc-value purple">{row["count"]}</div>'
                        f'  <div class="mc-sub">issues ingested</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            st.markdown('<div style="margin-top:1.2rem;"></div>', unsafe_allow_html=True)

            # ── Per-defect registry cards ─────────────────────────────────
            mapped_ids  = set(df_mappings["defect_id"].unique()) if not df_mappings.empty else set()
            conf_ids    = set(df_mappings[df_mappings["confirmed"]==1]["defect_id"].unique()) if not df_mappings.empty else set()

            dr_f1, dr_f2 = st.columns([2, 1.2])
            with dr_f1:
                reg_proj_opts = ["All projects"] + sorted(df_defects["project"].dropna().unique().tolist())
                sel_reg_proj  = st.selectbox("Filter by project", reg_proj_opts, key="reg_proj")
            with dr_f2:
                reg_status_opts = ["All statuses"] + sorted(df_defects["status"].dropna().unique().tolist())
                sel_reg_status  = st.selectbox("Filter by status", reg_status_opts, key="reg_status")

            df_reg = df_defects.copy()
            if sel_reg_proj   != "All projects":  df_reg = df_reg[df_reg["project"] == sel_reg_proj]
            if sel_reg_status != "All statuses":  df_reg = df_reg[df_reg["status"]  == sel_reg_status]

            for _, row in df_reg.head(50).iterrows():
                did     = row["defect_id"]
                is_conf = did in conf_ids
                is_map  = did in mapped_ids
                card_cls = "defect-confirmed" if is_conf else ("defect-candidate" if is_map else "defect-unmatched")
                link_badge = (
                    f'<span class="mc-badge badge-green">✓ confirmed</span>'
                    if is_conf else
                    f'<span class="mc-badge badge-amber">~ candidate</span>'
                    if is_map else
                    f'<span class="mc-badge badge-blue">unmapped</span>'
                )
                labels = _parse_labels(row.get("labels",""))
                label_html = " ".join(
                    f'<span style="display:inline-block; padding:1px 7px; border-radius:4px; '
                    f'margin:2px 2px 2px 0; font:400 0.67rem/1.6 \'JetBrains Mono\',monospace; '
                    f'background:{C["border"]}44; color:{C["muted"]}; '
                    f'border:1px solid {C["border"]};">{lbl}</span>'
                    for lbl in labels
                )
                created_str = str(row.get("created",""))[:10]
                st.markdown(
                    f'<div class="defect-card {card_cls}">'
                    f'  <div style="display:flex; align-items:center; gap:.6rem; margin-bottom:.25rem;">'
                    f'    <span class="defect-key">{did}</span>'
                    f'    {_proj_tag(row["project"])}'
                    f'    {_status_pill(row.get("status",""))}'
                    f'    {link_badge}'
                    f'    <span style="margin-left:auto; font:400 0.7rem/1 \'JetBrains Mono\',monospace; '
                    f'color:{C["muted"]};">{created_str}</span>'
                    f'  </div>'
                    f'  <div class="defect-summary">{row["summary"]}</div>'
                    f'  <div class="defect-meta">'
                    f'    <b>Reporter:</b> {row.get("reporter_email","—")} &nbsp;·&nbsp; '
                    f'    <b>Priority:</b> {row.get("priority","—")} &nbsp;·&nbsp; '
                    f'    <b>Type:</b> {row.get("issue_type","—")}'
                    f'  </div>'
                    f'  <div style="margin-top:.5rem;">{label_html}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ════════════════════════════════════════════════════════════════════════
    # Tab 4 — Jira Sync Log
    # ════════════════════════════════════════════════════════════════════════
    with dt4:
        conn_sl = _open_db_conn(db_paths)
        tables_sl = (
            {r[0] for r in conn_sl.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if conn_sl else set()
        )

        if "jira_sync_log" not in tables_sl or conn_sl is None:
            st.markdown(
                f'<div class="info-banner">ℹ  Jira sync log table not found. '
                f'Apply the updated <code>schema.sql</code> and run '
                f'<code>python pipeline.py --writeback</code> to populate it.</div>',
                unsafe_allow_html=True,
            )
        else:
            try:
                from jira_client import get_sync_stats, load_sync_log
                sync_stats = get_sync_stats(conn_sl)
                sync_rows  = load_sync_log(conn_sl)

                # ── KPI strip ─────────────────────────────────────────────────
                sc1, sc2, sc3, sc4 = st.columns(4)
                sync_kpis = [
                    (sc1, "TOTAL SYNCED",   sync_stats["total_synced"],  "blue"),
                    (sc2, "SUCCESSFUL",     sync_stats["successes"],     "green"),
                    (sc3, "ERRORS",         sync_stats["errors"],        "amber"),
                    (sc4, "LAST SYNC",      str(sync_stats["last_sync_at"])[:16], "purple"),
                ]
                for col, label, value, color in sync_kpis:
                    with col:
                        st.markdown(
                            f'<div class="metric-card mc-{color}">'
                            f'  <div class="mc-label">{label}</div>'
                            f'  <div class="mc-value {color}">{value}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                st.markdown('<div style="margin-top:1.2rem;"></div>', unsafe_allow_html=True)

                pending_count = conn_sl.execute(
                    """
                    SELECT COUNT(*) AS pending
                    FROM defect_test_mappings m
                    LEFT JOIN jira_sync_log s ON m.mapping_id = s.mapping_id
                    WHERE m.confirmed = 1 AND s.mapping_id IS NULL
                    """
                ).fetchone()["pending"] or 0
                total_confirmed = conn_sl.execute(
                    "SELECT COUNT(*) AS total_confirmed FROM defect_test_mappings WHERE confirmed = 1"
                ).fetchone()["total_confirmed"] or 0

                wb_c1, wb_c2, wb_c3, wb_c4 = st.columns([1, 1, 1, 1])
                with wb_c1:
                    st.metric("Pending confirmed", pending_count)
                with wb_c2:
                    st.metric("Total confirmed", total_confirmed)
                with wb_c3:
                    wb_dry_run = st.checkbox("Dry run", value=True, key="sync_log_dry_run")
                    wb_force = st.checkbox("Force re-write", value=False, key="sync_log_force")
                with wb_c4:
                    wb_update_field = st.checkbox("Update custom field", value=True, key="sync_log_update_field")

                if st.button("📤 Write back confirmed mappings", key="sync_log_writeback_btn", type="primary"):
                    try:
                        from jira_client import load_credentials, write_back_confirmed_mappings
                        creds = load_credentials()
                        with st.spinner("Writing back confirmed mappings …"):
                            conn2 = _open_write_conn(db_paths)
                            if conn2 is None:
                                raise RuntimeError("Failed to open writeable database connection.")
                            wstats = write_back_confirmed_mappings(
                                conn2,
                                creds,
                                dashboard_url = os.environ.get("DASHBOARD_URL"),
                                dry_run       = wb_dry_run,
                                update_field  = wb_update_field,
                                force         = wb_force,
                            )
                            conn2.close()
                        label = "[DRY RUN] " if wb_dry_run else ""
                        st.success(
                            f"✅ {label}Write-back: {wstats['comments_posted']} comments posted, "
                            f"{wstats['fields_updated']} fields updated, "
                            f"{wstats['skipped']} skipped, {wstats['errors']} errors."
                        )
                        st.cache_resource.clear()
                        st.cache_data.clear()
                        st.rerun()
                    except EnvironmentError as exc:
                        st.error(
                            f"Missing Jira credentials: {exc}\n\n"
                            "Create a `.env` file with JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."
                        )
                    except ImportError:
                        st.error(
                            "jira_client.py not found or `requests` not installed.\n"
                            "Write-back is unavailable in the dashboard."
                        )
                    except Exception as exc:
                        st.error(f"Write-back failed: {exc}")

                if not sync_rows:
                    st.markdown(
                        f'<div class="info-banner">ℹ  No sync entries yet. '
                        f'Run <code>python pipeline.py --writeback</code> to post confirmed '
                        f'mappings as Jira comments.</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    # Build Jira base URL for hyperlinks
                    jira_base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")

                    rows_html = ""
                    for r in sync_rows:
                        s = str(r["status"])
                        if s == "success":
                            status_html = f'<span class="status-pill status-closed">✓ success</span>'
                        elif s == "error":
                            status_html = f'<span class="status-pill status-triage">✗ error</span>'
                        else:
                            status_html = f'<span class="status-pill status-other">– skipped</span>'

                        dry_html = (
                            f'<span style="font:400 0.66rem/1 \'JetBrains Mono\',monospace; '
                            f'color:{C["amber"]};">dry run</span>'
                            if r["dry_run"] else ""
                        )
                        err_html = (
                            f'<span style="font:400 0.68rem/1.4 \'JetBrains Mono\',monospace; '
                            f'color:{C["amber"]}; display:block;">{r["error_msg"]}</span>'
                            if r["error_msg"] else ""
                        )
                        did = r["defect_id"]
                        if jira_base:
                            did_html = (
                                f'<a href="{jira_base}/browse/{did}" target="_blank" '
                                f'style="color:{C["blue"]}; text-decoration:none; '
                                f'font:700 0.78rem/1 \'JetBrains Mono\',monospace;">{did}</a>'
                            )
                        else:
                            did_html = f'<span class="defect-key">{did}</span>'

                        written = str(r["written_at"] or "")[:16]
                        rows_html += f"""
                        <tr>
                          <td>{did_html}</td>
                          <td><span style="font:400 0.75rem/1 'JetBrains Mono',monospace;
                              color:{C['txt']};">{r['run_id']}</span></td>
                          <td>{status_html} {dry_html}</td>
                          <td><span style="font:400 0.7rem/1 'JetBrains Mono',monospace;
                              color:{C['muted']};">{written}</span></td>
                          <td>{err_html}</td>
                        </tr>"""

                    st.markdown(
                        f'<table class="defect-table">'
                        f'<thead><tr>'
                        f'<th>Defect</th><th>Run ID</th><th>Status</th>'
                        f'<th>Written At</th><th>Error</th>'
                        f'</tr></thead>'
                        f'<tbody>{rows_html}</tbody>'
                        f'</table>',
                        unsafe_allow_html=True,
                    )

                    if jira_base:
                        st.markdown(
                            f'<div style="font:400 0.72rem/1.6 \'JetBrains Mono\',monospace; '
                            f'color:{C["muted"]}; margin-top:.6rem;">'
                            f'Defect keys link to <b style="color:{C["txt"]};">{jira_base}</b></div>',
                            unsafe_allow_html=True,
                        )

            except ImportError:
                st.markdown(
                    f'<div class="warn-banner">⚠  jira_client.py not found — '
                    f'sync log stats unavailable.</div>',
                    unsafe_allow_html=True,
                )

    # ════════════════════════════════════════════════════════════════════════
    # Tab 5 — Duplicate Defect Detection
    # ════════════════════════════════════════════════════════════════════════
    with dt5:
        st.markdown(
            f'<div class="defect-meta" style="margin-bottom:1rem; line-height:1.8;">'
            f'Surfaces pairs of Jira defects that likely describe <b>the same underlying failure</b> '
            f'and should be merged or linked in Jira.<br>'
            f'Detection criteria: same TC_* test name in both summaries, created within the date window, '
            f'and cosine similarity ≥ threshold between their cached embeddings '
            f'(falls back to Jaccard overlap if embeddings are not yet computed).'
            f'</div>',
            unsafe_allow_html=True,
        )

        dup_c1, dup_c2 = st.columns([1, 1])
        with dup_c1:
            dup_threshold = st.slider(
                "Similarity threshold", min_value=0.70, max_value=1.00,
                value=0.90, step=0.01, key="dup_threshold_slider",
            )
        with dup_c2:
            dup_window = st.number_input(
                "Date window (days)", min_value=1, max_value=90,
                value=7, key="dup_window",
            )

        if st.button("🔍  Detect Duplicates", key="detect_dup_btn"):
            try:
                from jira_client import detect_duplicate_defects
                conn_dup = _open_db_conn(db_paths)
                if conn_dup is None:
                    st.error("Database not found.")
                else:
                    with st.spinner("Scanning for duplicates …"):
                        dups = detect_duplicate_defects(
                            conn_dup,
                            model_name    = DEFAULT_EMBED_MODEL,
                            sim_threshold = dup_threshold,
                            window_days   = dup_window,
                        )

                    if not dups:
                        st.success(
                            f"✅  No duplicate pairs found above threshold {dup_threshold:.2f}."
                        )
                    else:
                        st.warning(f"⚠  {len(dups)} potential duplicate pair(s) detected:")
                        jira_base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")

                        for dup in dups:
                            sim_color = _conf_color(dup["similarity"])
                            def _did_link(did: str) -> str:
                                if jira_base:
                                    return (
                                        f'<a href="{jira_base}/browse/{did}" target="_blank" '
                                        f'style="color:{C["blue"]}; font:700 0.78rem/1 '
                                        f'\'JetBrains Mono\',monospace; text-decoration:none;">{did}</a>'
                                    )
                                return f'<span class="defect-key">{did}</span>'

                            st.markdown(
                                f'<div class="defect-card defect-candidate">'
                                f'  <div style="display:flex; align-items:center; gap:.8rem;">'
                                f'    {_did_link(dup["defect_a"])}'
                                f'    <span style="color:{C["muted"]}; font-size:1.1rem;">↔</span>'
                                f'    {_did_link(dup["defect_b"])}'
                                f'    <span style="margin-left:auto;">{_conf_bar(dup["similarity"])}</span>'
                                f'  </div>'
                                f'  <div class="defect-meta" style="margin-top:.5rem;">'
                                f'    <b>Date diff:</b> {dup["date_diff_days"]} days &nbsp;·&nbsp; '
                                f'    <b>Shared tests:</b> {", ".join(dup["shared_test_names"])}'
                                f'  </div>'
                                f'  <div style="margin-top:.4rem;">{_reason_tags(dup["reason"])}</div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

            except ImportError:
                st.error(
                    "jira_client.py not found or `requests` not installed.\n"
                    "Run: `pip install requests python-dotenv`"
                )
            except Exception as exc:
                st.error(f"Error during duplicate detection: {exc}")


def render_footer(db_paths: list[str], df_runs: pd.DataFrame) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dbs = ", ".join(Path(p).name for p in db_paths) if db_paths else "demo"
    st.markdown(
        f'<div class="dash-footer">'
        f'  <span>Phase 3 + Defect Mapping · schema.sql v2 · Option B multi-DB</span>'
        f'  <span>{len(df_runs)} runs · refreshed {now} · {dbs}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _parse_db_paths() -> list[str]:
    return [str((Path.cwd() / DEFAULT_DB).resolve())]

def main() -> None:
    inject_css()
    db_paths = _parse_db_paths()
    db_key = "|".join(sorted(db_paths))
    with st.spinner("Opening database connections…"):
        result = get_db_data(db_key)

    # Guard against unexpected return value from get_db_data
    if not isinstance(result, (list, tuple)):
        st.error("Failed to open database connections.")
        st.stop()

    if len(result) == 3:
        df_runs, df_results, _ = cast(tuple[pd.DataFrame, pd.DataFrame, object], result)
    elif len(result) == 2:
        df_runs, df_results = cast(tuple[pd.DataFrame, pd.DataFrame], result)
    else:
        st.error("Unexpected data returned from database.")
        st.stop()
    if df_runs.empty:
        st.error("No run data found. Please run `python pipeline2.py` first.")
        st.stop()

    db_sources = sorted(df_runs["_source_db"].unique().tolist()) if "_source_db" in df_runs.columns else ["unknown"]
    selected_source, selected_week, selected_run_id, drift_test, show_trend, show_run_inspector, show_duration_drift, show_health_matrix, show_defect_mapping = render_sidebar(df_runs, db_paths, db_sources)

    df_runs_f = filter_by_source(df_runs, selected_source)
    df_results_f = filter_by_source(df_results, selected_source)

    total_runs_f = len(df_runs_f)
    max_weeks_f = max(1, total_runs_f // 7)
    if selected_week > max_weeks_f:
        selected_week = max_weeks_f

    render_header(df_runs_f)
    render_ml_insights_cta()
    render_weekly_health(df_runs_f, df_results_f, selected_week)
    if show_trend:
        render_trend(df_runs_f)
    if show_run_inspector:
        render_run_inspector(df_results_f, selected_run_id)
    if show_duration_drift:
        render_duration_drift(df_results_f, drift_test, selected_source)
    if show_health_matrix:
        render_test_health_matrix(df_results_f, selected_source)
    if show_defect_mapping:
        render_defect_mapping(db_paths)
    render_footer(db_paths, df_runs_f)

if __name__ == "__main__":
    main()