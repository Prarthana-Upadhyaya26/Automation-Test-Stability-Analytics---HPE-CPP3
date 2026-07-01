"""Presentation-layer helpers for explainable ML analytics views."""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

try:
    from .ml_pipeline import (
        _build_run_order,
        build_duration_drift_report,
        build_feature_importance,
        build_flakiness_predictions,
    )
except ImportError:
    from ml_pipeline import (
        _build_run_order,
        build_duration_drift_report,
        build_feature_importance,
        build_flakiness_predictions,
    )

RISK_COLORS = {"High": "#F85149", "Moderate": "#D29922", "Low": "#3FB950"}
RECOMMENDATION_COLORS = {
    "Execute Early": "#58A6FF",
    "Isolate": "#F85149",
    "Retry Automatically": "#D29922",
    "Monitor": "#3FB950",
}


def build_aggregate_feature_importance(df_results: pd.DataFrame, top_n: int = 15) -> pd.DataFrame:
    """Top feature importances from the global flakiness Random Forest."""
    return build_feature_importance(df_results, top_n=top_n)


def enrich_flakiness_predictions(flakiness: pd.DataFrame) -> pd.DataFrame:
    if flakiness.empty:
        return flakiness

    out = flakiness.copy()
    out["confidence"] = out["failure_probability_pct"].apply(lambda p: round(max(p, 1 - p), 2))
    out["recommendation"] = out.apply(_flakiness_recommendation, axis=1)
    out["risk"] = out["risk"].replace({"Moderate": "Medium"})
    return out


def _flakiness_recommendation(row: pd.Series) -> str:
    risk = row.get("risk", "Low")
    trend = row.get("trend", "Stable")
    if risk == "High":
        return "Execute Early" if trend == "Increasing" else "Isolate"
    if risk in ("Moderate", "Medium"):
        return "Retry Automatically"
    return "Monitor"


def build_duration_forecast(
    df_results: pd.DataFrame,
    test_name: str,
    drift_report: pd.DataFrame,
) -> dict[str, float | str]:
    group = _build_run_order(df_results)
    group = group[(group["test_name"] == test_name) & (group["status"] == "PASS")].sort_values("_run_num", kind="mergesort")
    if len(group) < 5:
        return {"predicted_duration_s": 0.0, "ci_low_s": 0.0, "ci_high_s": 0.0, "drift_status": "Insufficient data"}

    rolling = group["duration_s"].rolling(5, min_periods=1).mean()
    pred = float(rolling.iloc[-1])
    std = float(group["duration_s"].tail(5).std(ddof=0)) if len(group) >= 5 else 0.5

    drift_status = "Stable"
    if not drift_report.empty and test_name in drift_report["test_name"].values:
        row = drift_report[drift_report["test_name"] == test_name].iloc[0]
        if row["percent_increase"] > 25:
            drift_status = "Drifting"
        elif row["percent_increase"] > 15:
            drift_status = "Watch"

    return {
        "predicted_duration_s": round(pred, 2),
        "ci_low_s": round(max(0.0, pred - 1.96 * std), 2),
        "ci_high_s": round(pred + 1.96 * std, 2),
        "drift_status": drift_status,
    }


def build_cluster_summary(failure_clusters: pd.DataFrame) -> pd.DataFrame:
    if failure_clusters.empty:
        return pd.DataFrame(columns=["cluster_label", "cluster_category", "size"])

    summary = (
        failure_clusters.groupby(["cluster_label", "cluster_category"], as_index=False)
        .size()
        .rename(columns={"size": "size"})
        .sort_values("size", ascending=False)
    )
    return summary


def build_cluster_time_trends(
    failure_clusters: pd.DataFrame,
    df_results: pd.DataFrame,
) -> pd.DataFrame:
    """Per-build failure counts for each cluster category (wide format)."""
    if failure_clusters.empty:
        return pd.DataFrame()

    merged = failure_clusters.copy()
    if "build_no" not in merged.columns and not df_results.empty and "build_no" in df_results.columns:
        run_build = df_results[["run_id", "build_no"]].drop_duplicates("run_id")
        merged = merged.merge(run_build, on="run_id", how="left")

    if "build_no" not in merged.columns:
        merged["build_no"] = merged["run_id"].astype(str).str.extract(r"(\d+)$").astype(float)

    merged["build_no"] = pd.to_numeric(merged["build_no"], errors="coerce")
    merged = merged.dropna(subset=["build_no"])
    if merged.empty:
        return pd.DataFrame()

    counts = (
        merged.groupby(["build_no", "cluster_category"], as_index=False)
        .size()
        .rename(columns={"size": "count"})
    )
    pivot = (
        counts.pivot(index="build_no", columns="cluster_category", values="count")
        .fillna(0)
        .astype(int)
        .reset_index()
        .sort_values("build_no")
    )
    return pivot


