from __future__ import annotations

import json
import re
import string
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import StandardScaler

WINDOW_SIZE = 20
MIN_HISTORY = 10
DRIFT_BASELINE_BUILDS = 15
DRIFT_SIGMA = 2.0
DRIFT_MIN_FLAGGED = 3
DRIFT_PCT_THRESHOLD = 15.0
N_CLUSTERS = 5
RANDOM_STATE = 42


def _coerce_timestamp(df: pd.DataFrame, timestamp_col: str = "run_timestamp") -> pd.DataFrame:
    out = df.copy()
    if timestamp_col not in out.columns:
        return out
    out[timestamp_col] = pd.to_datetime(out[timestamp_col], errors="coerce")
    return out


def _build_run_order(df_results: pd.DataFrame) -> pd.DataFrame:
    out = _coerce_timestamp(df_results).copy()
    if "build_num" not in out.columns:
        out["build_num"] = out["run_id"].astype(str).str.extract(r"(\d+)$").astype(float)
    out = out.sort_values(["test_name", "run_timestamp", "run_id", "build_num"], kind="mergesort")
    out["_run_num"] = out.groupby("test_name").cumcount() + 1
    return out


def _parse_tag(tags_json: Optional[str], position: int) -> Optional[str]:
    if not tags_json:
        return None
    try:
        tags = json.loads(tags_json)
        return tags[position] if len(tags) > position else None
    except (json.JSONDecodeError, TypeError, IndexError):
        return None


def _prepare_results(df_results: pd.DataFrame) -> pd.DataFrame:
    out = _build_run_order(df_results).copy()
    out["status_binary"] = (out["status"] == "FAIL").astype(int)
    if "tags" in out.columns:
        out["feature_tag"] = out["tags"].apply(lambda x: _parse_tag(x, 1))
        out["priority_tag"] = out["tags"].apply(lambda x: _parse_tag(x, 2))
    else:
        out["feature_tag"] = None
        out["priority_tag"] = None
    if "run_pass_rate" not in out.columns and "pass_rate_pct" in out.columns:
        out["run_pass_rate"] = out["pass_rate_pct"]
    return out


def _build_features_for_test(df_test: pd.DataFrame, window_size: int = WINDOW_SIZE, min_history: int = MIN_HISTORY) -> pd.DataFrame:
    df = df_test.reset_index(drop=True)
    rows = []
    for i in range(min_history, len(df) - 1):
        start = max(0, i - window_size + 1)
        past = df.iloc[start : i + 1].copy()
        status_seq = past["status_binary"].values
        flips = int(np.sum(np.abs(np.diff(status_seq)))) if len(status_seq) > 1 else 0

        consecutive_fails = 0
        for s in reversed(status_seq):
            if s == 1:
                consecutive_fails += 1
            else:
                break
        consecutive_passes = 0
        for s in reversed(status_seq):
            if s == 0:
                consecutive_passes += 1
            else:
                break

        pass_mask = past["status"] == "PASS"
        fail_mask = past["status"] == "FAIL"
        timestamps = pd.to_datetime(past["run_timestamp"], errors="coerce") if "run_timestamp" in past.columns else pd.Series(dtype="datetime64[ns]")
        if len(timestamps) >= 2 and pd.notna(timestamps.iloc[-1]) and pd.notna(timestamps.iloc[-2]):
            time_since_last = (timestamps.iloc[-1] - timestamps.iloc[-2]).total_seconds() / 86400.0
        else:
            time_since_last = np.nan

        rows.append(
            {
                "test_name": past.iloc[-1]["test_name"],
                "build_no_target": df.iloc[i + 1].get("build_no", df.iloc[i + 1]["_run_num"]),
                "fail_rate_last_5": past["status_binary"].tail(5).mean(),
                "fail_rate_last_10": past["status_binary"].tail(10).mean(),
                "fail_rate_last_20": past["status_binary"].tail(20).mean(),
                "flip_count_last_w": flips,
                "flip_rate_last_w": flips / max(len(status_seq) - 1, 1),
                "consecutive_failures": consecutive_fails,
                "consecutive_passes": consecutive_passes,
                "avg_dur_pass_5": past.loc[pass_mask, "duration_s"].tail(5).mean() if pass_mask.any() else np.nan,
                "avg_dur_fail_5": past.loc[fail_mask, "duration_s"].tail(5).mean() if fail_mask.any() else np.nan,
                "duration_last": past.iloc[-1]["duration_s"],
                "time_since_last": time_since_last,
                "run_pass_rate": past.iloc[-1].get("run_pass_rate", past.iloc[-1].get("pass_rate_pct", 0.0)),
                "feature_tag": past.iloc[-1].get("feature_tag"),
                "priority_tag": past.iloc[-1].get("priority_tag"),
                "target": df.iloc[i + 1]["status_binary"],
            }
        )
    return pd.DataFrame(rows)


