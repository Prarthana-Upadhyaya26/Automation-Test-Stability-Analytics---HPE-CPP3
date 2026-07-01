"""
ML Insights — Explainable AI Analytics Dashboard
==================================================
Interactive XAI views for CI test stability models.
Each ML module runs only when its run button is clicked.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = Path(__file__).resolve().parent
for candidate in (PROJECT_ROOT, PAGES_DIR):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

try:
    from .lib.ml_pipeline import (
        build_duration_drift_report,
        build_failure_cluster_report,
        build_flakiness_predictions,
        build_run_anomaly_report,
    )
    from .lib.ml_xai import (
        build_aggregate_feature_importance,
        build_cluster_keywords,
        build_cluster_root_cause_hints,
        build_cluster_summary,
        build_cluster_time_trends,
        build_test_prioritization,
        build_run_health_enriched,
        cluster_time_trend_chart,
        enrich_flakiness_predictions,
        recommendation_badge,
        risk_badge,
        run_health_chart,
        workflow_steps_markdown,
    )
except ImportError:
    from lib.ml_pipeline import (
        build_duration_drift_report,
        build_failure_cluster_report,
        build_flakiness_predictions,
        build_run_anomaly_report,
    )
    from lib.ml_xai import (
        build_aggregate_feature_importance,
        build_cluster_keywords,
        build_cluster_root_cause_hints,
        build_cluster_summary,
        build_cluster_time_trends,
        build_test_prioritization,
        build_run_health_enriched,
        cluster_time_trend_chart,
        enrich_flakiness_predictions,
        recommendation_badge,
        risk_badge,
        run_health_chart,
        workflow_steps_markdown,
    )

try:
    from pipeline import load_multi_db
    PIPELINE2_AVAILABLE = True
except ImportError:
    load_multi_db = None  # type: ignore[assignment]
    PIPELINE2_AVAILABLE = False

DEFAULT_DB = "./analytics.db"

PALETTE = {
    "bg": "#0D1117",
    "card": "#161B22",
    "border": "#30363D",
    "txt": "#E6EDF3",
    "muted": "#8B949E",
    "blue": "#58A6FF",
    "green": "#3FB950",
    "amber": "#D29922",
    "red": "#F85149",
    "purple": "#BC8CFF",
}

ML_MODULES = [
    {"id": "ml1", "icon": "🎯", "title": "ML1 · Flakiness Intelligence", "help": "Run flakiness classifier"},
    {"id": "ml2", "icon": "⏱", "title": "ML2 · Duration Drift", "help": "Run duration drift detection"},
    {"id": "ml3", "icon": "💬", "title": "ML3 · Failure Clustering", "help": "Run failure message clustering"},
    {"id": "ml4", "icon": "🏥", "title": "ML4 · Run Health", "help": "Run anomaly detection"},
    {"id": "ml5", "icon": "🚀", "title": "ML5 · Test Prioritization", "help": "Run prioritization engine"},
]


st.set_page_config(page_title="ML Insights", page_icon="🤖", layout="wide")


def _inject_css() -> None:
    c = PALETTE
    st.markdown(
        f"""
        <style>
        .xai-hero {{
            background: linear-gradient(135deg, {c["card"]} 0%, {c["bg"]} 100%);
            border: 1px solid {c["border"]};
            border-radius: 12px;
            padding: 1.4rem 1.6rem;
            margin-bottom: 1.2rem;
        }}
        .xai-hero h1 {{ margin: 0; font-size: 1.6rem; color: {c["txt"]}; }}
        .xai-hero p {{ margin: .4rem 0 0; color: {c["muted"]}; font-size: .92rem; }}
        .module-header {{
            font-size: 1.15rem;
            font-weight: 700;
            color: {c["txt"]};
            margin: 1.2rem 0 .8rem;
            padding-bottom: .5rem;
            border-bottom: 1px solid {c["border"]};
        }}
        .ml-slot {{
            min-height: 120px;
            border: 1px dashed {c["border"]};
            border-radius: 10px;
            padding: 1rem;
            margin-bottom: 1rem;
            background: {c["card"]}55;
        }}
        .ml-slot-empty {{
            color: {c["muted"]};
            font-size: .88rem;
            text-align: center;
            padding: 2.5rem 1rem;
        }}
        .spec-shell {{
            display: flex;
            flex-direction: column;
            gap: 0.8rem;
            margin-top: 0.25rem;
        }}
        .spec-card {{
            background: {c["card"]};
            border: 1px solid {c["border"]};
            border-radius: 12px;
            overflow: hidden;
        }}
        .spec-card-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.8rem 1rem;
            border-bottom: 1px solid {c["border"]};
        }}
        .spec-title-row {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .spec-title {{
            font-size: 0.95rem;
            font-weight: 600;
            color: {c["txt"]};
        }}
        .spec-subtitle {{
            font-size: 0.78rem;
            color: {c["muted"]};
            margin-top: 2px;
        }}
        .spec-badge {{
            font-size: 10px;
            font-weight: 600;
            padding: 2px 7px;
            border-radius: 4px;
        }}
        .spec-badge-purple {{ background: #4f3d9a; color: #f2eeff; }}
        .spec-badge-teal {{ background: #144d45; color: #dff8f2; }}
        .spec-badge-amber {{ background: #5d3f00; color: #ffe6af; }}
        .spec-badge-blue {{ background: #16416d; color: #d9eaff; }}
        .spec-card-body {{ padding: 1rem; }}
        .spec-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }}
        .spec-item {{
            background: rgba(255,255,255,0.03);
            border-radius: 8px;
            padding: 0.6rem 0.8rem;
        }}
        .spec-item-key {{
            font-size: 10px;
            color: {c["muted"]};
            margin-bottom: 2px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .spec-item-val {{
            font-size: 12px;
            color: {c["txt"]};
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        }}
        .spec-formula {{
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 11px;
            background: rgba(255,255,255,0.03);
            border-radius: 6px;
            padding: 0.55rem 0.7rem;
            color: {c["txt"]};
            border: 1px solid {c["border"]};
            line-height: 1.65;
            margin-top: 8px;
        }}
        .spec-note {{
            font-size: 11px;
            color: {c["muted"]};
            margin-top: 8px;
            padding: 0.55rem 0.7rem;
            background: rgba(255,255,255,0.03);
            border-radius: 6px;
            border-left: 2px solid {c["border"]};
        }}
        .spec-metrics {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 8px;
            margin-top: 10px;
        }}
        .spec-metric {{
            text-align: center;
            background: rgba(255,255,255,0.03);
            border-radius: 8px;
            padding: 0.5rem 0.4rem;
        }}
        .spec-metric-num {{ font-size: 16px; font-weight: 600; color: {c["txt"]}; }}
        .spec-metric-lbl {{ font-size: 10px; color: {c["muted"]}; margin-top: 1px; }}
        .spec-metric-num.good {{ color: #3FB950; }}
        .spec-metric-num.warn {{ color: #D29922; }}
        .spec-metric-num.info {{ color: #58A6FF; }}
        .spec-factor-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px;
            margin: 10px 0 12px;
        }}
        .spec-factor {{
            background: rgba(255,255,255,0.03);
            border-radius: 8px;
            padding: 0.6rem 0.75rem;
            border-left: 2px solid {c["border"]};
        }}
        .spec-factor.blue {{ border-color: #58A6FF; }}
        .spec-factor.amber {{ border-color: #D29922; }}
        .spec-factor.purple {{ border-color: #BC8CFF; }}
        .spec-factor-name {{ font-size: 11px; font-weight: 600; color: {c["txt"]}; margin-bottom: 2px; }}
        .spec-factor-desc {{ font-size: 10px; color: {c["muted"]}; line-height: 1.4; }}
        .spec-cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }}
        .spec-cols-item {{ background: rgba(255,255,255,0.03); border-radius: 6px; padding: 0.55rem 0.7rem; }}
        .spec-cols-title {{ font-size: 11px; font-weight: 600; color: {c["txt"]}; margin-bottom: 4px; }}
        .spec-cols-list {{ font-size: 11px; color: {c["muted"]}; line-height: 1.7; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
        div[data-testid="column"] button {{
            font-size: 1.6rem;
            padding: 0.65rem 0.5rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_impl_card(
    title: str,
    subtitle: str,
    badge_text: str,
    badge_class: str,
    details: list[tuple[str, str]],
    formula: str | None = None,
    metrics: list[tuple[str, str, str]] | None = None,
    note: str | None = None,
) -> str:
    details_html = "".join(
        f"<div class='spec-item'><div class='spec-item-key'>{label}</div><div class='spec-item-val'>{value}</div></div>"
        for label, value in details
    )
    formula_html = f"<div class='spec-formula'>{formula}</div>" if formula else ""
    metrics_html = ""
    if metrics:
        metric_items = "".join(
            f"<div class='spec-metric'><div class='spec-metric-num {cls}'>{value}</div><div class='spec-metric-lbl'>{label}</div></div>"
            for label, value, cls in metrics
        )
        metrics_html = f"<div class='spec-metrics'>{metric_items}</div>"
    note_html = f"<div class='spec-note'>{note}</div>" if note else ""
    return f"""
    <div class='spec-card'>
      <div class='spec-card-header'>
        <div>
          <div class='spec-title-row'>
            <span class='spec-badge spec-badge-{badge_class}'>{badge_text}</span>
            <span class='spec-title'>{title}</span>
          </div>
          <div class='spec-subtitle'>{subtitle}</div>
        </div>
      </div>
      <div class='spec-card-body'>
        <div class='spec-grid'>{details_html}</div>
        {formula_html}
        {metrics_html}
        {note_html}
      </div>
    </div>
    """


def _render_priority_card() -> str:
    return """
    <div class='spec-card'>
      <div class='spec-card-header'>
        <div>
          <div class='spec-title-row'>
            <span class='spec-badge spec-badge-blue'>ML5</span>
            <span class='spec-title'>Test priority list — risk score ranking</span>
          </div>
          <div class='spec-subtitle'>Formula · output columns · display spec</div>
        </div>
      </div>
      <div class='spec-card-body'>
        <div class='spec-formula'>risk_score = P(fail) × (1 + drift_factor) / predicted_duration</div>
        <div class='spec-factor-grid'>
          <div class='spec-factor blue'>
            <div class='spec-factor-name'>P(fail)</div>
            <div class='spec-factor-desc'>ML1 predict_proba output · threshold 0.70 on test set · range 0–1</div>
          </div>
          <div class='spec-factor amber'>
            <div class='spec-factor-name'>drift_factor</div>
            <div class='spec-factor-desc'>pct_increase / 100 from ML2 drift summary · 0 if test not drifting</div>
          </div>
          <div class='spec-factor purple'>
            <div class='spec-factor-name'>predicted_duration</div>
            <div class='spec-factor-desc'>exp(LinearRegression output) · log-transformed prediction in seconds</div>
          </div>
        </div>
        <div class='spec-note'>High P(fail) × large drift → run first. Divide by duration so a fast, risky test beats a slow, equally-risky one — maximises defect detection per CI minute.</div>
        <div class='spec-cols'>
          <div class='spec-cols-item'>
            <div class='spec-cols-title'>Output dataframe columns</div>
            <div class='spec-cols-list'>
              test_name<br>
              build_no_target<br>
              timestamp_target<br>
              failure_probability<br>
              predicted_duration (s)<br>
              pct_increase (drift %)<br>
              drift_factor_value<br>
              risk_score ← sort desc
            </div>
          </div>
          <div class='spec-cols-item'>
            <div class='spec-cols-title'>Dashboard display spec</div>
            <div class='spec-cols-list'>
              rank #1, #2, #3 …<br>
              test_name<br>
              risk_score (3 dp)<br>
              P(fail) as %<br>
              drift as % (+157%)<br>
              pred_dur as "12.3 s"<br>
              badge: HIGH / MED / LOW<br>
              color row by risk tier
            </div>
          </div>
        </div>
        <div class='spec-formula'>HIGH → risk_score ≥ 75th percentile of current batch<br>MED → 25th – 75th percentile<br>LOW → below 25th percentile<br><br>Use dynamic percentiles (not fixed values) so tiers stay meaningful as data grows.</div>
      </div>
    </div>
    """


def _implementation_expander(title: str, body: str | None = None, steps: list[str] | None = None, html: str | None = None) -> None:
    with st.expander(title, expanded=False):
        if html:
            st.markdown(f"<div class='spec-shell'>{html}</div>", unsafe_allow_html=True)
        elif body:
            st.markdown(body)
        if steps:
            st.markdown(workflow_steps_markdown(steps))


def _parse_db_paths() -> list[str]:
    return [str((Path.cwd() / DEFAULT_DB).resolve())]


def standardize_columns(df_runs: pd.DataFrame, df_results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
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
            df_results = df_results.merge(df_runs[["run_id", "timestamp"]], on="run_id", how="left")
            df_results.rename(columns={"timestamp": "run_timestamp"}, inplace=True)
        else:
            df_results["run_timestamp"] = None
    if "build_no" not in df_results.columns and "build_no" in df_runs.columns:
        df_results = df_results.merge(df_runs[["run_id", "build_no"]], on="run_id", how="left")
    return df_runs, df_results


def _fallback_single_read(db_path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
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
        SELECT tr.result_id, tr.run_id, r.team, r.suite_name, r.build_no,
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


@st.cache_resource
def load_db_data(db_paths_key: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    db_paths = [p.strip() for p in db_paths_key.split("|") if p.strip()]
    existing = [p for p in db_paths if Path(p).exists()]
    if not existing:
        return pd.DataFrame(), pd.DataFrame()

    if PIPELINE2_AVAILABLE and load_multi_db is not None:
        df_runs, df_results = load_multi_db(existing)
    else:
        df_runs, df_results = _fallback_single_read(existing[0])

    df_runs, df_results = standardize_columns(df_runs, df_results)
    df_runs["run_id"] = df_runs["run_id"].astype(str)
    df_results["run_id"] = df_results["run_id"].astype(str)
    return df_runs, df_results


def _plotly_dark(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=PALETTE["txt"], size=11),
        height=height,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor=PALETTE["border"], borderwidth=1),
    )
    fig.update_xaxes(gridcolor=PALETTE["border"], zerolinecolor=PALETTE["border"])
    fig.update_yaxes(gridcolor=PALETTE["border"], zerolinecolor=PALETTE["border"])
    return fig


def render_flakiness_intelligence(flakiness: pd.DataFrame, df_results: pd.DataFrame, top_n: int) -> None:
    st.markdown(f'<div class="module-header">{ML_MODULES[0]["title"]}</div>', unsafe_allow_html=True)
    _implementation_expander(
        "Implementation details",
        html=_render_impl_card(
            "Flakiness classifier",
            "Random Forest · binary FAIL prediction",
            "ML1",
            "purple",
            [
                ("Algorithm", "RandomForestClassifier"),
                ("Split strategy", "Time-series (75/15/10)"),
                ("n_estimators / depth", "200 / max_depth=8"),
                ("class_weight / leaf", "balanced / min_leaf=10"),
                ("Window size", "last 20 runs per test"),
                ("Threshold val / test", "0.75 val · 0.70 test"),
            ],
            formula="1. fail_rate_last_20 → strongest predictor<br>2. fail_rate_last_10 / fail_rate_last_5<br>3. flip_rate_last_w → instability signal<br>4. consecutive_failures / consecutive_passes<br>5. duration_last → subtle timing cue",
            metrics=[
                ("Val ROC-AUC", "0.9430", "good"),
                ("Test ROC-AUC", "0.8912", "info"),
                ("PASS F1", "0.95", "good"),
                ("FAIL F1 (test)", "0.60", "warn"),
            ],
            note="Val FAIL precision/recall: 0.66 · Test FAIL precision/recall: 0.60 — class imbalance reduces FAIL detection; balanced weights partially offset this. Overall accuracy: 0.92.",
        ),
    )

    enriched = enrich_flakiness_predictions(flakiness)
    importance = build_aggregate_feature_importance(df_results, top_n=15)

    col_a, col_b = st.columns([1.1, 1])
    with col_a:
        if importance.empty:
            st.info("Not enough history to compute feature importance.")
        else:
            fig = px.bar(
                importance,
                x="importance",
                y="feature",
                orientation="h",
                color="importance",
                color_continuous_scale=["#30363D", PALETTE["blue"]],
                title="Top 15 Feature Importances",
            )
            fig.update_layout(coloraxis_showscale=False, showlegend=False, yaxis=dict(categoryorder="total ascending"))
            _plotly_dark(fig, height=420)
            st.plotly_chart(fig, use_container_width=True)

    with col_b:
        st.markdown("**Prediction Results**")
        if enriched.empty:
            st.info("No flakiness predictions for the current selection.")
        else:
            view = enriched.head(top_n).copy()
            display = pd.DataFrame(
                {
                    "Test Name": view["test_name"],
                    "Flakiness Probability (%)": (view["failure_probability_pct"] * 100).round(1),
                    "Confidence": view["confidence"],
                    "Risk Category": view["risk"],
                    "Recommendation": view["recommendation"],
                }
            )
            styled_html = "<table style='width:100%;border-collapse:collapse;font-size:0.82rem;'>"
            styled_html += "<tr style='border-bottom:1px solid #30363D;color:#8B949E;text-align:left;'>"
            for col in display.columns:
                styled_html += f"<th style='padding:8px 6px;'>{col}</th>"
            styled_html += "</tr>"
            for _, row in display.iterrows():
                styled_html += "<tr style='border-bottom:1px solid #30363D44;'>"
                styled_html += f"<td style='padding:8px 6px;'>{row['Test Name']}</td>"
                styled_html += f"<td style='padding:8px 6px;'>{row['Flakiness Probability (%)']}%</td>"
                styled_html += f"<td style='padding:8px 6px;'>{row['Confidence']}</td>"
                styled_html += f"<td style='padding:8px 6px;'>{risk_badge(row['Risk Category'])}</td>"
                styled_html += f"<td style='padding:8px 6px;'>{recommendation_badge(row['Recommendation'])}</td>"
                styled_html += "</tr>"
            styled_html += "</table>"
            st.markdown(styled_html, unsafe_allow_html=True)


def render_duration_drift_intelligence(duration_drift: pd.DataFrame) -> None:
    st.markdown(f'<div class="module-header">{ML_MODULES[1]["title"]}</div>', unsafe_allow_html=True)
    _implementation_expander(
        "Implementation details",
        html=_render_impl_card(
            "Duration drift detection",
            "Z-score baseline · Linear Regression prediction",
            "ML2",
            "teal",
            [
                ("Baseline window", "DRIFT_WINDOW = 15"),
                ("Flag threshold", "DRIFT_SIGMA = 3.0σ"),
                ("Confirm rule", "MIN_CONSECUTIVE = 3"),
                ("Baseline source", "PASS runs only"),
                ("Prediction model", "LinearRegression"),
                ("Transform", "log(duration_s)"),
            ],
            formula="z = (duration − baseline_mean) / baseline_std<br>flagged → z ≥ 3.0σ<br>confirmed → ≥ 3 consecutive flagged in rolling(10, min=5)<br><br>pred features: historical_mean · rolling_mean_5<br>rolling_std · prev_duration · build_num",
            metrics=[
                ("Tests drifted", "3", "warn"),
                ("BulkImport", "+157%", "info"),
                ("ExportChart", "+200%", "info"),
                ("R² (test)", "0.85", "good"),
            ],
            note="MAE ≈ 1.5 s on actual scale. BulkImport: 10 s → 26 s progressive. ExportChart: 4 s → 12 s step-change at build 50. ValidCredentials: odd/even seasonal.",
        ),
    )

    if duration_drift.empty:
        st.info("No confirmed duration drift detected above the threshold.")
        return

    st.markdown("**Confirmed drift signals**")
    st.dataframe(
        duration_drift.rename(
            columns={
                "test_name": "Test Name",
                "baseline_duration_s": "Baseline (s)",
                "drifted_duration_s": "Drifted (s)",
                "percent_increase": "% Increase",
                "first_drift_build": "First Drift Build",
                "model_confidence": "Confidence",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )


def render_failure_clustering(failure_clusters: pd.DataFrame, df_results: pd.DataFrame) -> None:
    st.markdown(f'<div class="module-header">{ML_MODULES[2]["title"]}</div>', unsafe_allow_html=True)
    _implementation_expander(
        "Implementation details",
        html=_render_impl_card(
            "Failure clustering",
            "TF-IDF + KMeans · 5 root-cause categories",
            "ML3",
            "amber",
            [
                ("Vectoriser", "TfidfVectorizer"),
                ("max_features / ngram", "500 · (1, 2)"),
                ("sublinear_tf / min_df", "True · 2"),
                ("Actual vocab size", "~118 terms"),
                ("k chosen", "5 (elbow ≈ k=4)"),
                ("Hybrid override", "failure_kw == Environment_Setup"),
            ],
            formula="preprocess: lower → em-dash→space → strip punct → digits→NUM → drop 1-char<br><br>confidence = 1 − min(centroid_dist / percentile_95, 1.0)<br>low-conf threshold: 0.30<br><br>artefacts: tfidf_vectorizer.pkl · kmeans_model.pkl<br>cluster_labels.pkl · train_distances.pkl",
            metrics=[
                ("ARI text-only", "0.94", "warn"),
                ("ARI hybrid", "1.00", "good"),
                ("Silhouette k=5", "0.3987", "info"),
                ("Failure rows", "493", "info"),
            ],
            note="5 categories: timeout · element · assertion · data · environment (4% of failures — environment absorbed by timeout in text-only mode; hybrid fixes it to ARI 1.00).",
        ),
    )

    summary = build_cluster_summary(failure_clusters)
    keywords = build_cluster_keywords(failure_clusters)

    if summary.empty:
        st.info("No failure clusters found.")
        return

    trends = build_cluster_time_trends(failure_clusters, df_results)

    col_bar, col_trend = st.columns([1, 1.4])
    with col_bar:
        fig = px.bar(
            summary,
            x="cluster_category",
            y="size",
            color="cluster_category",
            text="size",
            title=f"{len(summary)} clusters detected",
        )
        _plotly_dark(fig, height=280)
        st.plotly_chart(fig, use_container_width=True)

    with col_trend:
        if not trends.empty:
            trend_fig = cluster_time_trend_chart(trends, PALETTE)
            _plotly_dark(trend_fig, height=280)
            st.plotly_chart(trend_fig, use_container_width=True)

    st.markdown("<div style='margin-top:1.1rem;'></div>", unsafe_allow_html=True)
    hints = build_cluster_root_cause_hints(trends) if not trends.empty else []
    if hints:
        st.markdown("**Root cause hints**")
        for item in hints:
            st.markdown(f"- `{item['category']}` builds **{item['build_start']}–{item['build_end']}**: {item['hint']}")

    st.markdown("<div style='margin-top:1.4rem;'></div>", unsafe_allow_html=True)
    cluster_options = summary.apply(lambda r: f"{r['cluster_category']} (n={r['size']})", axis=1).tolist()
    selected = st.selectbox("Explore cluster", cluster_options)
    selected_idx = cluster_options.index(selected)
    cluster_row = summary.iloc[selected_idx]
    cluster_id = int(cluster_row["cluster_label"])
    category = cluster_row["cluster_category"]

    st.markdown(f"**{category.title()} cluster** — {cluster_row['size']} failures")
    kw = keywords.get(cluster_id, [])
    if kw:
        st.markdown("Top keywords: " + ", ".join(f"`{k}`" for k in kw[:8]))

    examples = (
        failure_clusters[failure_clusters["cluster_label"] == cluster_id]["failure_msg"]
        .dropna()
        .astype(str)
        .head(5)
        .tolist()
    )
    for i, msg in enumerate(examples, 1):
        st.markdown(f"{i}. {msg[:200]}")


def render_run_health_intelligence(df_runs: pd.DataFrame, anomalies: pd.DataFrame) -> None:
    st.markdown(f'<div class="module-header">{ML_MODULES[3]["title"]}</div>', unsafe_allow_html=True)
    _implementation_expander(
        "Implementation details",
        html=_render_impl_card(
            "Pass rate anomaly detection",
            "Z-score + Isolation Forest · dual method",
            "ML4",
            "blue",
            [
                ("Rolling window", "ROLLING_WINDOW = 10"),
                ("Z-score threshold", "ANOMALY_SIGMA = 2.0"),
                ("IF contamination", "0.05 (5% assumed)"),
                ("IF n_estimators", "200"),
            ],
            formula="pass_rate_pct · pass_rate_change<br>deviation (roll_mean − pass_rate) · fail_rate_prev<br><br>z_score = (roll_mean − pass_rate) / roll_std.clip(min=1.0)<br>anomaly_zscore → z ≥ 2.0σ<br>anomaly_if → IsolationForest predicts −1",
            metrics=[
                ("Z-score flags", "2", "warn"),
                ("Anomaly builds", "36, 37", "info"),
                ("IF flags (5%)", "~5", "info"),
                ("Agree core", "Both", "good"),
            ],
            note="Z-score catches sharp dips (builds 36–37 at 25–28% pass rate). Isolation Forest is broader — flags runs with unusual change patterns even if not extreme dips. Both methods agree on the core anomaly builds.",
        ),
    )

    health = build_run_health_enriched(df_runs, anomalies)
    if health.empty:
        st.info("No run history available.")
        return

    fig = run_health_chart(health, PALETTE)
    _plotly_dark(fig, height=400)
    st.plotly_chart(fig, use_container_width=True)

    anomaly_rows = health[health["detection_method"] != "Normal"].copy()
    if anomaly_rows.empty:
        st.success("No anomalous runs detected.")
        return



def render_test_prioritization(
    flakiness: pd.DataFrame,
    duration_drift: pd.DataFrame,
    df_results: pd.DataFrame,
    top_n: int,
) -> None:
    st.markdown(f'<div class="module-header">{ML_MODULES[4]["title"]}</div>', unsafe_allow_html=True)
    _implementation_expander(
        "Implementation details",
        html=_render_priority_card(),
    )

    priority = build_test_prioritization(flakiness, duration_drift, df_results, top_n=top_n)
    if priority.empty:
        st.info("Not enough data to generate prioritization recommendations.")
        return

    fig = px.bar(
        priority,
        x="priority_score",
        y="test_name",
        orientation="h",
        color="recommendation",
        hover_data={
            "flakiness_probability": True,
            "predicted_duration_s": True,
            "historical_failure_rate": True,
        },
        color_discrete_map={
            "Isolate": PALETTE["red"],
            "Execute Early": PALETTE["blue"],
            "Retry Automatically": PALETTE["amber"],
            "Monitor": PALETTE["green"],
        },
    )
    _plotly_dark(fig, height=max(320, 36 * len(priority)))
    st.plotly_chart(fig, use_container_width=True)

    table_html = "<table style='width:100%;border-collapse:collapse;font-size:0.82rem;'>"
    table_html += "<tr style='border-bottom:1px solid #30363D;color:#8B949E;'>"
    for col in ["Rank", "Test Name", "Flakiness %", "Pred. Duration", "Fail Rate %", "Score", "Recommendation"]:
        table_html += f"<th style='padding:8px 6px;text-align:left;'>{col}</th>"
    table_html += "</tr>"
    for _, row in priority.iterrows():
        table_html += "<tr style='border-bottom:1px solid #30363D44;'>"
        table_html += f"<td style='padding:8px 6px;'>#{int(row['priority_rank'])}</td>"
        table_html += f"<td style='padding:8px 6px;'>{row['test_name']}</td>"
        table_html += f"<td style='padding:8px 6px;'>{row['flakiness_probability']}%</td>"
        table_html += f"<td style='padding:8px 6px;'>{row['predicted_duration_s']}s</td>"
        table_html += f"<td style='padding:8px 6px;'>{row['historical_failure_rate']}%</td>"
        table_html += f"<td style='padding:8px 6px;'>{row['priority_score']}</td>"
        table_html += f"<td style='padding:8px 6px;'>{recommendation_badge(row['recommendation'])}</td>"
        table_html += "</tr>"
    table_html += "</table>"
    st.markdown(table_html, unsafe_allow_html=True)


def _render_module_slot(module_id: str, df_runs: pd.DataFrame, df_results: pd.DataFrame, top_n: int) -> None:
    if not st.session_state.get(f"ran_{module_id}"):
        st.markdown(
            '<div class="ml-slot"><div class="ml-slot-empty">Click the module button above to run this analysis.</div></div>',
            unsafe_allow_html=True,
        )
        return

    with st.spinner("Running analysis…"):
        if module_id == "ml1":
            flakiness = build_flakiness_predictions(df_results)
            render_flakiness_intelligence(flakiness, df_results, top_n)
        elif module_id == "ml2":
            duration_drift = build_duration_drift_report(df_results)
            render_duration_drift_intelligence(duration_drift)
        elif module_id == "ml3":
            failure_clusters = build_failure_cluster_report(df_results)
            render_failure_clustering(failure_clusters, df_results)
        elif module_id == "ml4":
            anomalies = build_run_anomaly_report(df_runs, df_results)
            render_run_health_intelligence(df_runs, anomalies)
        elif module_id == "ml5":
            flakiness = build_flakiness_predictions(df_results)
            duration_drift = build_duration_drift_report(df_results)
            render_test_prioritization(flakiness, duration_drift, df_results, top_n)


def main() -> None:
    _inject_css()
    db_paths = _parse_db_paths()
    db_paths_key = "|".join(sorted(db_paths))
    df_runs, df_results = load_db_data(db_paths_key)

    st.sidebar.header("Data Selection")
    for p in db_paths:
        st.sidebar.code(p, language=None)

    if df_runs.empty:
        st.warning(
            "No analytics database found. Launch with "
            "`streamlit run pages/2_ML_Insights.py -- --db ./analytics.db`."
        )
        return

    db_sources = sorted(df_runs["_source_db"].unique().tolist()) if "_source_db" in df_runs.columns else ["unknown"]
    selected_source = st.sidebar.selectbox("Database source", ["All"] + db_sources)
    if selected_source != "All":
        df_runs_filtered = df_runs[df_runs["_source_db"] == selected_source]
        df_results_filtered = df_results[df_results["_source_db"] == selected_source]
    else:
        df_runs_filtered = df_runs
        df_results_filtered = df_results

    st.sidebar.markdown("---")
    top_n = st.sidebar.slider("Top N results", min_value=5, max_value=25, value=10, step=1)
    st.sidebar.markdown(f"**Runs:** {len(df_runs_filtered)}")
    st.sidebar.markdown(f"**Results:** {len(df_results_filtered)}")

    st.markdown("**Run an ML module**")

    btn_cols = st.columns(len(ML_MODULES))
    for col, module in zip(btn_cols, ML_MODULES):
        with col:
            if st.button(module["title"], key=f"btn_{module['id']}", help=module["help"], use_container_width=True):
                st.session_state[f"ran_{module['id']}"] = True
                st.session_state["scroll_to"] = module["id"]


    for module in ML_MODULES:
        st.markdown("---")
        _render_module_slot(module["id"], df_runs_filtered, df_results_filtered, top_n)


if __name__ == "__main__":
    main()
