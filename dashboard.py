"""
Phase 3 — Automation Test Stability Analytics Dashboard                   
=======================================================
                                                                            
Answers five operational questions:
    Q1  Are we green today?          Latest pass rate + health status
    Q2  Trending up or down?         Pass-rate trend + anomaly flags
    Q3  Which tests broke today?     Failures table + category donut
    Q4  Which tests are flaky?       Flip-count chart + duration drift
    Q5  Better or worse this week?   Week-on-week delta metrics
"""

import sqlite3
import sys
import json
import random
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


# PAGE CONFIG

st.set_page_config(
    page_title="Test Stability Analytics",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)


# CONSTANTS

DEFAULT_DB       = "./analytics.db"
GREEN_THRESHOLD  = 80.0
AMBER_THRESHOLD  = 60.0
ROLLING_WINDOW   = 10
ANOMALY_SIGMA    = 2.0

# Tests with interesting duration patterns
DURATION_TESTS = [
    "TC_User_BulkImport",        # progressive drift
    "TC_Dashboard_ExportChart",  # step change at run 51
    "TC_Login_ValidCredentials", # seasonal (even/odd)
]


# COLOUR PALETTE

C = {
    "bg":      "#0D1117",
    "bg2":     "#161B22",
    "card":    "#1C2128",
    "border":  "#30363D",
    "txt":     "#E6EDF3",
    "muted":   "#8B949E",
    "green":   "#3FB950",
    "red":     "#F85149",
    "amber":   "#D29922",
    "blue":    "#58A6FF",
    "purple":  "#BC8CFF",
    "orange":  "#FFA657",
    "teal":    "#39D353",
    "pink":    "#FF7EB3",
}

FAILURE_COLOR = {
    "timeout":     C["amber"],
    "element":     C["blue"],
    "assertion":   C["purple"],
    "data":        C["orange"],
    "environment": C["teal"],
    "unknown":     C["muted"],
}


# CSS

def inject_css() -> None:
    st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,400;0,600;0,700;1,400&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');

    /* ── global ────────────────────────────────────────────────────────────── */
    html, body, [class*="css"] {{
        font-family: 'IBM Plex Sans', sans-serif !important;
        background-color: {C["bg"]} !important;
        color: {C["txt"]} !important;
    }}
    #MainMenu, footer, header {{ visibility: hidden; }}
    .block-container {{ padding: 1.8rem 2.5rem 4rem !important; max-width: 1440px; }}
    .element-container {{ margin-bottom: 0 !important; }}
    a {{ color: {C["blue"]} !important; text-decoration: none; }}

    /* ── scrollbar ──────────────────────────────────────────────────────────── */
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: {C["bg"]}; }}
    ::-webkit-scrollbar-thumb {{ background: {C["border"]}; border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: {C["muted"]}; }}

    /* ── sidebar ────────────────────────────────────────────────────────────── */
    section[data-testid="stSidebar"] > div:first-child {{
        background-color: {C["bg2"]} !important;
        border-right: 1px solid {C["border"]} !important;
    }}
    [data-testid="stSidebar"] * {{ color: {C["txt"]} !important; }}

    /* ── section header ─────────────────────────────────────────────────────── */
    .sec-wrap {{
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 2.4rem 0 1.1rem;
        padding-bottom: 0.55rem;
        border-bottom: 1px solid {C["border"]};
    }}
    .sec-tag {{
        background: {C["blue"]}1a;
        color: {C["blue"]};
        font: 700 0.63rem/1 'JetBrains Mono', monospace;
        letter-spacing: .12em;
        text-transform: uppercase;
        padding: 3px 9px;
        border-radius: 4px;
        border: 1px solid {C["blue"]}44;
        white-space: nowrap;
    }}
    .sec-title {{
        font-size: 1.05rem;
        font-weight: 600;
        margin: 0;
    }}
    .sec-sub {{
        font-size: 0.78rem;
        color: {C["muted"]};
        margin-left: auto;
        font-family: 'JetBrains Mono', monospace;
    }}

    /* ── dashboard header ───────────────────────────────────────────────────── */
    .dash-header {{
        background: linear-gradient(135deg, {C["bg2"]} 0%, {C["card"]} 100%);
        border: 1px solid {C["border"]};
        border-radius: 12px;
        padding: 1.4rem 1.8rem;
        margin-bottom: 2rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }}
    .dash-wordmark {{
        font: 700 1.55rem/1 'IBM Plex Sans', sans-serif;
        letter-spacing: -.03em;
    }}
    .dash-wordmark span {{ color: {C["blue"]}; }}
    .dash-subtitle {{
        font: 400 0.78rem/1.5 'JetBrains Mono', monospace;
        color: {C["muted"]};
        margin-top: .3rem;
    }}
    .dash-right {{
        text-align: right;
        font: 400 0.75rem/1.7 'JetBrains Mono', monospace;
        color: {C["muted"]};
    }}

    /* ── metric cards ───────────────────────────────────────────────────────── */
    .metric-card {{
        background: {C["card"]};
        border: 1px solid {C["border"]};
        border-radius: 10px;
        padding: 1.4rem 1.6rem;
        height: 100%;
        position: relative;
        overflow: hidden;
    }}
    .metric-card::before {{
        content: '';
        position: absolute;
        left: 0; top: 0; bottom: 0;
        width: 4px;
        border-radius: 10px 0 0 10px;
    }}
    .mc-green::before  {{ background: {C["green"]}; }}
    .mc-red::before    {{ background: {C["red"]}; }}
    .mc-amber::before  {{ background: {C["amber"]}; }}
    .mc-blue::before   {{ background: {C["blue"]}; }}
    .mc-purple::before {{ background: {C["purple"]}; }}

    .mc-label {{
        font: 500 0.7rem/1 'JetBrains Mono', monospace;
        letter-spacing: .1em;
        text-transform: uppercase;
        color: {C["muted"]};
        margin-bottom: .5rem;
    }}
    .mc-value {{
        font: 700 3rem/1 'JetBrains Mono', monospace;
        margin-bottom: .35rem;
    }}
    .mc-value.green  {{ color: {C["green"]}; }}
    .mc-value.red    {{ color: {C["red"]}; }}
    .mc-value.amber  {{ color: {C["amber"]}; }}
    .mc-value.blue   {{ color: {C["blue"]}; }}
    .mc-sub {{
        font: 400 0.78rem/1.4 'IBM Plex Sans', sans-serif;
        color: {C["muted"]};
    }}
    .mc-badge {{
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 10px 3px 7px;
        border-radius: 20px;
        font: 600 0.72rem/1 'JetBrains Mono', monospace;
        margin-top: .55rem;
    }}
    .badge-green  {{ background:{C["green"]}1a; color:{C["green"]}; border:1px solid {C["green"]}44; }}
    .badge-red    {{ background:{C["red"]}1a;   color:{C["red"]};   border:1px solid {C["red"]}44;   }}
    .badge-amber  {{ background:{C["amber"]}1a; color:{C["amber"]}; border:1px solid {C["amber"]}44; }}
    .badge-blue   {{ background:{C["blue"]}1a;  color:{C["blue"]};  border:1px solid {C["blue"]}44;  }}
    .badge-purple {{ background:{C["purple"]}1a;color:{C["purple"]};border:1px solid {C["purple"]}44;}}
    .badge-orange {{ background:{C["orange"]}1a;color:{C["orange"]};border:1px solid {C["orange"]}44;}}

    /* ── delta cards (Q5) ───────────────────────────────────────────────────── */
    .delta-card {{
        background: {C["card"]};
        border: 1px solid {C["border"]};
        border-radius: 10px;
        padding: 1.2rem 1.5rem;
        text-align: center;
    }}
    .delta-num {{
        font: 700 2.6rem/1.1 'JetBrains Mono', monospace;
    }}
    .delta-lbl {{
        font: 500 0.68rem/1 'JetBrains Mono', monospace;
        letter-spacing: .1em;
        text-transform: uppercase;
        color: {C["muted"]};
        margin-top: .45rem;
    }}

    /* ── failure table ──────────────────────────────────────────────────────── */
    .fail-table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 0.84rem;
        background: {C["card"]};
        border: 1px solid {C["border"]};
        border-radius: 10px;
        overflow: hidden;
    }}
    .fail-table th {{
        background: {C["bg2"]};
        color: {C["muted"]};
        font: 600 0.67rem/1 'JetBrains Mono', monospace;
        letter-spacing: .1em;
        text-transform: uppercase;
        padding: 10px 14px;
        text-align: left;
        border-bottom: 1px solid {C["border"]};
        white-space: nowrap;
    }}
    .fail-table td {{
        padding: 10px 14px;
        border-bottom: 1px solid {C["border"]}55;
        vertical-align: middle;
        line-height: 1.4;
    }}
    .fail-table tr:last-child td {{ border-bottom: none; }}
    .fail-table tr:hover td {{ background: {C["bg2"]}88; }}

    .tname {{ font: 600 0.8rem/1.3  'JetBrains Mono', monospace; color: {C["blue"]}; }}
    .tmsg  {{ font: 400 0.75rem/1.4 'JetBrains Mono', monospace; color: {C["muted"]};
              max-width: 460px; word-break: break-word; }}
    .tkw   {{ font: 400 0.72rem/1   'JetBrains Mono', monospace; color: {C["muted"]}; }}
    .tdur  {{ font: 600 0.78rem/1   'JetBrains Mono', monospace; color: {C["txt"]};
              white-space: nowrap; }}

    /* ── info / warn banners ────────────────────────────────────────────────── */
    .info-banner {{
        background: {C["blue"]}12;
        border: 1px solid {C["blue"]}33;
        border-radius: 8px;
        padding: .7rem 1rem;
        font-size: 0.82rem;
        color: {C["blue"]};
        margin-bottom: 1rem;
    }}
    .warn-banner {{
        background: {C["amber"]}12;
        border: 1px solid {C["amber"]}33;
        border-radius: 8px;
        padding: .7rem 1rem;
        font-size: 0.82rem;
        color: {C["amber"]};
        margin-bottom: 1rem;
    }}

    /* ── footer ─────────────────────────────────────────────────────────────── */
    .dash-footer {{
        margin-top: 3rem;
        padding: 1rem 0 .5rem;
        border-top: 1px solid {C["border"]};
        font: 400 0.72rem/1.6 'JetBrains Mono', monospace;
        color: {C["muted"]};
        display: flex;
        justify-content: space-between;
    }}
    </style>
    """, unsafe_allow_html=True)


# PLOTLY THEME FACTORY

def dark_layout(height: int = 360, title: str = "", margin: dict | None = None) -> dict:
    m = margin or dict(l=10, r=20, t=40 if title else 10, b=10)
    layout = dict(
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
    return layout


# DATABASE CONNECTION

@st.cache_resource
def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a cached connection to analytics.db (or fall back to demo DB)."""
    p = Path(db_path)
    if not p.exists():
        st.markdown(
            f'<div class="warn-banner">⚠️  <b>Database not found:</b> <code>{db_path}</code> — '
            f'running in <b>DEMO MODE</b> with synthetic data. '
            f'Run <code>python pipeline.py</code> to load your real data.</div>',
            unsafe_allow_html=True,
        )
        return _build_demo_db()

    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# DEMO DATABASE  (synthetic data matching the company schema exactly)