def _find_peak_build_window(series: pd.Series, min_width: int = 4, max_width: int = 14) -> tuple[int, int, float] | None:
    """Return (start_idx, end_idx, peak_ratio) for the strongest contiguous build window."""
    values = series.astype(float).values
    if len(values) < min_width:
        return None

    baseline = float(values.mean())
    if baseline <= 0:
        return None

    best: tuple[float, int, int, float] | None = None
    upper = min(max_width, len(values))
    for width in range(min_width, upper + 1):
        for start in range(0, len(values) - width + 1):
            window = values[start : start + width]
            window_mean = float(window.mean())
            if window_mean < baseline * 1.35:
                continue
            score = float(window.sum())
            peak_ratio = float(window.max()) / baseline
            if best is None or score > best[0]:
                best = (score, start, start + width - 1, peak_ratio)

    if best is None:
        return None
    _, start_i, end_i, peak_ratio = best
    return start_i, end_i, peak_ratio


def build_cluster_root_cause_hints(trends: pd.DataFrame) -> list[dict[str, str]]:
    """Derive short diagnostic hints from cluster count spikes over build ranges."""
    if trends.empty or "build_no" not in trends.columns:
        return []

    categories = [c for c in trends.columns if c != "build_no"]
    hints: list[dict[str, str]] = []

    hint_templates = {
        "timeout": (
            "Timeout failures spiked across builds {start}–{end} "
            "({ratio:.1f}× typical) — likely infrastructure or network degradation."
        ),
        "environment": (
            "Environment/setup failures concentrated in builds {start}–{end} "
            "({ratio:.1f}× typical) — check CI agents, staging health, or deploy gates."
        ),
        "element": (
            "'Element not found' errors clustered around builds {start}–{end} "
            "({ratio:.1f}× typical) — probable UI locator or DOM change from a sprint deploy."
        ),
        "data": (
            "Data validation failures peaked in builds {start}–{end} "
            "({ratio:.1f}× typical) — review export/import or fixture data changes."
        ),
        "assertion": (
            "HTTP assertion failures rose in builds {start}–{end} "
            "({ratio:.1f}× typical) — possible API contract or backend regression."
        ),
    }

    builds = trends["build_no"].astype(int).tolist()

    for category in categories:
        series = trends[category].astype(float)
        if series.sum() < 3:
            continue

        window = _find_peak_build_window(series)
        if window is None:
            continue
        start_i, end_i, ratio = window

        template = hint_templates.get(
            category,
            "{category} failures peaked in builds {start}–{end} ({ratio:.1f}× typical).",
        )
        text = template.format(
            start=builds[start_i],
            end=builds[end_i],
            ratio=ratio,
            category=category,
        )
        hints.append({"category": category, "build_start": builds[start_i], "build_end": builds[end_i], "hint": text})

    hints.sort(key=lambda h: (h["build_start"], h["category"]))
    return hints


def cluster_time_trend_chart(trends: pd.DataFrame, palette: dict[str, str]) -> go.Figure:
    """Line chart of cluster failure counts over build number."""
    fig = go.Figure()
    if trends.empty or "build_no" not in trends.columns:
        return fig

    category_colors = {
        "timeout": palette.get("amber", "#D29922"),
        "element": palette.get("blue", "#58A6FF"),
        "assertion": palette.get("purple", "#BC8CFF"),
        "data": palette.get("green", "#3FB950"),
        "environment": palette.get("red", "#F85149"),
        "unknown": palette.get("muted", "#8B949E"),
    }
    categories = [c for c in trends.columns if c != "build_no"]

    for category in categories:
        if trends[category].sum() == 0:
            continue
        fig.add_trace(
            go.Scatter(
                x=trends["build_no"],
                y=trends[category],
                mode="lines+markers",
                name=category,
                line=dict(color=category_colors.get(category, palette.get("muted", "#8B949E")), width=2),
                marker=dict(size=4),
                hovertemplate=f"{category}<br>Build %{{x}}<br>Count %{{y}}<extra></extra>",
            )
        )

    fig.layout.title = None
    fig.update_layout(
        xaxis_title="Build Number",
        yaxis_title="Failure Count",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.08,
            x=0,
            xanchor="left",
            bgcolor="rgba(0,0,0,0)",
        ),
        margin=dict(l=15, r=15, t=45, b=25, pad=10),
    )
    return fig