def build_flakiness_feature_matrix(df_results: pd.DataFrame) -> pd.DataFrame:
    if df_results.empty:
        return pd.DataFrame()

    prepared = _prepare_results(df_results)
    frames = [_build_features_for_test(group) for _, group in prepared.groupby("test_name")]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()

    features = pd.concat(frames, ignore_index=True)
    return features.fillna(
        {
            "avg_dur_pass_5": features["avg_dur_pass_5"].median(),
            "avg_dur_fail_5": features["avg_dur_fail_5"].median(),
            "time_since_last": 1.0,
        }
    )


def _encode_feature_frame(df_features: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    exclude_cols = {"test_name", "build_no_target", "target"}
    feature_cols = [c for c in df_features.columns if c not in exclude_cols]
    encoded = pd.get_dummies(df_features[feature_cols], drop_first=True)
    return encoded, df_features["target"].astype(int), list(encoded.columns)


def _train_flakiness_model(df_features: pd.DataFrame) -> tuple[RandomForestClassifier, list[str]]:
    X, y, feature_names = _encode_feature_frame(df_features)
    if len(X) < 20 or y.nunique() < 2:
        raise ValueError("Insufficient flakiness training data")
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=10,
        class_weight="balanced",
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )
    model.fit(X, y)
    return model, feature_names


def _latest_feature_row(df_test: pd.DataFrame) -> Optional[pd.DataFrame]:
    prepared = _prepare_results(df_test)
    if len(prepared) < MIN_HISTORY + 1:
        return None
    latest = _build_features_for_test(prepared).tail(1)
    return latest if not latest.empty else None


def build_flakiness_predictions(df_results: pd.DataFrame, lookback: int = 8) -> pd.DataFrame:
    del lookback  # kept for API compatibility
    if df_results.empty:
        return pd.DataFrame(columns=["test_name", "failure_probability_pct", "failure_rate_last20", "flip_count", "trend", "risk"])

    feature_matrix = build_flakiness_feature_matrix(df_results)
    if feature_matrix.empty:
        return pd.DataFrame(columns=["test_name", "failure_probability_pct", "failure_rate_last20", "flip_count", "trend", "risk"])

    try:
        model, feature_names = _train_flakiness_model(feature_matrix)
    except ValueError:
        return pd.DataFrame(columns=["test_name", "failure_probability_pct", "failure_rate_last20", "flip_count", "trend", "risk"])

    prepared = _prepare_results(df_results)
    rows = []
    for test_name, group in prepared.groupby("test_name"):
        latest = _latest_feature_row(group)
        if latest is None:
            continue

        encoded = pd.get_dummies(latest.drop(columns=["target"], errors="ignore"), drop_first=True)
        for col in feature_names:
            if col not in encoded.columns:
                encoded[col] = 0.0
        encoded = encoded[feature_names]
        proba = model.predict_proba(encoded)
        prob = float(proba[0, 1]) if proba.shape[1] > 1 else float(group["status_binary"].mean())

        recent_fail_rate = group.tail(20)["status_binary"].mean() * 100.0
        flip_count = int(group["status"].ne(group["status"].shift()).sum())
        recent = group.tail(5)
        prior = group.iloc[-10:-5] if len(group) >= 10 else group.head(5)
        recent_rate = float(recent["status_binary"].mean())
        prior_rate = float(prior["status_binary"].mean()) if not prior.empty else recent_rate
        delta = recent_rate - prior_rate
        if delta >= 0.10:
            trend = "Increasing"
        elif delta <= -0.10:
            trend = "Decreasing"
        else:
            trend = "Stable"
        risk = "High" if prob >= 0.70 else "Moderate" if prob >= 0.40 else "Low"
        rows.append(
            {
                "test_name": test_name,
                "failure_probability_pct": round(prob, 3),
                "failure_rate_last20": round(recent_fail_rate, 1),
                "flip_count": flip_count,
                "trend": trend,
                "risk": risk,
            }
        )

    return pd.DataFrame(rows).sort_values(["failure_probability_pct", "flip_count"], ascending=[False, False]).reset_index(drop=True)