def _build_demo_db() -> sqlite3.Connection:
    """Build a complete in-memory SQLite DB matching the company schema."""
    rng = random.Random(42)

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Only the three tables defined in the company schema
    conn.executescript("""
        CREATE TABLE runs (
            run_id        TEXT PRIMARY KEY,
            team          TEXT NOT NULL,
            suite_name    TEXT NOT NULL,
            job_name      TEXT,
            build_no      INTEGER,
            timestamp     DATETIME NOT NULL,
            duration_s    REAL,
            total         INTEGER NOT NULL,
            passed        INTEGER NOT NULL,
            failed        INTEGER NOT NULL,
            pass_rate_pct REAL,
            environment   TEXT,
            executor      TEXT
        );
        CREATE TABLE test_results (
            result_id   TEXT PRIMARY KEY,
            run_id      TEXT NOT NULL REFERENCES runs(run_id),
            suite_name  TEXT NOT NULL,
            test_name   TEXT NOT NULL,
            status      TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
            duration_s  REAL,
            failure_msg TEXT,
            failure_kw  TEXT,
            tags        TEXT
        );
        CREATE TABLE ingestion_log (
            run_id      TEXT PRIMARY KEY,
            ingested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            status      TEXT NOT NULL,
            error_msg   TEXT
        );
    """)

    TESTS = [
        ("TC_Login_ValidCredentials",  "feature_login",     "priority_high",   "stable",               0.00),
        ("TC_Login_InvalidPassword",   "feature_login",     "priority_high",   "stable",               0.00),
        ("TC_Login_SessionTimeout",    "feature_login",     "priority_high",   "stable",               0.00),
        ("TC_Login_AccountLockout",    "feature_login",     "priority_medium", "stable",               0.00),
        ("TC_Dashboard_FilterByDate",  "feature_dashboard", "priority_medium", "stable",               0.00),
        ("TC_Dashboard_Pagination",    "feature_dashboard", "priority_medium", "stable",               0.00),
        ("TC_Dashboard_ExportChart",   "feature_dashboard", "priority_medium", "stable",               0.00),
        ("TC_Dashboard_SearchBar",     "feature_dashboard", "priority_medium", "stable",               0.00),
        ("TC_User_CreateAccount",      "feature_usermgmt",  "priority_high",   "stable",               0.00),
        ("TC_User_EditProfile",        "feature_usermgmt",  "priority_medium", "stable",               0.00),
        ("TC_User_DeleteAccount",      "feature_usermgmt",  "priority_high",   "stable",               0.00),
        ("TC_User_PasswordReset",      "feature_usermgmt",  "priority_medium", "stable",               0.00),
        ("TC_Login_MFAVerification",   "feature_login",     "priority_high",   "flaky-mild",           0.30),
        ("TC_Login_SSORedirect",       "feature_login",     "priority_high",   "flaky-mild",           0.35),
        ("TC_Dashboard_LoadWidget",    "feature_dashboard", "priority_medium", "flaky-moderate",       0.50),
        ("TC_Dashboard_RefreshData",   "feature_dashboard", "priority_medium", "flaky-moderate",       0.55),
        ("TC_User_BulkImport",         "feature_usermgmt",  "priority_medium", "flaky-heavy",          0.65),
        ("TC_User_RoleAssignment",     "feature_usermgmt",  "priority_high",   "consistently_failing", 0.80),
        ("TC_User_BatchExport",        "feature_usermgmt",  "priority_medium", "consistently_failing", 0.75),
        ("TC_Login_OAuthCallback",     "feature_login",     "priority_high",   "consistently_failing", 0.70),
    ]
    FAIL_CFG = {
        "TC_Login_MFAVerification": ("timeout",   "assertion", 0.70),
        "TC_Login_SSORedirect":     ("timeout",   "element",   0.70),
        "TC_Dashboard_LoadWidget":  ("element",   "timeout",   0.70),
        "TC_Dashboard_RefreshData": ("assertion", "data",      0.60),
        "TC_User_BulkImport":       ("data",      "assertion", 0.70),
        "TC_User_RoleAssignment":   ("assertion", "data",      0.65),
        "TC_User_BatchExport":      ("data",      "element",   0.65),
        "TC_Login_OAuthCallback":   ("timeout",   "element",   0.70),
    }

    def _fail_msg(cat):
        if cat == "timeout":
            e = rng.choice(["loading-spinner", "overlay-modal", "auth-redirect", "session-token"])
            t = rng.choice([15, 20, 30, 45])
            return f"Element '{e}' still visible after {t}s timeout", "Wait Until Element Is Visible"
        elif cat == "element":
            l = rng.choice(["id=widget-container", "id=submit-btn", "css=.data-grid", "id=modal-confirm"])
            r = rng.choice([3, 5, 7])
            return f"Element with locator '{l}' not found after {r} retries", "Click Element"
        elif cat == "assertion":
            exp, got, desc = rng.choice([("200","500","Internal Server Error"),("200","404","Not Found"),("201","400","Bad Request")])
            return f"Expected HTTP status '{exp}' but got '{got}' — {desc}", "Should Be Equal As Numbers"
        else:
            rows = rng.choice([0, 1, 2])
            mins = rng.choice([50, 100, 200])
            rng2 = rng.choice(["Oct 2024", "last 30 days", "Q4 2024"])
            return f"CSV export contained {rows} rows — expected at least {mins} records for {rng2}", "Verify Row Count"

    def _dur(name, n, status):
        if name == "TC_User_BulkImport":
            base = rng.uniform(10, 14) if n <= 40 else rng.uniform(18, 24) if n <= 65 else rng.uniform(28, 36)
        elif name == "TC_Dashboard_ExportChart":
            base = rng.uniform(3, 5) if n <= 50 else rng.uniform(12, 15)
        elif name == "TC_Login_ValidCredentials":
            base = rng.uniform(2.0, 3.5) if n % 2 == 0 else rng.uniform(4.5, 6.5)
        else:
            base = rng.uniform(1.2, 8.5)
        if status == "FAIL":
            base += rng.uniform(5, 15)
        return round(base, 3)

    ANOMALY_RUNS      = {36, 37}
    ANOMALY_FAIL_RATE = 0.80

    start_dt = datetime(2024, 10, 1)
    for n in range(1, 101):
        ts      = (start_dt + timedelta(hours=24 * (n - 1))).isoformat()
        anomaly = n in ANOMALY_RUNS
        run_id  = f"TeamAlpha_build_{n:03d}"

        results = []
        for name, feat, pri, cat, fp in TESTS:
            if anomaly:
                eff = max(fp, ANOMALY_FAIL_RATE)
            elif fp == 0.0:
                eff = 0.0
            else:
                if n <= 25:    env = 0.65
                elif n <= 35:  env = 0.60
                elif n <= 45:  env = 0.55
                elif n <= 75:  env = 0.35
                else:          env = 0.15
                eff = min(0.95, fp * (1.0 + env))
            status = "FAIL" if rng.random() < eff else "PASS"
            results.append((name, feat, pri, cat, fp, status))

        passed = sum(1 for *_, s in results if s == "PASS")
        failed = 20 - passed
        pr     = round(passed * 100.0 / 20, 1)

        conn.execute(
            """INSERT INTO runs
               (run_id, team, suite_name, job_name, build_no, timestamp,
                duration_s, total, passed, failed, pass_rate_pct, environment, executor)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, "TeamAlpha", "Suite_Regression", "regression-job", n, ts,
             None, 20, passed, failed, pr, "staging", f"jenkins-agent-{(n % 3) + 1:02d}"),
        )

        for name, feat, pri, cat, fp, status in results:
            dur = _dur(name, n, status)
            tags = json.dumps([feat, pri, f"alpha_regression"])

            failure_msg, failure_kw = None, None
            if status == "FAIL" and name in FAIL_CFG:
                prim, sec, pp = FAIL_CFG[name]
                fcat = prim if rng.random() < pp else sec
                failure_msg, failure_kw = _fail_msg(fcat)

            result_id = f"{run_id}_{name}"
            conn.execute(
                """INSERT INTO test_results
                   (result_id, run_id, suite_name, test_name, status,
                    duration_s, failure_msg, failure_kw, tags)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (result_id, run_id, "Suite_Regression", name, status,
                 dur, failure_msg, failure_kw, tags),
            )

    conn.commit()
    return conn