def build_cluster_keywords(failure_clusters: pd.DataFrame, top_n: int = 8) -> dict[int, list[str]]:
    if failure_clusters.empty:
        return {}

    from sklearn.feature_extraction.text import TfidfVectorizer

    from ml_pipeline import preprocess_message

    messages = failure_clusters["failure_msg"].fillna("").astype(str).apply(preprocess_message)
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=200)
    try:
        X = vectorizer.fit_transform(messages)
    except ValueError:
        return {}

    terms = vectorizer.get_feature_names_out()
    keywords: dict[int, list[str]] = {}
    for cluster_id in sorted(failure_clusters["cluster_label"].unique()):
        mask = failure_clusters["cluster_label"] == cluster_id
        if not mask.any():
            continue
        cluster_matrix = X[mask.values]
        scores = np.asarray(cluster_matrix.mean(axis=0)).ravel()
        top_idx = scores.argsort()[::-1][:top_n]
        keywords[int(cluster_id)] = [terms[i] for i in top_idx if scores[i] > 0]
    return keywords


def build_run_health_enriched(
    df_runs: pd.DataFrame,
    anomalies: pd.DataFrame,
    rolling_window: int = 10,
    anomaly_sigma: float = 2.0,
) -> pd.DataFrame:
    if df_runs.empty:
        return pd.DataFrame()

    runs = df_runs.sort_values("run_id").reset_index(drop=True).copy()
    runs["run_number"] = np.arange(1, len(runs) + 1)
    roll = runs["pass_rate_pct"].rolling(window=rolling_window, min_periods=3)
    runs["roll_mean"] = roll.mean().shift(1)
    runs["roll_std"] = roll.std().shift(1).fillna(5.0)
    runs["z_score"] = (runs["roll_mean"] - runs["pass_rate_pct"]) / runs["roll_std"].clip(lower=1.0)
    runs["zscore_anomaly"] = runs["z_score"] >= anomaly_sigma

    if not anomalies.empty:
        merge_cols = ["run_id", "is_anomalous", "anomaly_score", "failed_tests", "avg_duration_s", "if_anomaly"]
        available = [c for c in merge_cols if c in anomalies.columns]
        runs = runs.merge(anomalies[available], on="run_id", how="left")
    else:
        runs["is_anomalous"] = False
        runs["anomaly_score"] = 0.0
        runs["failed_tests"] = runs.get("failed", 0)
        runs["avg_duration_s"] = 0.0
        runs["if_anomaly"] = False

    runs["if_anomaly"] = runs["if_anomaly"].fillna(False)
    runs["flagged_by_both"] = runs["zscore_anomaly"] & runs["if_anomaly"]
    runs["detection_method"] = runs.apply(_detection_method_label, axis=1)
    runs["severity"] = runs.apply(_anomaly_severity, axis=1)
    return runs


def _detection_method_label(row: pd.Series) -> str:
    z = bool(row.get("zscore_anomaly"))
    iso = bool(row.get("if_anomaly"))
    if z and iso:
        return "Both"
    if z:
        return "Z-score"
    if iso:
        return "Isolation Forest"
    return "Normal"


def _anomaly_severity(row: pd.Series) -> str:
    if row.get("flagged_by_both"):
        return "Critical"
    if row.get("zscore_anomaly") or row.get("if_anomaly"):
        return "High"
    if float(row.get("z_score", 0)) >= 1.5:
        return "Medium"
    return "Low"