def build_feature_importance(df_results: pd.DataFrame, top_n: int = 15) -> pd.DataFrame:
    feature_matrix = build_flakiness_feature_matrix(df_results)
    if feature_matrix.empty:
        return pd.DataFrame(columns=["feature", "importance"])

    try:
        model, feature_names = _train_flakiness_model(feature_matrix)
    except ValueError:
        return pd.DataFrame(columns=["feature", "importance"])

    importances = model.feature_importances_
    order = np.argsort(importances)[::-1][:top_n]
    out = pd.DataFrame(
        {
            "feature": [feature_names[i] for i in order[::-1]],
            "importance": importances[order[::-1]],
        }
    )
    return out.reset_index(drop=True)


def _detect_duration_drift(
    df_test: pd.DataFrame,
    baseline_builds: int = DRIFT_BASELINE_BUILDS,
    sigma_flag: float = DRIFT_SIGMA,
    min_recent_flagged: int = DRIFT_MIN_FLAGGED,
) -> pd.DataFrame:
    df = df_test.sort_values("build_num").reset_index(drop=True).copy()
    pass_mask = df["status"] == "PASS"
    df_pass = df[pass_mask].copy()
    if len(df_pass) < baseline_builds + 5:
        df["drift_confirmed"] = False
        return df

    baseline = df_pass.head(baseline_builds)
    baseline_mean = float(baseline["duration_s"].mean())
    baseline_std = float(baseline["duration_s"].std(ddof=0))
    df_pass["z_score_dur"] = (df_pass["duration_s"] - baseline_mean) / max(baseline_std, 0.01)
    df_pass["drift_flagged"] = df_pass["z_score_dur"] >= sigma_flag
    df_pass["drift_confirmed"] = (
        df_pass["drift_flagged"].rolling(window=10, min_periods=5).sum() >= min_recent_flagged
    )

    df["drift_confirmed"] = False
    df.loc[pass_mask, "drift_confirmed"] = df_pass["drift_confirmed"].values
    return df


def build_duration_drift_report(df_results: pd.DataFrame) -> pd.DataFrame:
    if df_results.empty:
        return pd.DataFrame(
            columns=[
                "test_name",
                "baseline_duration_s",
                "drifted_duration_s",
                "percent_increase",
                "first_drift_build",
                "model_confidence",
            ]
        )

    prepared = _prepare_results(df_results)
    rows = []
    for test_name, group in prepared.groupby("test_name"):
        result = _detect_duration_drift(group.copy())
        confirmed = result[result["drift_confirmed"]]
        if confirmed.empty:
            continue

        first_drift = int(confirmed["build_num"].min())
        baseline = result.loc[result["build_num"] < first_drift, "duration_s"]
        recent = result.loc[result["build_num"] >= first_drift, "duration_s"]
        baseline_median = float(baseline.median()) if not baseline.empty else float(result["duration_s"].median())
        recent_median = float(recent.median()) if not recent.empty else float(result["duration_s"].median())
        percent_increase = ((recent_median / baseline_median) - 1.0) * 100.0 if baseline_median else 0.0
        if percent_increase < DRIFT_PCT_THRESHOLD:
            continue

        latest_duration = float(recent.iloc[-1]) if not recent.empty else float(result["duration_s"].iloc[-1])
        rows.append(
            {
                "test_name": test_name,
                "baseline_duration_s": round(baseline_median, 2),
                "drifted_duration_s": round(latest_duration, 2),
                "percent_increase": round(percent_increase, 1),
                "first_drift_build": first_drift,
                "model_confidence": round(min(1.0, 0.5 + percent_increase / 200.0), 2),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "test_name",
                "baseline_duration_s",
                "drifted_duration_s",
                "percent_increase",
                "first_drift_build",
                "model_confidence",
            ]
        )
    return pd.DataFrame(rows).sort_values(["percent_increase", "model_confidence"], ascending=[False, False]).reset_index(drop=True)