# DATA FETCHERS
# All queries use only: runs, test_results, ingestion_log
# Column names match the company schema exactly.

def fetch_all_runs(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch all runs ordered chronologically. Aliased for dashboard compatibility."""
    return pd.read_sql_query(
        """
        SELECT
            run_id,
            timestamp,
            passed,
            failed,
            total                AS total_tests,
            pass_rate_pct        AS pass_rate,
            environment,
            executor
        FROM runs
        ORDER BY build_no, run_id
        """,
        conn,
    )


def fetch_run_options(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return [(run_id, label), ...] for the run selector drop-down."""
    df = pd.read_sql_query(
        "SELECT run_id, timestamp, pass_rate_pct FROM runs ORDER BY build_no DESC, run_id DESC",
        conn,
    )
    return [
        (str(row.run_id),
         f"{str(row.run_id)}  —  {str(row.timestamp)[:10]}  —  {row.pass_rate_pct:.1f}%")
        for _, row in df.iterrows()
    ]


def fetch_run_by_id(conn: sqlite3.Connection, run_id: str) -> dict:
    df = pd.read_sql_query(
        """
        SELECT
            run_id,
            timestamp,
            passed,
            failed,
            total         AS total_tests,
            pass_rate_pct AS pass_rate,
            environment,
            executor
        FROM runs
        WHERE run_id = ?
        """,
        conn, params=(run_id,),
    )
    return df.iloc[0].to_dict() if len(df) else {}


def fetch_latest_run(conn: sqlite3.Connection) -> dict:
    df = pd.read_sql_query(
        """
        SELECT
            run_id,
            timestamp,
            passed,
            failed,
            total         AS total_tests,
            pass_rate_pct AS pass_rate,
            environment,
            executor
        FROM runs
        ORDER BY build_no DESC, run_id DESC
        LIMIT 1
        """,
        conn,
    )
    return df.iloc[0].to_dict() if len(df) else {}


def fetch_failures_for_run(conn: sqlite3.Connection, run_id: str) -> pd.DataFrame:
    """
    Return all failed tests for a given run_id.

    In the company schema failure info (message, keyword) lives directly in
    test_results — there is no separate failures table or test_results join.
    We derive a failure_category from the message text in Python after fetching.
    """
    df = pd.read_sql_query(
        """
        SELECT
            tr.test_name,
            tr.duration_s                                        AS duration,
            COALESCE(tr.failure_msg, '(no message)')             AS failure_message,
            COALESCE(tr.failure_kw,  '—')                        AS keyword_name,
            tr.tags
        FROM test_results tr
        WHERE tr.run_id = ?
          AND tr.status = 'FAIL'
        ORDER BY tr.test_name
        """,
        conn, params=(run_id,),
    )
    if df.empty:
        return df

    # Derive failure_category from the message — mirrors the pipeline classification
    df["failure_category"] = df["failure_message"].apply(_classify_failure_message)

    # Extract a feature tag (first tag starting with "feature_") from the JSON tags column
    def _feature(tags_json):
        try:
            tags = json.loads(tags_json or "[]")
            for t in tags:
                if t.startswith("feature_"):
                    return t.replace("feature_", "")
        except Exception:
            pass
        return ""

    df["feature"] = df["tags"].apply(_feature)
    return df


def _classify_failure_message(message: str) -> str:
    """Derive a failure category from a failure message string."""
    if not message or message == "(no message)":
        return "unknown"
    lower = message.lower()
    if "still visible after" in lower and "timeout" in lower:
        return "timeout"
    if "not found after" in lower and "retries" in lower:
        return "element"
    if "expected http status" in lower:
        return "assertion"
    if "csv export contained" in lower and "rows" in lower:
        return "data"
    if "environment" in lower or "unreachable" in lower:
        return "environment"
    return "data"


def fetch_flaky_scores(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Per-test flip count, failure rate.

    In the company schema test_results has one row per (run, test).
    We join runs only for ordering by build_no to get chronological order.
    """
    return pd.read_sql_query(
        """
        WITH ordered AS (
            SELECT
                tr.test_name,
                tr.run_id,
                tr.status,
                LAG(tr.status) OVER (
                    PARTITION BY tr.test_name
                    ORDER BY r.build_no, tr.run_id
                ) AS prev_status
            FROM test_results tr
            JOIN runs r ON tr.run_id = r.run_id
        )
        SELECT
            test_name,
            COUNT(CASE WHEN status <> prev_status AND prev_status IS NOT NULL THEN 1 END) AS flip_count,
            COUNT(CASE WHEN status = 'FAIL' THEN 1 END)  AS fail_count,
            COUNT(*)                                      AS total_runs,
            ROUND(COUNT(CASE WHEN status = 'FAIL' THEN 1 END) * 100.0 / COUNT(*), 1) AS failure_rate
        FROM ordered
        GROUP BY test_name
        ORDER BY flip_count DESC, failure_rate DESC
        """,
        conn,
    )


def fetch_duration_series(conn: sqlite3.Connection, test_name: str) -> pd.DataFrame:
    """
    Return per-run duration and status for one test, sorted chronologically.
    Used by the Q4 duration drift chart.
    """
    return pd.read_sql_query(
        """
        SELECT
            r.build_no  AS run_id,
            tr.duration_s AS duration,
            tr.status
        FROM test_results tr
        JOIN runs r ON tr.run_id = r.run_id
        WHERE tr.test_name = ?
        ORDER BY r.build_no, tr.run_id
        """,
        conn, params=(test_name,),
    )


def fetch_week_on_week(conn: sqlite3.Connection) -> dict:
    """Compare latest 7 runs vs the 7 before that."""
    df = pd.read_sql_query(
        """
        SELECT run_id, pass_rate_pct AS pass_rate, passed, failed
        FROM runs
        ORDER BY build_no DESC, run_id DESC
        LIMIT 14
        """,
        conn,
    )
    if len(df) < 2:
        return dict(pass_rate_delta=0.0, this_avg=0.0, last_avg=0.0, new_failures=0, tests_fixed=0)

    half     = min(7, len(df) // 2)
    this_w   = df.iloc[:half]
    last_w   = df.iloc[half: half * 2]
    this_avg = this_w["pass_rate"].mean()
    last_avg = last_w["pass_rate"].mean()

    latest_run = str(df["run_id"].iloc[0])
    prev_run   = str(df["run_id"].iloc[half])

    def _failed_names(rid):
        r = pd.read_sql_query(
            "SELECT DISTINCT test_name FROM test_results WHERE run_id=? AND status='FAIL'",
            conn, params=(rid,),
        )
        return set(r["test_name"].tolist())

    this_set = _failed_names(latest_run)
    last_set = _failed_names(prev_run)

    return dict(
        pass_rate_delta=this_avg - last_avg,
        this_avg=this_avg,
        last_avg=last_avg,
        new_failures=len(this_set - last_set),
        tests_fixed=len(last_set - this_set),
    )


def compute_anomalies(df_runs: pd.DataFrame) -> pd.DataFrame:
    """Add rolling mean, std, z-score, and anomaly flag columns."""
    df = df_runs.copy().reset_index(drop=True)
    roll            = df["pass_rate"].rolling(window=ROLLING_WINDOW, min_periods=3)
    df["roll_mean"] = roll.mean().shift(1)
    df["roll_std"]  = roll.std().shift(1).fillna(5.0)
    df["z_score"]   = (df["roll_mean"] - df["pass_rate"]) / df["roll_std"].clip(lower=1.0)
    df["anomaly"]   = df["z_score"] >= ANOMALY_SIGMA
    return df


# HTML HELPERS

def _badge(text: str, kind: str) -> str:
    return f'<span class="mc-badge badge-{kind}">{text}</span>'


def _failure_badge(fcat: str) -> str:
    color_map = {
        "timeout": "amber", "element": "blue", "assertion": "purple",
        "data": "orange", "environment": "blue", "unknown": "blue",
    }
    return _badge(fcat.upper(), color_map.get(fcat, "blue"))


def _section(tag: str, title: str, sub: str = "") -> None:
    sub_html = f'<span class="sec-sub">{sub}</span>' if sub else ""
    st.markdown(
        f'<div class="sec-wrap">'
        f'<span class="sec-tag">{tag}</span>'
        f'<span class="sec-title">{title}</span>'
        f'{sub_html}</div>',
        unsafe_allow_html=True,
    )


# CHART BUILDERS

def chart_trend(df_all: pd.DataFrame, show_n: int) -> go.Figure:
    """Q2 — Pass-rate trend with area fill, rolling mean, and anomaly markers."""
    df         = compute_anomalies(df_all)
    display_df = df.tail(show_n).copy()

    fig = go.Figure(layout=dark_layout(height=380, margin=dict(l=10, r=20, t=10, b=30)))

    for lo, hi, col in [
        (0,               AMBER_THRESHOLD, "rgba(248,81,73,0.09)"),
        (AMBER_THRESHOLD, GREEN_THRESHOLD, "rgba(210,153,34,0.09)"),
        (GREEN_THRESHOLD, 100,             "rgba(63,185,80,0.06)"),
    ]:
        fig.add_hrect(y0=lo, y1=hi, fillcolor=col, line_width=0, layer="below")

    for y, col, label in [
        (GREEN_THRESHOLD, "rgba(63,185,80,0.53)",  "80% target"),
        (AMBER_THRESHOLD, "rgba(210,153,34,0.4)",  "60% warning"),
    ]:
        fig.add_hline(
            y=y, line=dict(color=col, width=1, dash="dot"),
            annotation_text=label,
            annotation=dict(font=dict(color=col, size=9.5), xanchor="right", x=1),
        )

    fig.add_trace(go.Scatter(
        x=display_df.index, y=display_df["roll_mean"],
        mode="lines",
        line=dict(color="rgba(139,148,158,0.53)", width=1.5, dash="dot"),
        name=f"Rolling mean ({ROLLING_WINDOW} runs)",
        hovertemplate="Rolling mean: %{y:.1f}%<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=display_df.index, y=display_df["pass_rate"],
        mode="lines+markers",
        line=dict(color=C["blue"], width=2.5),
        fill="tozeroy",
        fillcolor="rgba(88,166,255,0.09)",
        marker=dict(size=4, color=C["blue"]),
        name="Pass rate %",
        customdata=display_df["run_id"],
        hovertemplate="<b>%{customdata}</b><br>Pass rate: <b>%{y:.1f}%</b><br><extra></extra>",
    ))

    anom = display_df[display_df["anomaly"]]
    if len(anom):
        fig.add_trace(go.Scatter(
            x=anom.index, y=anom["pass_rate"],
            mode="markers",
            marker=dict(size=11, color=C["red"], symbol="circle", line=dict(color="#fff", width=1.5)),
            name="⚠ Anomaly",
            customdata=list(zip(anom["run_id"], anom["z_score"])),
            hovertemplate=(
                "<b>⚠ ANOMALY — %{customdata[0]}</b><br>"
                "Pass rate: <b>%{y:.1f}%</b><br>"
                "Z-score: %{customdata[1]:.2f}σ below baseline<br><extra></extra>"
            ),
        ))

    fig.update_layout(
        xaxis=dict(title=dict(text="Run", font=dict(size=10, color=C["muted"])), dtick=5),
        yaxis=dict(title=dict(text="Pass Rate (%)", font=dict(size=10, color=C["muted"])), range=[0, 102]),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        hovermode="x unified",
    )
    return fig


def chart_flaky(df: pd.DataFrame) -> go.Figure:
    """Q4 — Horizontal bar chart: tests ranked by PASS↔FAIL flip count."""
    df = df[df["flip_count"] > 0].copy()
    if df.empty:
        return go.Figure(layout=dark_layout(height=320))

    df = df.sort_values("flip_count", ascending=True)
    df["short_name"] = df["test_name"].str.replace("TC_", "", regex=False)

    fig = go.Figure(layout=dark_layout(
        height=max(320, len(df) * 42 + 60),
        margin=dict(l=10, r=120, t=10, b=10),
    ))

    fig.add_trace(go.Bar(
        y=df["short_name"],
        x=df["flip_count"],
        orientation="h",
        marker=dict(color=C["blue"], opacity=0.88, line=dict(color=C["border"], width=0.5)),
        text=df["flip_count"],
        textposition="outside",
        textfont=dict(family="JetBrains Mono", size=11, color=C["txt"]),
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            "Flips: <b>%{x}</b><br>"
            "Failure rate: <b>%{customdata[1]:.1f}%</b><br><extra></extra>"
        ),
        customdata=list(zip(df["test_name"], df["failure_rate"])),
        name="",
    ))

    fig.update_layout(
        xaxis=dict(title=dict(text="PASS ↔ FAIL Flips", font=dict(size=10, color=C["muted"])), showgrid=True),
        yaxis=dict(tickfont=dict(family="JetBrains Mono", size=10.5)),
        showlegend=False,
        bargap=0.28,
    )
    return fig


def chart_failure_dist(df_failures: pd.DataFrame) -> go.Figure | None:
    """Mini donut chart — failure category breakdown for one run."""
    if df_failures.empty:
        return None

    counts = df_failures["failure_category"].value_counts().reset_index()
    counts.columns = ["category", "count"]

    fig = go.Figure(layout=dark_layout(height=260, margin=dict(l=0, r=0, t=10, b=0)))
    fig.add_trace(go.Pie(
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
    ))
    return fig


def chart_duration_drift(df_dur: pd.DataFrame, test_name: str) -> go.Figure:
    """Q4 — Duration over time for one test, coloured by PASS/FAIL status."""
    if df_dur.empty:
        return go.Figure(layout=dark_layout(height=280))

    pass_df = df_dur[df_dur["status"] == "PASS"]
    fail_df = df_dur[df_dur["status"] == "FAIL"]

    fig = go.Figure(layout=dark_layout(height=280, margin=dict(l=10, r=20, t=10, b=30)))

    df_dur_sorted = df_dur.sort_values("run_id").copy()
    df_dur_sorted["roll_mean"] = df_dur_sorted["duration"].rolling(window=5, min_periods=2).mean()
    fig.add_trace(go.Scatter(
        x=df_dur_sorted["run_id"],
        y=df_dur_sorted["roll_mean"],
        mode="lines",
        line=dict(color="rgba(139,148,158,0.45)", width=1.5, dash="dot"),
        name="5-run rolling mean",
        hovertemplate="Rolling mean: %{y:.2f}s<extra></extra>",
    ))

    if not pass_df.empty:
        fig.add_trace(go.Scatter(
            x=pass_df["run_id"], y=pass_df["duration"],
            mode="markers",
            marker=dict(size=5, color=C["green"], opacity=0.75),
            name="PASS",
            hovertemplate="Run %{x} · PASS · %{y:.2f}s<extra></extra>",
        ))

    if not fail_df.empty:
        fig.add_trace(go.Scatter(
            x=fail_df["run_id"], y=fail_df["duration"],
            mode="markers",
            marker=dict(size=6, color=C["red"], opacity=0.85, symbol="x"),
            name="FAIL",
            hovertemplate="Run %{x} · FAIL · %{y:.2f}s<extra></extra>",
        ))

    fig.update_layout(
        xaxis=dict(title=dict(text="Run #", font=dict(size=10, color=C["muted"])), dtick=10),
        yaxis=dict(title=dict(text="Duration (s)", font=dict(size=10, color=C["muted"]))),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        hovermode="x unified",
    )
    return fig


# SIDEBAR

def render_sidebar(df_all: pd.DataFrame, conn: sqlite3.Connection, db_path: str):
    with st.sidebar:
        st.markdown(
            f'<div style="font:700 1.1rem/1 \'IBM Plex Sans\',sans-serif; '
            f'margin-bottom:.3rem;">⚡ Test Stability</div>'
            f'<div style="font:400 0.72rem/1 \'JetBrains Mono\',monospace; '
            f'color:{C["muted"]}; margin-bottom:1.2rem;">CI Analytics</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
            f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
            f'margin-bottom:.5rem;">Q2 — Trend Chart</div>',
            unsafe_allow_html=True,
        )
        trend_window = st.slider(
            "Runs to show", 10, len(df_all), min(100, len(df_all)),
            help="Number of most-recent runs shown in the Q2 trend chart",
        )

        st.markdown("---")
        st.markdown(
            f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
            f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
            f'margin-bottom:.5rem;">Q3 — Run Inspector</div>',
            unsafe_allow_html=True,
        )
        run_options = fetch_run_options(conn)
        run_labels  = [label for _, label in run_options]
        run_ids     = [rid   for rid, _  in run_options]

        selected_idx = st.selectbox(
            "Inspect run",
            options=range(len(run_labels)),
            format_func=lambda i: run_labels[i],
            index=0,
            help="Select any past run to inspect its failures in Q3",
        )
        selected_run_id = run_ids[selected_idx]

        st.markdown("---")
        st.markdown(
            f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
            f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
            f'margin-bottom:.5rem;">Q4 — Flaky Tests</div>',
            unsafe_allow_html=True,
        )
        show_stable_flaky = st.checkbox(
            "Show stable tests (0 flips)", value=False,
            help="Include 0-flip tests in the flaky leaderboard",
        )
        drift_test = st.selectbox(
            "Duration drift — test",
            options=DURATION_TESTS,
            index=0,
            help="Choose a test to show its duration pattern over 100 runs",
        )

        st.markdown("---")
        st.markdown(
            f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
            f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
            f'margin-bottom:.6rem;">Database</div>',
            unsafe_allow_html=True,
        )
        st.code(db_path, language=None)
        if len(df_all):
            st.markdown(
                f'<div style="font:400 0.72rem/1.7 \'JetBrains Mono\',monospace; color:{C["muted"]};">'
                f'Runs loaded: <b style="color:{C["txt"]}">{len(df_all)}</b><br>'
                f'Date range: <b style="color:{C["txt"]}">'
                f'{df_all["timestamp"].iloc[0][:10]} → '
                f'{df_all["timestamp"].iloc[-1][:10]}</b></div>',
                unsafe_allow_html=True,
            )

        st.markdown("---")
        if st.button("🔄 Refresh data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.markdown(
            f'<div style="font:400 0.68rem/1.7 \'JetBrains Mono\',monospace; '
            f'color:{C["muted"]}; margin-top:1rem;">'
            f'Phase 3 · CPP Test Stability Analytics<br>'
            f'Robot Framework · SQLite · Streamlit</div>',
            unsafe_allow_html=True,
        )

    return trend_window, show_stable_flaky, selected_run_id, drift_test


# SECTION RENDERERS

def render_header(latest: dict) -> None:
    ts_str = latest.get("timestamp", "")[:16].replace("T", " ") if latest else "—"
    build  = latest.get("run_id", "—")
    env    = latest.get("environment", "—")
    exec_  = latest.get("executor", "—")
    st.markdown(
        f'<div class="dash-header">'
        f'  <div>'
        f'    <div class="dash-wordmark">Test Stability <span>Analytics</span></div>'
        f'    <div class="dash-subtitle">Suite_Regression · Robot Framework</div>'
        f'  </div>'
        f'  <div class="dash-right">'
        f'    Latest build: <b style="color:{C["txt"]}">{build}</b><br>'
        f'    Environment: {env} · Executor: {exec_}<br>'
        f'    Timestamp: {ts_str}'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_q1(latest: dict, df_all: pd.DataFrame) -> None:
    """Q1 — Are we green today?"""
    _section("Q1", "Are We Green Today?", "Latest build health at a glance")

    pr     = latest.get("pass_rate", 0)
    passed = int(latest.get("passed", 0))
    failed = int(latest.get("failed", 0))
    total  = int(latest.get("total_tests", 20))
    run_id = latest.get("run_id", "—")

    if pr >= GREEN_THRESHOLD:
        val_cls, card_cls, badge_kind, status_text = "green", "mc-green", "green", "✅  HEALTHY"
    elif pr >= AMBER_THRESHOLD:
        val_cls, card_cls, badge_kind, status_text = "amber", "mc-amber", "amber", "⚠️  WARNING"
    else:
        val_cls, card_cls, badge_kind, status_text = "red",   "mc-red",   "red",   "🔴  AT RISK"

    recent_avg   = df_all.tail(7)["pass_rate"].mean()
    best_recent  = df_all.tail(10)["pass_rate"].max()
    worst_recent = df_all.tail(10)["pass_rate"].min()

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
        avg_cls  = "green" if recent_avg >= GREEN_THRESHOLD else "amber" if recent_avg >= AMBER_THRESHOLD else "red"
        avg_card = f"mc-{avg_cls}"
        st.markdown(
            f'<div class="metric-card {avg_card}">'
            f'  <div class="mc-label">7-Run Average</div>'
            f'  <div class="mc-value {avg_cls}">{recent_avg:.1f}%</div>'
            f'  <div class="mc-sub">Rolling average across<br>the last 7 builds</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div class="metric-card mc-blue">'
            f'  <div class="mc-label">Best (Last 10)</div>'
            f'  <div class="mc-value blue">{best_recent:.1f}%</div>'
            f'  <div class="mc-sub">Peak pass rate in<br>the last 10 builds</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c4:
        worst_cls = "red" if worst_recent < AMBER_THRESHOLD else "amber"
        st.markdown(
            f'<div class="metric-card mc-{worst_cls}">'
            f'  <div class="mc-label">Worst (Last 10)</div>'
            f'  <div class="mc-value {worst_cls}">{worst_recent:.1f}%</div>'
            f'  <div class="mc-sub">Lowest pass rate in<br>the last 10 builds</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)


def render_q2(df_all: pd.DataFrame, trend_window: int) -> None:
    """Q2 — Trending up or down?"""
    df_anom  = compute_anomalies(df_all)
    n_anom   = int(df_anom["anomaly"].sum())
    anom_lbl = f"{n_anom} anomal{'y' if n_anom == 1 else 'ies'} detected" if n_anom else "no anomalies"

    _section("Q2", "Trending Up or Down?",
             f"Pass rate over last {min(trend_window, len(df_all))} runs · {anom_lbl}")

    fig = chart_trend(df_all, trend_window)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    last_5 = df_all.tail(5)["pass_rate"].mean()
    prev_5 = df_all.iloc[-10:-5]["pass_rate"].mean() if len(df_all) >= 10 else last_5
    delta5 = last_5 - prev_5

    if abs(delta5) < 2:
        insight = f"📊  Pass rate is <b>stable</b> — last 5 runs average {last_5:.1f}%, unchanged from the 5 runs before."
        color = C["blue"]
    elif delta5 > 0:
        insight = f"📈  Pass rate is <b>improving</b> — last 5 runs average {last_5:.1f}%, up {delta5:+.1f}pp vs the previous 5."
        color = C["green"]
    else:
        insight = f"📉  Pass rate is <b>declining</b> — last 5 runs average {last_5:.1f}%, down {delta5:+.1f}pp vs the previous 5."
        color = C["red"]

    st.markdown(
        f'<div style="background:{color}12; border:1px solid {color}33; '
        f'border-radius:8px; padding:.65rem 1rem; font-size:.82rem; '
        f'color:{color}; margin-top:.3rem;">{insight}</div>',
        unsafe_allow_html=True,
    )


def render_q3(conn: sqlite3.Connection, selected_run_id: str) -> None:
    """Q3 — Which tests broke today?"""
    df     = fetch_failures_for_run(conn, selected_run_id)
    run_md = fetch_run_by_id(conn, selected_run_id)
    n      = len(df)
    pr     = run_md.get("pass_rate", "—")

    _section("Q3", "Which Tests Broke?",
             f"{selected_run_id}  ·  {n} failure{'s' if n != 1 else ''}  ·  pass rate {pr}%")

    if df.empty:
        st.markdown(
            f'<div class="info-banner">✅  No failures in {selected_run_id} — all tests passed!</div>',
            unsafe_allow_html=True,
        )
        return

    col_table, col_donut = st.columns([2.6, 1])

    with col_table:
        rows = ""
        for _, row in df.iterrows():
            feat = row.get("feature", "")
            rows += f"""
            <tr>
                <td><span class="tname">{row['test_name']}</span><br>
                    <span class="tkw">⌗ {feat}</span></td>
                <td>{_failure_badge(row.get('failure_category', 'unknown'))}</td>
                <td><span class="tmsg">{row.get('failure_message', '')}</span><br>
                    <span class="tkw">via {row.get('keyword_name', '—')}</span></td>
                <td><span class="tdur">{row['duration']:.2f}s</span></td>
            </tr>"""

        st.markdown(f"""
        <table class="fail-table">
          <thead>
            <tr>
              <th>Test Name</th>
              <th>Failure Type</th><th>Failure Message</th><th>Duration</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>""", unsafe_allow_html=True)

    with col_donut:
        st.markdown(
            f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
            f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
            f'padding-top:.3rem; margin-bottom:.4rem;">Failure Breakdown</div>',
            unsafe_allow_html=True,
        )
        fig_donut = chart_failure_dist(df)
        if fig_donut is not None:
            st.plotly_chart(fig_donut, use_container_width=True,
                            config={"displayModeBar": False})

        cat_counts = df["failure_category"].value_counts()
        for cat, cnt in cat_counts.items():
            col = FAILURE_COLOR.get(str(cat), C["muted"])
            st.markdown(
                f'<div style="display:flex; align-items:center; gap:8px; '
                f'margin-bottom:.35rem; font-size:.78rem;">'
                f'<span style="width:8px; height:8px; border-radius:50%; '
                f'background:{col}; flex-shrink:0;"></span>'
                f'<span style="color:{C["txt"]}">{str(cat).upper()}</span>'
                f'<span style="margin-left:auto; font-family:\'JetBrains Mono\',monospace; '
                f'color:{C["muted"]};">{cnt}</span></div>',
                unsafe_allow_html=True,
            )


def render_q4(conn: sqlite3.Connection, show_stable: bool, drift_test: str) -> None:
    """Q4 — Which tests are flaky?"""
    df       = fetch_flaky_scores(conn)
    df_chart = df[df["flip_count"] > 0].copy() if not show_stable else df.copy()

    _section("Q4", "Which Tests Are Flaky?",
             f"{len(df_chart)} tests with ≥1 PASS↔FAIL flip")

    if df_chart.empty:
        st.markdown(
            '<div class="info-banner">ℹ️  No PASS↔FAIL flips found — all tests are stable.</div>',
            unsafe_allow_html=True,
        )
    else:
        col_chart, col_legend = st.columns([3, 1])

        with col_chart:
            fig = chart_flaky(df_chart)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        with col_legend:
            top3 = df_chart.head(3)
            if len(top3):
                st.markdown(
                    f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
                    f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
                    f'margin-bottom:.6rem;">Fix First</div>',
                    unsafe_allow_html=True,
                )
                for rank, (_, row) in enumerate(top3.iterrows(), 1):
                    st.markdown(
                        f'<div style="background:{C["card"]}; border:1px solid {C["border"]}; '
                        f'border-left:3px solid {C["blue"]}; border-radius:6px; '
                        f'padding:.55rem .75rem; margin-bottom:.5rem;">'
                        f'<div style="font:600 .78rem/1 \'JetBrains Mono\',monospace; '
                        f'color:{C["txt"]}; margin-bottom:.25rem;">'
                        f'#{rank} {row["test_name"].replace("TC_", "")}</div>'
                        f'<div style="font:.72rem/1 \'JetBrains Mono\',monospace; color:{C["muted"]};">'
                        f'{int(row["flip_count"])} flips · {row["failure_rate"]:.0f}% fail rate</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

    st.markdown(
        f'<div style="font:600 0.7rem/1 \'JetBrains Mono\',monospace; '
        f'text-transform:uppercase; letter-spacing:.1em; color:{C["muted"]}; '
        f'margin: 1.6rem 0 .5rem; padding-bottom:.4rem; '
        f'border-bottom:1px solid {C["border"]}55;">'
        f'Duration Drift — {drift_test}</div>',
        unsafe_allow_html=True,
    )

    df_dur    = fetch_duration_series(conn, drift_test)
    fig_drift = chart_duration_drift(df_dur, drift_test)
    st.plotly_chart(fig_drift, use_container_width=True, config={"displayModeBar": False})

    drift_insights = {
        "TC_User_BulkImport":
            "📈  <b>Progressive drift</b> — duration increases across three phases "
            "(runs 1–40, 41–65, 66–100). This is the leading indicator that Phase 4 ML4 "
            "(rolling Z-score) will detect automatically.",
        "TC_Dashboard_ExportChart":
            "⚡  <b>Step change</b> — duration roughly triples after run 50. "
            "This simulates a dependency upgrade or infrastructure change. "
            "A rolling Z-score flags this within 2–3 runs of the step.",
        "TC_Login_ValidCredentials":
            "🔄  <b>Seasonal alternation</b> — odd runs are ~2× slower than even runs. "
            "This pattern is NOT detected by a simple rolling Z-score (it averages out). "
            "Phase 4 insight: discovering why is the ML learning moment.",
    }
    if drift_test in drift_insights:
        st.markdown(
            f'<div style="background:{C["blue"]}12; border:1px solid {C["blue"]}33; '
            f'border-radius:8px; padding:.65rem 1rem; font-size:.82rem; '
            f'color:{C["blue"]}; margin-top:.4rem;">'
            f'{drift_insights[drift_test]}</div>',
            unsafe_allow_html=True,
        )


def render_q5(conn: sqlite3.Connection) -> None:
    """Q5 — Better or worse this week?"""
    wow = fetch_week_on_week(conn)

    delta_pr    = wow["pass_rate_delta"]
    new_fails   = wow["new_failures"]
    tests_fixed = wow["tests_fixed"]

    _section("Q5", "Better or Worse This Week?",
             f"Last 7 runs vs previous 7 · baseline {wow.get('last_avg', 0):.1f}%")

    c1, c2, c3 = st.columns(3)

    with c1:
        if delta_pr >= 2:    pr_color, pr_arrow = C["green"], "▲"
        elif delta_pr <= -2: pr_color, pr_arrow = C["red"],   "▼"
        else:                pr_color, pr_arrow = C["muted"], "—"
        sign = "+" if delta_pr > 0 else ""
        st.markdown(
            f'<div class="delta-card">'
            f'  <div class="delta-num" style="color:{pr_color};">'
            f'    {pr_arrow} {sign}{delta_pr:.1f}pp</div>'
            f'  <div class="delta-lbl">Pass Rate Change<br><br>'
            f'    <span style="color:{C["txt"]};">'
            f'    {wow.get("this_avg",0):.1f}% this week vs '
            f'    {wow.get("last_avg",0):.1f}% last week</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with c2:
        nf_color = C["red"]   if new_fails > 0 else C["green"]
        nf_arrow = "▲"        if new_fails > 0 else "✓"
        st.markdown(
            f'<div class="delta-card">'
            f'  <div class="delta-num" style="color:{nf_color};">'
            f'    {nf_arrow} {new_fails}</div>'
            f'  <div class="delta-lbl">New Failures<br><br>'
            f'    <span style="color:{C["txt"]};">Tests failing now that passed last week</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with c3:
        tf_color = C["green"] if tests_fixed > 0 else C["muted"]
        tf_arrow = "▼"        if tests_fixed > 0 else "—"
        st.markdown(
            f'<div class="delta-card">'
            f'  <div class="delta-num" style="color:{tf_color};">'
            f'    {tf_arrow} {tests_fixed}</div>'
            f'  <div class="delta-lbl">Tests Fixed<br><br>'
            f'    <span style="color:{C["txt"]};">Tests passing now that failed last week</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    if delta_pr >= 2 and tests_fixed > 0:
        msg   = f"📈  Good week — pass rate improved by {delta_pr:+.1f}pp and {tests_fixed} test(s) were fixed."
        color = C["green"]
    elif delta_pr <= -2 or new_fails > 0:
        parts = []
        if new_fails > 0:  parts.append(f"{new_fails} new failure(s) introduced")
        if delta_pr < -2:  parts.append(f"pass rate dropped {abs(delta_pr):.1f}pp")
        msg   = "📉  Regression detected — " + " and ".join(parts) + ". Investigate before next sprint."
        color = C["red"]
    else:
        msg   = f"📊  Stable week — pass rate held at ~{wow.get('this_avg',0):.1f}%, no regressions."
        color = C["blue"]

    st.markdown(
        f'<div style="background:{color}12; border:1px solid {color}33; '
        f'border-radius:8px; padding:.65rem 1rem; font-size:.82rem; '
        f'color:{color}; margin-top:.8rem;">{msg}</div>',
        unsafe_allow_html=True,
    )


def render_footer(db_path: str, df_all: pd.DataFrame) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.markdown(
        f'<div class="dash-footer">'
        f'  <span>Phase 3 · Automation Test Stability Analytics</span>'
        f'  <span>{len(df_all)} runs · refreshed {now} · db: {db_path}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


# MAIN

def _parse_db_path() -> str:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--db" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--db="):
            return arg.split("=", 1)[1]
    return DEFAULT_DB


def main() -> None:
    inject_css()

    db_path = _parse_db_path()
    conn    = get_connection(db_path)
    st.session_state["_conn"] = conn

    with st.spinner("Loading analytics data…"):
        df_all = fetch_all_runs(conn)
        latest = fetch_latest_run(conn)

    if df_all.empty:
        st.error("No run data found. Please run `python pipeline.py` first.")
        st.stop()

    trend_window, show_stable_flaky, selected_run_id, drift_test = \
        render_sidebar(df_all, conn, db_path)

    render_header(latest)
    render_q1(latest, df_all)
    render_q2(df_all, trend_window)
    render_q3(conn, selected_run_id)
    render_q4(conn, show_stable_flaky, drift_test)
    render_q5(conn)
    render_footer(db_path, df_all)


if __name__ == "__main__":
    main()