def build_test_prioritization(
    flakiness: pd.DataFrame,
    duration_drift: pd.DataFrame,
    df_results: pd.DataFrame,
    top_n: int = 25,
) -> pd.DataFrame:
    if flakiness.empty and duration_drift.empty:
        return pd.DataFrame()

    tests = set(flakiness["test_name"].tolist()) if not flakiness.empty else set()
    if not duration_drift.empty:
        tests |= set(duration_drift["test_name"].tolist())

    hist_fail: dict[str, float] = {}
    avg_duration: dict[str, float] = {}
    if not df_results.empty:
        for test_name, group in df_results.groupby("test_name"):
            hist_fail[test_name] = round((group["status"] == "FAIL").mean() * 100.0, 1)
            avg_duration[test_name] = round(float(group["duration_s"].mean()), 2)

    drift_lookup = (
        duration_drift.set_index("test_name")["drifted_duration_s"].to_dict()
        if not duration_drift.empty
        else {}
    )
    flake_lookup = flakiness.set_index("test_name").to_dict("index") if not flakiness.empty else {}

    rows = []
    for test_name in tests:
        flake = flake_lookup.get(test_name, {})
        prob = float(flake.get("failure_probability_pct", 0.0))
        predicted_duration = float(drift_lookup.get(test_name, avg_duration.get(test_name, 0.0)))
        failure_rate = float(flake.get("failure_rate_last20", hist_fail.get(test_name, 0.0)))
        duration_norm = min(predicted_duration / 60.0, 1.0)
        priority_score = round((prob * 0.55) + (failure_rate / 100.0 * 0.30) + (duration_norm * 0.15), 3)
        rows.append(
            {
                "test_name": test_name,
                "flakiness_probability": round(prob * 100.0, 1),
                "predicted_duration_s": round(predicted_duration, 2),
                "historical_failure_rate": failure_rate,
                "priority_score": priority_score,
                "recommendation": _prioritization_recommendation(prob, failure_rate, priority_score),
            }
        )

    out = pd.DataFrame(rows).sort_values("priority_score", ascending=False).head(top_n).reset_index(drop=True)
    out.insert(0, "priority_rank", np.arange(1, len(out) + 1))
    return out


def _prioritization_recommendation(prob: float, failure_rate: float, score: float) -> str:
    if prob >= 0.7 or score >= 0.65:
        return "Isolate"
    if prob >= 0.4 or failure_rate >= 30:
        return "Execute Early"
    if prob >= 0.25:
        return "Retry Automatically"
    return "Monitor"


def styled_badge(label: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:12px;'
        f'font-size:0.72rem;font-weight:600;background:{color}22;color:{color};'
        f'border:1px solid {color}55;">{label}</span>'
    )


def risk_badge(risk: str) -> str:
    mapped = {"High": "High Risk", "Medium": "Medium Risk", "Moderate": "Medium Risk", "Low": "Low Risk"}
    color = RISK_COLORS.get(risk if risk != "Medium" else "Moderate", "#8B949E")
    if risk == "Medium":
        color = RISK_COLORS["Moderate"]
    return styled_badge(mapped.get(risk, risk), color)


def recommendation_badge(rec: str) -> str:
    return styled_badge(rec, RECOMMENDATION_COLORS.get(rec, "#8B949E"))


def workflow_steps_markdown(steps: list[str]) -> str:
    return " → ".join(f"**{step}**" for step in steps)


def run_health_chart(health: pd.DataFrame, palette: dict[str, str]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=health["run_number"],
            y=health["pass_rate_pct"],
            mode="lines+markers",
            name="Pass rate",
            line=dict(color=palette["blue"], width=2),
            marker=dict(size=5, color=palette["blue"]),
            hovertemplate="Run %{x}<br>Pass rate: %{y:.1f}%<extra></extra>",
        )
    )

    z_only = health[health["zscore_anomaly"] & ~health["if_anomaly"]]
    if_only = health[health["if_anomaly"] & ~health["zscore_anomaly"]]
    both = health[health["flagged_by_both"]]

    if not z_only.empty:
        fig.add_trace(
            go.Scatter(
                x=z_only["run_number"],
                y=z_only["pass_rate_pct"],
                mode="markers",
                name="Z-score anomaly",
                marker=dict(color=palette["amber"], size=11, symbol="diamond"),
                hovertemplate="Run %{x}<br>%{y:.1f}%<extra></extra>",
            )
        )
    if not if_only.empty:
        fig.add_trace(
            go.Scatter(
                x=if_only["run_number"],
                y=if_only["pass_rate_pct"],
                mode="markers",
                name="Isolation Forest",
                marker=dict(color=palette["purple"], size=11, symbol="square"),
                hovertemplate="Run %{x}<br>%{y:.1f}%<extra></extra>",
            )
        )
    if not both.empty:
        fig.add_trace(
            go.Scatter(
                x=both["run_number"],
                y=both["pass_rate_pct"],
                mode="markers",
                name="Both methods",
                marker=dict(color=palette["red"], size=13, symbol="x"),
                hovertemplate="Run %{x}<br>%{y:.1f}%<extra></extra>",
            )
        )

    fig.update_layout(
        xaxis_title="Run Number",
        yaxis_title="Pass Rate (%)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return fig