def preprocess_message(msg: Optional[str]) -> str:
    if not isinstance(msg, str) or not msg.strip():
        return "_empty_"
    text = msg.lower().replace("—", " ").replace("–", " ")
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\b\d+\b", "NUM", text)
    text = re.sub(r"\b\w\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else "_empty_"


def _map_terms_to_category(terms: list[str]) -> str:
    joined = " ".join(terms)
    if any(w in joined for w in ("still visible", "visible", "timeout", "progressbar", "spinner")):
        return "timeout"
    if any(w in joined for w in ("not found", "locator", "retries", "widget", "submit", "modal", "nav")):
        return "element"
    if any(w in joined for w in ("expected http", "http status", "internal server", "bad request", "status num")):
        return "assertion"
    if any(w in joined for w in ("csv", "export", "rows", "records", "contained")):
        return "data"
    if any(w in joined for w in ("connection", "refused", "unreachable", "environment", "unable", "browser", "infrastructure")):
        return "environment"
    return "unknown"


def build_failure_cluster_report(df_results: pd.DataFrame) -> pd.DataFrame:
    failures = df_results[df_results["status"] == "FAIL"].copy()
    if failures.empty:
        return pd.DataFrame(columns=["run_id", "test_name", "failure_msg", "cluster_label", "cluster_category", "confidence"])

    failures["clean_msg"] = failures["failure_msg"].apply(preprocess_message)
    vectorizer = TfidfVectorizer(
        max_features=500,
        ngram_range=(1, 2),
        stop_words="english",
        sublinear_tf=True,
        min_df=2,
    )
    X = vectorizer.fit_transform(failures["clean_msg"])
    n_clusters = min(N_CLUSTERS, max(2, len(failures)))
    model = KMeans(n_clusters=n_clusters, random_state=RANDOM_STATE, n_init=10)
    labels = model.fit_predict(X)

    feature_names = vectorizer.get_feature_names_out()
    order_centroids = model.cluster_centers_.argsort()[:, ::-1]
    cluster_categories: dict[int, str] = {}
    for cluster_id in range(n_clusters):
        top_terms = [feature_names[j] for j in order_centroids[cluster_id, :10]]
        cluster_categories[cluster_id] = _map_terms_to_category(top_terms)

    X_dense = X.toarray()
    assigned_centroids = model.cluster_centers_[labels]
    distances = np.linalg.norm(X_dense - assigned_centroids, axis=1)
    dist_threshold = np.percentile(distances, 95) if len(distances) else 1.0
    confidences = np.clip(1.0 - np.minimum(distances / max(dist_threshold, 1e-6), 1.0), 0.0, 1.0)

    failures = failures.copy()
    failures["cluster_label"] = labels
    failures["cluster_category"] = failures["cluster_label"].map(cluster_categories)
    if "failure_kw" in failures.columns:
        env_mask = failures["failure_kw"] == "Environment_Setup"
        failures.loc[env_mask, "cluster_category"] = "environment"
        confidences[env_mask.values] = 1.0
    failures["confidence"] = np.round(confidences, 2)
    return failures[["run_id", "test_name", "failure_msg", "cluster_label", "cluster_category", "confidence"]].reset_index(drop=True)


def build_run_anomaly_report(df_runs: pd.DataFrame, df_results: pd.DataFrame) -> pd.DataFrame:
    if df_runs.empty:
        return pd.DataFrame(columns=["run_id", "pass_rate_pct", "is_anomalous", "anomaly_score", "failed_tests", "avg_duration_s", "zscore_anomaly", "if_anomaly"])

    runs = df_runs.sort_values("run_id").reset_index(drop=True).copy()
    runs["build_num"] = runs["run_id"].astype(str).str.extract(r"(\d+)$").astype(float)
    roll = runs["pass_rate_pct"].rolling(window=10, min_periods=3)
    runs["roll_mean"] = roll.mean().shift(1)
    runs["roll_std"] = roll.std().shift(1).fillna(5.0)
    runs["z_score"] = (runs["roll_mean"] - runs["pass_rate_pct"]) / runs["roll_std"].clip(lower=1.0)
    runs["zscore_anomaly"] = runs["z_score"] >= 2.0

    run_features = runs[["run_id", "pass_rate_pct", "total", "passed", "failed"]].copy()
    run_features = run_features.rename(columns={"total": "total_tests", "passed": "passed_tests", "failed": "failed_tests"})
    run_features["failed_tests"] = run_features["failed_tests"].fillna(0)
    run_features["pass_rate_change"] = run_features["pass_rate_pct"].diff().fillna(0.0)
    run_features["fail_rate_prev"] = (run_features["failed_tests"] / run_features["total_tests"].clip(lower=1)).shift(1).fillna(0.0)
    run_features["deviation"] = runs["roll_mean"].fillna(run_features["pass_rate_pct"]) - run_features["pass_rate_pct"]

    if not df_results.empty:
        duration_stats = (
            df_results.groupby("run_id")["duration_s"]
            .agg(["mean"])
            .reset_index()
            .rename(columns={"mean": "avg_duration_s"})
        )
        run_features = run_features.merge(duration_stats, on="run_id", how="left")
    run_features["avg_duration_s"] = run_features.get("avg_duration_s", pd.Series(0.0, index=run_features.index)).fillna(0.0)

    feature_cols = ["pass_rate_pct", "pass_rate_change", "deviation", "fail_rate_prev"]
    feature_df = run_features[feature_cols].fillna(0.0)
    if len(feature_df) < 10:
        runs["if_anomaly"] = False
        runs["anomaly_score"] = 0.0
        runs["is_anomalous"] = runs["zscore_anomaly"]
        runs["failed_tests"] = run_features["failed_tests"]
        runs["avg_duration_s"] = run_features["avg_duration_s"]
        return runs[
            ["run_id", "pass_rate_pct", "is_anomalous", "anomaly_score", "failed_tests", "avg_duration_s", "zscore_anomaly", "if_anomaly"]
        ].reset_index(drop=True)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(feature_df)
    iso = IsolationForest(n_estimators=200, contamination=0.05, random_state=RANDOM_STATE, n_jobs=-1)
    iso.fit(X_scaled)
    scores = iso.decision_function(X_scaled)
    if_labels = iso.predict(X_scaled) == -1

    runs["if_anomaly"] = False
    runs["anomaly_score"] = 0.0
    runs.loc[run_features.index, "if_anomaly"] = if_labels
    runs.loc[run_features.index, "anomaly_score"] = np.round(scores, 3)
    runs["is_anomalous"] = runs["zscore_anomaly"] | runs["if_anomaly"]
    runs["failed_tests"] = run_features["failed_tests"]
    runs["avg_duration_s"] = run_features["avg_duration_s"]
    return runs[
        ["run_id", "pass_rate_pct", "is_anomalous", "anomaly_score", "failed_tests", "avg_duration_s", "zscore_anomaly", "if_anomaly"]
    ].reset_index(drop=True)


def build_live_ml_summary(df_runs: pd.DataFrame, df_results: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "flakiness": build_flakiness_predictions(df_results),
        "duration_drift": build_duration_drift_report(df_results),
        "failure_clusters": build_failure_cluster_report(df_results),
        "anomalies": build_run_anomaly_report(df_runs, df_results),
    }
