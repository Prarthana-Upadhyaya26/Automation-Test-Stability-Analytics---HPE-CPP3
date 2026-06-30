"""
Test Stability Analytics Dashboard
====================================
Streamlit dashboard for CI test run analytics backed by schema.sql.

Usage:
  streamlit run dashboard.py
  streamlit run dashboard.py -- --db ./analytics.db
  streamlit run dashboard.py -- --db ./analytics.db ./teambravo.db
"""

import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from pipeline2 import load_multi_db
    PIPELINE2_AVAILABLE = True
except ImportError:
    PIPELINE2_AVAILABLE = False

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

    [data-testid="stTable"] table {{
        border-collapse: collapse; width: 100%;
        font: 400 0.8rem/1.55 'IBM Plex Sans', sans-serif;
    }}
    [data-testid="stTable"] th {{
        background: {C["bg2"]}; color: {C["muted"]};
        font: 600 0.66rem/1 'JetBrains Mono', monospace;
        text-transform: uppercase; letter-spacing: .1em;
        padding: .55rem .85rem; border-bottom: 1px solid {C["border"]};
        white-space: nowrap; text-align: left;
    }}
    [data-testid="stTable"] td {{
        color: {C["txt"]}; padding: .48rem .85rem;
        border-bottom: 1px solid {C["border"]}44;
        word-break: break-word; white-space: normal;
        vertical-align: top;
    }}
    [data-testid="stTable"] tr:hover td {{
        background: {C["card"]};
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

pd.DataFrame.safe_sort_runs = lambda self, *args, **kwargs: safe_sort_runs(self, *args, **kwargs)

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
            rng2  = rng.choice(["Oct 2024", "last 30 days", "Q4 2024"])
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
    start_dt = datetime(2024, 10, 1)
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
    df = df_runs.copy().safe_sort_runs("run_id").reset_index(drop=True)
    roll = df["pass_rate_pct"].rolling(window=ROLLING_WINDOW, min_periods=3)
    df["roll_mean"] = roll.mean().shift(1)
    df["roll_std"] = roll.std().shift(1).fillna(5.0)
    df["z_score"] = (df["roll_mean"] - df["pass_rate_pct"]) / df["roll_std"].clip(lower=1.0)
    df["anomaly"] = df["z_score"] >= ANOMALY_SIGMA
    return df

def get_failures_for_run(df_results: pd.DataFrame, run_id: str) -> pd.DataFrame:
    df = df_results[(df_results["run_id"] == run_id) & (df_results["status"] == "FAIL")].copy()
    def classify(msg):
        if not msg:
            return "unknown"
        m = str(msg).lower()
        if "still visible" in m:
            return "timeout"
        if "not found after" in m:
            return "element"
        if "expected http" in m:
            return "assertion"
        if "csv export" in m:
            return "data"
        if "environment" in m or "unreachable" in m:
            return "environment"
        return "unknown"
    df["failure_category"] = df["failure_msg"].apply(classify)
    df["keyword_name"] = df["failure_kw"].fillna("—")
    return df.safe_sort_runs("test_name")

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
    df = df.safe_sort_runs("run_timestamp").reset_index(drop=True)
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
        .safe_sort_runs("run_timestamp")["run_id"]
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
    def classify(msg):
        if not msg:
            return "unknown"
        m = str(msg).lower()
        if "still visible" in m:
            return "timeout"
        if "not found after" in m:
            return "element"
        if "expected http" in m:
            return "assertion"
        if "csv export" in m:
            return "data"
        if "environment" in m or "unreachable" in m:
            return "environment"
        return "unknown"
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
    failures["fail_cat"] = failures["failure_msg"].apply(classify)
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
        show_jira_linkage = st.checkbox("JIRA Defect Linkage", value=False)
        st.markdown("---")

        st.markdown(f'<div class="sb-label">Run Inspector</div>', unsafe_allow_html=True)
        run_ids_sorted = df_runs.safe_sort_runs("timestamp", ascending=False)["run_id"].tolist()
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

    return selected_source, selected_week, selected_run_id, drift_test, show_trend, show_run_inspector, show_duration_drift, show_health_matrix, show_jira_linkage


def render_header(df_runs: pd.DataFrame) -> None:
    if df_runs.empty:
        return
    latest = df_runs.safe_sort_runs("run_id").iloc[-1]
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

    st.markdown('<div style="margin-top:2rem; padding-top:1.5rem; border-top:1px solid ' + C["border"] + '; font:700 1.05rem/1 \'IBM Plex Sans\',sans-serif; color:' + C["txt"] + '; display:flex; align-items:center; gap:8px; margin-bottom:1rem;"><span style="background:' + C["blue"] + '18; color:' + C["blue"] + '; font:700 0.62rem/1 \'JetBrains Mono\',monospace; letter-spacing:.14em; text-transform:uppercase; padding:4px 10px; border-radius:5px; border:1px solid ' + C["blue"] + '33;">FAILURE FLOW</span> Sankey Diagram</div>', unsafe_allow_html=True)
    phase_filter = st.selectbox(
        "Filter by phase",
        ["All phases", "Phase 1 (1–25)", "Phase 2 (26–45)", "Phase 3 (46–75)", "Phase 4 (76–100)"],
        key="sankey_phase",
    )
    fig_sankey = chart_sankey(df_results, phase_filter)
    st.plotly_chart(fig_sankey, use_container_width=True, config={"displayModeBar": False})
    st.markdown(
        f'<div style="font:400 0.76rem/1.6 \'JetBrains Mono\',monospace; color:{C["muted"]}; margin-top:.2rem;">'
        f'Flows left-to-right: failure category → affected test → run phase where the failure occurred.</div>',
        unsafe_allow_html=True,
    )

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
    _section("TEST HEALTH MATRIX", "Heatmap & Stability Spectrum", "")
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

    all_flaky = compute_flaky_from_results(df_results)
    flaky_df = all_flaky[all_flaky["failure_rate"] >= 25.0].sort_values("failure_rate", ascending=False)
    if not flaky_df.empty:
        st.markdown(f'<div style="font:700 1rem/1 \'IBM Plex Sans\',sans-serif; color:{C["txt"]}; margin:2rem 0 1rem; display:flex; align-items:center; gap:8px;"><span style="width:14px;height:14px;border-radius:3px;background:{C["blue"]};display:inline-block;"></span>Test Stability Spectrum</div>', unsafe_allow_html=True)
        cols = st.columns(min(5, len(flaky_df)))
        for i, (_, row) in enumerate(flaky_df.iterrows()):
            col_idx = i % len(cols)
            fail_rate = float(row["failure_rate"])
            flips = int(row["flip_count"])
            total_runs = int(row["total_runs"])
            fails = int(row["fail_count"])
            if fail_rate >= 70:
                signal_label, signal_color = "🔴 BROKEN", C["red"]
            elif fail_rate >= 60:
                signal_label, signal_color = "🟠 FLAKY·HEAVY", C["orange"]
            elif fail_rate >= 40:
                signal_label, signal_color = "🟡 FLAKY·MODERATE", C["amber"]
            else:
                signal_label, signal_color = "🔵 FLAKY·MILD", C["blue"]
            short_name = row["test_name"].replace("TC_", "").replace("_", " ")
            with cols[col_idx]:
                st.markdown(
                    f'<div class="metric-card" style="border-left:5px solid {signal_color}; padding:1.2rem;">'
                    f'  <div style="font:700 0.75rem/1; text-transform:uppercase; letter-spacing:.1em; color:{signal_color};">{signal_label}</div>'
                    f'  <div style="font:600 0.85rem/1.3; color:{C["txt"]}; margin:.4rem 0 .6rem;">{short_name}</div>'
                    f'  <div style="font:700 2.8rem/1; color:{signal_color};">{fail_rate:.0f}%</div>'
                    f'  <div class="mc-sub" style="margin-bottom:.5rem;">Failure Rate</div>'
                    f'  <div style="font:400 0.72rem/1.5; color:{C["muted"]};">{fails} fails · {flips} flips · {total_runs} runs</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

def render_jira_linkage(db_paths: list[str]) -> None:
    """JIRA Defect Linkage — KPIs, charts, confirmed/pending/all-defects tabs."""
    _section("JIRA", "Defect Linkage", "Automated defect → test run mapping")

    db_path = db_paths[0] if db_paths else DEFAULT_DB
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
    except Exception as e:
        st.error(f"Cannot connect to database: {e}")
        return

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "jira_defects" not in tables or "defect_test_links" not in tables:
        st.info(
            "No JIRA data found. Run `python jira_ingest.py defects.json` to import defects "
            "and generate candidate links."
        )
        conn.close()
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    _ALL_STRATEGIES = ["exact_name", "label_dict", "keyword_area", "keyword", "semantic"]
    fc1, fc2 = st.columns([1, 2])
    with fc1:
        teams = ["All"] + [
            r[0] for r in conn.execute(
                "SELECT DISTINCT team FROM runs ORDER BY team"
            ).fetchall()
        ]
        team_filter = st.selectbox("Filter by team", teams, key="jira_team_filter")
    with fc2:
        strategy_filter = st.multiselect(
            "Filter by strategy", _ALL_STRATEGIES,
            default=_ALL_STRATEGIES, key="jira_strategy_filter",
        )
    if not strategy_filter:
        strategy_filter = _ALL_STRATEGIES
    # Used as (team_filter, team_filter) in: WHERE (? = 'All' OR r.team = ?)
    tf = (team_filter, team_filter)

    # ── KPIs ──────────────────────────────────────────────────────────────────
    total_defects = conn.execute("SELECT COUNT(*) FROM jira_defects").fetchone()[0]
    total_links = conn.execute(
        "SELECT COUNT(*) FROM defect_test_links dtl "
        "JOIN runs r ON dtl.run_id = r.run_id "
        "WHERE (? = 'All' OR r.team = ?)", tf,
    ).fetchone()[0]
    confirmed = conn.execute(
        "SELECT COUNT(*) FROM defect_test_links dtl "
        "JOIN runs r ON dtl.run_id = r.run_id "
        "WHERE dtl.confirmed = 1 AND (? = 'All' OR r.team = ?)", tf,
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM defect_test_links dtl "
        "JOIN runs r ON dtl.run_id = r.run_id "
        "WHERE dtl.confirmed = 0 AND (? = 'All' OR r.team = ?)", tf,
    ).fetchone()[0]
    rejected = conn.execute(
        "SELECT COUNT(*) FROM defect_test_links dtl "
        "JOIN runs r ON dtl.run_id = r.run_id "
        "WHERE dtl.confirmed = -1 AND (? = 'All' OR r.team = ?)", tf,
    ).fetchone()[0]

    k1, k2, k3, k4, k5 = st.columns(5)
    for col, label, value, color, sub in [
        (k1, "Defects Imported",  total_defects, "blue",                                 "Total in DB"),
        (k2, "Candidate Links",   total_links,   "blue",                                 "All strategies"),
        (k3, "Auto-Confirmed",    confirmed,     "green",                                "Score ≥ 70"),
        (k4, "Pending Review",    pending,       "amber" if pending > 0 else "green",    "Score 40–69"),
        (k5, "Rejected",          rejected,      "red",                                  "Score &lt; 40 or manual"),
    ]:
        with col:
            st.markdown(
                f'<div class="metric-card mc-{color}">'
                f'  <div class="mc-label">{label}</div>'
                f'  <div class="mc-value {color}">{value}</div>'
                f'  <div class="mc-sub">{sub}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="margin-top:1.8rem;"></div>', unsafe_allow_html=True)

    # ── Charts ────────────────────────────────────────────────────────────────
    STRATEGY_COLORS = {
        "exact_name": C["blue"],
        "label_dict": C["purple"],
        "keyword":    C["orange"],
        "semantic":   C["teal"],
    }

    ch1, ch2 = st.columns(2)

    with ch1:
        df_cov = pd.read_sql_query(
            """SELECT dtl.jira_key, COUNT(*) AS link_count,
                      MAX(dtl.match_strategy) AS match_strategy
               FROM defect_test_links dtl
               JOIN runs r ON dtl.run_id = r.run_id
               WHERE dtl.confirmed = 1 AND (? = 'All' OR r.team = ?)
               GROUP BY dtl.jira_key
               ORDER BY link_count DESC""",
            conn, params=tf,
        )
        if not df_cov.empty:
            bar_colors = [STRATEGY_COLORS.get(s, C["muted"]) for s in df_cov["match_strategy"]]
            fig_cov = go.Figure(layout=dark_layout(height=340, margin=dict(l=10, r=20, t=36, b=10)))
            fig_cov.add_trace(go.Bar(
                x=df_cov["link_count"],
                y=df_cov["jira_key"],
                orientation="h",
                marker=dict(color=bar_colors, line=dict(width=0)),
                hovertemplate="<b>%{y}</b><br>Linked runs: <b>%{x}</b><extra></extra>",
            ))
            fig_cov.update_layout(
                title=dict(
                    text="Defect Coverage — linked failing runs per defect",
                    font=dict(size=11, color=C["muted"]), x=0,
                ),
                yaxis=dict(autorange="reversed"),
                xaxis=dict(
                    title=dict(text="Linked Run Count", font=dict(size=10, color=C["muted"])),
                    dtick=1,
                ),
                showlegend=False,
            )
            st.plotly_chart(fig_cov, use_container_width=True, config={"displayModeBar": False})
            legend_html = "  ".join(
                f'<span style="color:{clr}; font:600 0.68rem/1 \'JetBrains Mono\',monospace;">'
                f'&#9632; {strat.replace("_", " ").upper()}</span>'
                for strat, clr in STRATEGY_COLORS.items()
            )
            st.markdown(f'<div style="margin-top:.2rem;">{legend_html}</div>', unsafe_allow_html=True)

    with ch2:
        df_tc = pd.read_sql_query(
            """SELECT dtl.test_name,
                      COUNT(DISTINCT dtl.jira_key) AS defect_count
               FROM defect_test_links dtl
               JOIN runs r ON dtl.run_id = r.run_id
               WHERE dtl.confirmed = 1 AND (? = 'All' OR r.team = ?)
               GROUP BY dtl.test_name
               ORDER BY defect_count DESC
               LIMIT 15""",
            conn, params=tf,
        )
        if not df_tc.empty:
            df_tc["short_name"] = df_tc["test_name"].str.replace("TC_", "", regex=False)
            fig_tc = go.Figure(layout=dark_layout(height=340, margin=dict(l=10, r=20, t=36, b=90)))
            fig_tc.add_trace(go.Bar(
                x=df_tc["short_name"],
                y=df_tc["defect_count"],
                marker=dict(color=C["purple"], opacity=0.85, line=dict(width=0)),
                hovertemplate="<b>%{x}</b><br>Linked defects: <b>%{y}</b><extra></extra>",
            ))
            fig_tc.update_layout(
                title=dict(
                    text="Test Coverage — distinct defects mapped per test",
                    font=dict(size=11, color=C["muted"]), x=0,
                ),
                xaxis=dict(tickangle=-40, tickfont=dict(size=9)),
                yaxis=dict(
                    title=dict(text="Distinct Defects", font=dict(size=10, color=C["muted"])),
                    dtick=1,
                ),
                showlegend=False,
            )
            st.plotly_chart(fig_tc, use_container_width=True, config={"displayModeBar": False})

    st.markdown('<div style="margin-top:.4rem;"></div>', unsafe_allow_html=True)

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_confirmed, tab_pending, tab_all = st.tabs(["Confirmed Links", "Pending Review", "All Defects"])

    # ── Confirmed Links ───────────────────────────────────────────────────────
    with tab_confirmed:
        # Defect-grouped summary
        df_grouped = pd.read_sql_query(
            """SELECT dtl.jira_key, jd.summary, jd.project, jd.status, jd.priority,
                      GROUP_CONCAT(DISTINCT dtl.test_name) AS linked_test_names,
                      COUNT(DISTINCT dtl.test_name) AS test_count,
                      COUNT(*) AS run_count,
                      MAX(dtl.match_strategy) AS match_strategy
               FROM defect_test_links dtl
               JOIN jira_defects jd ON dtl.jira_key = jd.jira_key
               JOIN runs r ON dtl.run_id = r.run_id
               WHERE dtl.confirmed = 1 AND (? = 'All' OR r.team = ?)
               GROUP BY dtl.jira_key
               ORDER BY run_count DESC""",
            conn, params=tf,
        )
        df_grouped = df_grouped[df_grouped["match_strategy"].isin(strategy_filter)]

        # Flat link table with cosine_sim_score (loaded before summary so we can build per-test sim strings)
        df_conf = pd.read_sql_query(
            """SELECT dtl.jira_key, dtl.test_name, dtl.run_id,
                      dtl.date_delta_days, dtl.match_strategy,
                      dtl.cosine_sim_score,
                      jd.summary, jd.status AS jira_status,
                      jd.priority, jd.reporter_email
               FROM defect_test_links dtl
               JOIN jira_defects jd ON dtl.jira_key = jd.jira_key
               JOIN runs r ON dtl.run_id = r.run_id
               WHERE dtl.confirmed = 1 AND (? = 'All' OR r.team = ?)
               ORDER BY dtl.cosine_sim_score DESC NULLS LAST, dtl.date_delta_days ASC""",
            conn, params=tf,
        )
        df_conf = df_conf[df_conf["match_strategy"].isin(strategy_filter)]

        if not df_grouped.empty:
            df_grouped["linked_test_names"] = df_grouped["linked_test_names"].fillna("").str.replace(",", ", ", regex=False)

            # Build "TestA: 56%, TestB: 41%" per-defect strings from the flat link data
            if not df_conf.empty:
                def _sim_pairs(gdf):
                    parts = []
                    for _, row in gdf.sort_values("cosine_sim_score", ascending=False).drop_duplicates("test_name").iterrows():
                        if row["match_strategy"] == "semantic" and pd.notna(row["cosine_sim_score"]):
                            parts.append(f"{row['test_name']}: {row['cosine_sim_score'] * 100:.0f}%")
                    return ", ".join(parts) if parts else "—"
                sim_map = df_conf.groupby("jira_key").apply(_sim_pairs)
                df_grouped["cosine_sim"] = df_grouped["jira_key"].map(sim_map).fillna("—")
            else:
                df_grouped["cosine_sim"] = "—"

            st.markdown(
                f'<div style="font:600 0.68rem/1 \'JetBrains Mono\',monospace; '
                f'text-transform:uppercase; letter-spacing:.12em; '
                f'color:{C["muted"]}; margin-bottom:.5rem;">Defect Summary</div>',
                unsafe_allow_html=True,
            )
            tbl = df_grouped[["jira_key", "summary", "project", "status", "priority",
                               "linked_test_names", "test_count", "run_count",
                               "cosine_sim", "match_strategy"]].copy()
            tbl.columns = ["JIRA Key", "Summary", "Project", "Status", "Priority",
                           "Tests Linked", "# Tests", "Runs", "Cosine Sim", "Strategy"]
            st.table(tbl)
            st.markdown('<div style="margin-top:1.4rem;"></div>', unsafe_allow_html=True)

        if df_conf.empty:
            st.info("No confirmed links match the selected strategy filter.")
        else:
            st.markdown(
                f'<div style="font:600 0.68rem/1 \'JetBrains Mono\',monospace; '
                f'text-transform:uppercase; letter-spacing:.12em; '
                f'color:{C["muted"]}; margin-bottom:.5rem;">All Links (flat view)</div>',
                unsafe_allow_html=True,
            )
            df_conf["cosine_sim_score"] = df_conf.apply(
                lambda row: (
                    f"{row['cosine_sim_score'] * 100:.0f}%"
                    if row["match_strategy"] == "semantic" and pd.notna(row["cosine_sim_score"])
                    else "—"
                ),
                axis=1,
            )
            tbl = df_conf[["jira_key", "test_name", "run_id",
                            "date_delta_days", "match_strategy", "cosine_sim_score", "summary"]].copy()
            tbl.columns = ["JIRA Key", "Test", "Run",
                           "Date Δ", "Strategy", "Cosine Sim", "Summary"]
            st.table(tbl)

    # ── Pending Review ────────────────────────────────────────────────────────
    with tab_pending:
        df_pend = pd.read_sql_query(
            """SELECT dtl.link_id, dtl.jira_key, dtl.test_name, dtl.run_id,
                      dtl.date_delta_days, dtl.match_strategy, dtl.cosine_sim_score,
                      jd.summary, jd.status AS jira_status,
                      jd.priority, jd.reporter_email
               FROM defect_test_links dtl
               JOIN jira_defects jd ON dtl.jira_key = jd.jira_key
               JOIN runs r ON dtl.run_id = r.run_id
               WHERE dtl.confirmed = 0 AND (? = 'All' OR r.team = ?)
               ORDER BY dtl.cosine_sim_score DESC NULLS LAST, dtl.date_delta_days ASC""",
            conn, params=tf,
        )
        df_pend = df_pend[df_pend["match_strategy"].isin(strategy_filter)]
        if df_pend.empty:
            st.success("No pending links — all candidates have been reviewed.")
        else:
            df_pend["cosine_sim_score"] = df_pend.apply(
                lambda row: (
                    f"{row['cosine_sim_score'] * 100:.0f}%"
                    if row["match_strategy"] == "semantic" and pd.notna(row["cosine_sim_score"])
                    else "—"
                ),
                axis=1,
            )
            st.markdown(
                f'<div style="font:400 0.78rem/1.6 \'JetBrains Mono\',monospace; '
                f'color:{C["muted"]}; margin-bottom:.8rem;">'
                f"These links need human confirmation. "
                f"Select a row, then click Accept or Reject.</div>",
                unsafe_allow_html=True,
            )
            selected = st.dataframe(
                df_pend.drop(columns=["link_id"]),
                use_container_width=True,
                column_config={
                    "cosine_sim_score": st.column_config.TextColumn("Cosine Sim"),
                    "date_delta_days":  st.column_config.NumberColumn("Date Δ (days)"),
                    "match_strategy":   st.column_config.TextColumn("Strategy"),
                    "jira_key":         st.column_config.TextColumn("JIRA Key"),
                    "test_name":        st.column_config.TextColumn("Test"),
                    "run_id":           st.column_config.TextColumn("Run"),
                    "summary":          st.column_config.TextColumn("Summary", width="large"),
                },
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
            )
            rows = selected.get("selection", {}).get("rows", []) if isinstance(selected, dict) else []
            if rows:
                idx = rows[0]
                link_id   = int(df_pend.iloc[idx]["link_id"])
                jira_key  = df_pend.iloc[idx]["jira_key"]
                test_name = df_pend.iloc[idx]["test_name"]
                run_id    = df_pend.iloc[idx]["run_id"]
                st.markdown(f'**Selected:** `{jira_key}` → `{test_name}` in run `{run_id}`')
                col_accept, col_reject, _ = st.columns([1, 1, 6])
                reviewer = "dashboard-user"
                now = datetime.now(timezone.utc).isoformat()
                if col_accept.button("Accept", type="primary"):
                    conn.execute(
                        "UPDATE defect_test_links SET confirmed=1, confirmed_by=?, confirmed_at=? WHERE link_id=?",
                        (reviewer, now, link_id),
                    )
                    conn.commit()
                    st.rerun()
                if col_reject.button("Reject", type="secondary"):
                    conn.execute(
                        "UPDATE defect_test_links SET confirmed=-1, confirmed_by=?, confirmed_at=? WHERE link_id=?",
                        (reviewer, now, link_id),
                    )
                    conn.commit()
                    st.rerun()

    # ── All Defects ───────────────────────────────────────────────────────────
    with tab_all:
        df_all = pd.read_sql_query(
            """SELECT jd.jira_key, jd.summary, jd.reporter_email, jd.status,
                      jd.priority, jd.project, jd.labels, jd.created,
                      GROUP_CONCAT(DISTINCT CASE WHEN dtl.confirmed != -1 THEN dtl.test_name END) AS linked_test_names,
                      COUNT(DISTINCT CASE WHEN dtl.confirmed != -1 THEN dtl.run_id END)            AS linked_runs,
                      COUNT(DISTINCT CASE WHEN dtl.confirmed != -1 THEN dtl.test_name END)         AS linked_tests
               FROM jira_defects jd
               LEFT JOIN defect_test_links dtl ON jd.jira_key = dtl.jira_key
               GROUP BY jd.jira_key
               ORDER BY jd.created DESC""",
            conn,
        )
        if df_all.empty:
            st.info("No defects imported yet.")
        else:
            df_all["linked_test_names"] = df_all["linked_test_names"].fillna("—").str.replace(",", ", ", regex=False)
            tbl = df_all[["jira_key", "summary", "reporter_email", "status", "priority",
                           "project", "linked_test_names", "linked_runs", "linked_tests"]].copy()
            tbl.columns = ["JIRA Key", "Summary", "Reporter", "Status", "Priority",
                           "Project", "Tests Linked", "Linked Runs", "# Tests"]
            st.table(tbl)

    conn.close()


def render_footer(db_paths: list[str], df_runs: pd.DataFrame) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dbs = ", ".join(Path(p).name for p in db_paths) if db_paths else "demo"
    st.markdown(
        f'<div class="dash-footer">'
        f'  <span>Phase 3 · schema.sql v2 · Option B multi-DB</span>'
        f'  <span>{len(df_runs)} runs · refreshed {now} · {dbs}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _parse_db_paths() -> list[str]:
    paths = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--db" and i + 1 < len(args):
            paths.append(args[i + 1])
            i += 2
        elif args[i].startswith("--db="):
            paths.append(args[i].split("=", 1)[1])
            i += 1
        else:
            i += 1
    return paths if paths else [DEFAULT_DB]

def main() -> None:
    inject_css()
    db_paths = _parse_db_paths()
    db_key = "|".join(sorted(db_paths))
    with st.spinner("Opening database connections…"):
        result = get_db_data(db_key)
    if len(result) == 3:
        df_runs, df_results, _ = result
    else:
        df_runs, df_results = result
    if df_runs.empty:
        st.error("No run data found. Please run `python pipeline2.py` first.")
        st.stop()

    db_sources = sorted(df_runs["_source_db"].unique().tolist()) if "_source_db" in df_runs.columns else ["unknown"]
    selected_source, selected_week, selected_run_id, drift_test, show_trend, show_run_inspector, show_duration_drift, show_health_matrix, show_jira_linkage = render_sidebar(df_runs, db_paths, db_sources)

    df_runs_f = filter_by_source(df_runs, selected_source)
    df_results_f = filter_by_source(df_results, selected_source)

    total_runs_f = len(df_runs_f)
    max_weeks_f = max(1, total_runs_f // 7)
    if selected_week > max_weeks_f:
        selected_week = max_weeks_f

    render_header(df_runs_f)
    render_weekly_health(df_runs_f, df_results_f, selected_week)
    if show_trend:
        render_trend(df_runs_f)
    if show_run_inspector:
        render_run_inspector(df_results_f, selected_run_id)
    if show_duration_drift:
        render_duration_drift(df_results_f, drift_test, selected_source)
    if show_health_matrix:
        render_test_health_matrix(df_results_f, selected_source)
    if show_jira_linkage:
        render_jira_linkage(db_paths)
    render_footer(db_paths, df_runs_f)

if __name__ == "__main__":
    main()
