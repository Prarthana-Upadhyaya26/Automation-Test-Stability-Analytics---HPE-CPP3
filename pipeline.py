import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

import pandas as pd


DEFAULT_CONFIG = {
    "runs_dir":      "./runs",
    "database_path": "./analytics.db",
    "schema_path":   "./schema.sql",
    "batch_size":    50,
    "force":         False,
}



def create_database(db_path: str, schema_path: str) -> sqlite3.Connection:
    """Open (or create) analytics.db and apply schema.sql."""
    if not os.path.exists(schema_path):
        print(f"✗ Schema file not found: {schema_path}")
        sys.exit(1)

    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    try:
        conn.executescript(schema_sql)
        conn.commit()
        print(f"✓ Database ready: {db_path}")
    except sqlite3.Error as exc:
        print(f"✗ Error applying schema: {exc}")
        conn.close()
        sys.exit(1)

    return conn


def parse_rf_timestamp(ts_str: str) -> datetime:
    """Parse a Robot Framework timestamp string."""
    try:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S.%f")
    except ValueError:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S")


def calculate_duration(start_str: str, end_str: str) -> float:
    """Return elapsed seconds between two RF timestamps.  0.0 on parse error."""
    try:
        return (parse_rf_timestamp(end_str) - parse_rf_timestamp(start_str)).total_seconds()
    except Exception:
        return 0.0


def parse_run(run_folder: str) -> dict:
    """Parse one run folder and return data shaped for schema.sql insertion."""
    meta_path = os.path.join(run_folder, "ci_metadata.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)

    pass_rate = meta.get("pass_rate_pct") or meta.get("pass_rate")
    if pass_rate is None:
        raise ValueError(
            f"ci_metadata.json in {run_folder} has neither 'pass_rate_pct' "
            f"nor 'pass_rate'. Keys found: {list(meta.keys())}"
        )

    run_id = os.path.basename(run_folder)

    run_duration_s = None

    run_row = {
        "run_id":       run_id,
        "team":         meta.get("team",        "TeamAlpha"),
        "suite_name":   meta.get("suite",       "Suite_Regression_TeamAlpha"),
        "job_name":     meta.get("job_name",    None),
        "build_no":     int(meta.get("build_no", 0)),
        "timestamp":    meta.get("timestamp",   ""),
        "total":        int(meta.get("total",   0)),
        "passed":       int(meta.get("passed",  0)),
        "failed":       int(meta.get("failed",  0)),
        "pass_rate_pct": float(pass_rate),
        "environment":  meta.get("environment", "staging"),
        "executor":     meta.get("executor",    "jenkins-agent-01"),
    }

    xml_path = os.path.join(run_folder, "output.xml")
    root = ET.parse(xml_path).getroot()

    suite_el = root.find("suite")
    if suite_el is not None:
        suite_status = suite_el.find("status")
        if suite_status is not None:
            run_duration_s = calculate_duration(
                suite_status.get("starttime", ""),
                suite_status.get("endtime",   ""),
            ) or None

    run_row["duration_s"] = run_duration_s

    results = []
    suite_name_xml = suite_el.get("name", run_row["suite_name"]) if suite_el is not None else run_row["suite_name"]

    for test_el in root.findall(".//test"):
        test_name = test_el.get("name", "")

        status_el = test_el.find("status")
        if status_el is None:
            continue

        status      = status_el.get("status", "FAIL")
        start_time  = status_el.get("starttime", "")
        end_time    = status_el.get("endtime",   "")
        duration_s  = calculate_duration(start_time, end_time)

        failure_msg = None
        failure_kw  = None
        if status == "FAIL":
            raw_msg = (status_el.text or "").strip()
            if not raw_msg:
                msg_el = test_el.find(".//msg[@level='FAIL']")
                raw_msg = (msg_el.text or "").strip() if msg_el is not None else ""
            failure_msg = raw_msg or None

            for kw_el in reversed(test_el.findall(".//kw")):
                kw_status = kw_el.find("status")
                if kw_status is not None and kw_status.get("status") == "FAIL":
                    failure_kw = kw_el.get("name")
                    break

        tag_list = [
            (tag_el.text or "").strip()
            for tag_el in test_el.findall("tag")
            if (tag_el.text or "").strip()
        ]
        tags_json = json.dumps(tag_list)

        result_id = f"{run_id}_{test_name}"

        results.append({
            "result_id":   result_id,
            "run_id":      run_id,
            "suite_name":  suite_name_xml,
            "test_name":   test_name,
            "status":      status,
            "duration_s":  round(duration_s, 3),
            "failure_msg": failure_msg,
            "failure_kw":  failure_kw,
            "tags":        tags_json,
        })

    return {"run": run_row, "results": results}


def load_run_data(conn: sqlite3.Connection, run_data: dict) -> dict:
    """Insert one run's data into runs + test_results inside a single transaction."""
    cursor = conn.cursor()
    try:
        run = run_data["run"]

        cursor.execute(
            """
            INSERT INTO runs
                (run_id, team, suite_name, job_name, build_no, timestamp,
                 duration_s, total, passed, failed, pass_rate_pct,
                 environment, executor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["run_id"],
                run["team"],
                run["suite_name"],
                run["job_name"],
                run["build_no"],
                run["timestamp"],
                run["duration_s"],
                run["total"],
                run["passed"],
                run["failed"],
                run["pass_rate_pct"],
                run["environment"],
                run["executor"],
            ),
        )

        for result in run_data["results"]:
            cursor.execute(
                """
                INSERT INTO test_results
                    (result_id, run_id, suite_name, test_name, status,
                     duration_s, failure_msg, failure_kw, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["result_id"],
                    result["run_id"],
                    result["suite_name"],
                    result["test_name"],
                    result["status"],
                    result["duration_s"],
                    result["failure_msg"],
                    result["failure_kw"],
                    result["tags"],
                ),
            )

        return {"results_inserted": len(run_data["results"])}

    except sqlite3.Error as exc:
        conn.rollback()
        raise RuntimeError(
            f"DB error loading run {run_data['run']['run_id']}: {exc}"
        ) from exc


def is_already_ingested(conn: sqlite3.Connection, run_id: str) -> bool:
    """Return True if run_id already has a success row in ingestion_log."""
    row = conn.execute(
        "SELECT 1 FROM ingestion_log WHERE run_id = ? AND status = 'success'",
        (run_id,),
    ).fetchone()
    return row is not None


def log_success(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingestion_log (run_id, ingested_at, status, error_msg)
        VALUES (?, datetime('now'), 'success', NULL)
        """,
        (run_id,),
    )


def log_error(conn: sqlite3.Connection, run_id: str, error_msg: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingestion_log (run_id, ingested_at, status, error_msg)
        VALUES (?, datetime('now'), 'error', ?)
        """,
        (run_id, error_msg[:2000]),
    )
    conn.commit()


def run_pipeline(config: dict) -> dict:
    """Full ingestion pipeline."""
    runs_dir    = config["runs_dir"]
    db_path     = config["database_path"]
    schema_path = config["schema_path"]
    batch_size  = config["batch_size"]
    force       = config.get("force", False)

    print("=" * 70)
    print("  Phase 2 Ingestion Pipeline  (schema.sql-aligned)")
    print("=" * 70)
    print()

    if not os.path.exists(runs_dir):
        print(f"✗ Runs directory not found: {runs_dir}")
        sys.exit(1)

    folders = sorted(
        f for f in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, f))
        and any(f.startswith(f"{prog}_build_") for prog in ["alpha", "beta", "gamma"])
    )

    if not folders:
        print(f"✗ No program_build_* folders found in {runs_dir}")
        sys.exit(1)

    print(f"  Input directory  : {runs_dir}/")
    print(f"  Run folders      : {len(folders)}")
    print(f"  Database         : {db_path}")
    print(f"  Schema           : {schema_path}")
    print(f"  Force re-ingest  : {'yes' if force else 'no'}")
    print()

    conn = create_database(db_path, schema_path)
    print()

    stats = {
        "runs_processed":    0,
        "results_inserted":  0,
        "runs_skipped":      0,
        "errors":            0,
    }

    print(f"Processing {len(folders)} folders…")
    print()

    for i, folder in enumerate(folders, 1):
        folder_path = os.path.join(runs_dir, folder)
        run_id      = folder

        if not force and is_already_ingested(conn, run_id):
            stats["runs_skipped"] += 1
            continue

        try:
            run_data = parse_run(folder_path)
        except Exception as exc:
            msg = str(exc)
            print(f"  ✗ Parse  — {folder}: {msg[:110]}")
            log_error(conn, run_id, f"parse: {msg}")
            stats["errors"] += 1
            continue

        try:
            result = load_run_data(conn, run_data)
        except Exception as exc:
            msg = str(exc)
            print(f"  ✗ Load   — {folder}: {msg[:110]}")
            log_error(conn, run_id, f"load: {msg}")
            stats["errors"] += 1
            continue

        log_success(conn, run_id)
        stats["runs_processed"]   += 1
        stats["results_inserted"] += result["results_inserted"]

        if stats["runs_processed"] % batch_size == 0 or i == len(folders):
            conn.commit()
            print(
                f"  ✓  {stats['runs_processed']:3d}/{len(folders)} runs  "
                f"| {stats['results_inserted']:5d} test_results"
            )

    conn.commit()
    print()

    print("=" * 70)
    print("INGESTION COMPLETE")
    print("=" * 70)
    print()
    print(f"  Runs processed   : {stats['runs_processed']:4d}")
    print(f"  Runs skipped     : {stats['runs_skipped']:4d}  (already ingested)")
    print(f"  Test rows stored : {stats['results_inserted']:4d}")
    if stats["errors"]:
        print(f"  ✗ Errors        : {stats['errors']:4d}  (see above)")
    print()

    print("Verifying row counts…")
    cursor.execute("SELECT COUNT(DISTINCT test_name) FROM test_results")
    tests_per_run = cursor.fetchone()[0] or 0
    total_expected_results = stats["runs_processed"] * tests_per_run

    cursor = conn.cursor()
    for table, expected, label in [
        ("runs",         stats["runs_processed"] + stats["runs_skipped"], "runs"),
        ("test_results", total_expected_results,                          "test_results (≈20 per run)"),
        ("ingestion_log", stats["runs_processed"] + stats["runs_skipped"], "ingestion_log"),
    ]:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        actual = cursor.fetchone()[0]
        ok = actual >= expected
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {table:<16}: {actual:5d} rows  (expected ≥{expected})  [{label}]")

    print()
    conn.close()
    return stats


#  OPTION B — MULTI-DATABASE MERGING
#  Open a separate sqlite3.connect() per database, detect schema variant,
#  query each independently, and merge into a single canonical pandas DataFrame.

def detect_schema_variant(conn: sqlite3.Connection) -> str:
    """Identify which schema variant a database uses."""
    tables = {
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "tests" in tables and "failures" in tables:
        return "schema_v1"
    return "schema_v2"


def _fetch_runs_v2(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch runs from schema_v2 (schema.sql) database."""
    return pd.read_sql_query(
        """
        SELECT
            run_id,
            team,
            suite_name,
            build_no,
            timestamp,
            COALESCE(duration_s, 0)   AS duration_s,
            total,
            passed,
            failed,
            pass_rate_pct,
            environment,
            executor
        FROM runs
        ORDER BY timestamp ASC
        """,
        conn,
    )


def _fetch_runs_v1(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch runs from schema_v1 (extended pipeline.py schema)."""
    df = pd.read_sql_query(
        """
        SELECT
            CAST(run_id AS TEXT)          AS run_id,
            'unknown'                     AS team,
            'unknown'                     AS suite_name,
            build_number                  AS build_no,
            timestamp,
            0.0                           AS duration_s,
            total_tests                   AS total,
            passed,
            failed,
            pass_rate                     AS pass_rate_pct,
            environment,
            executor
        FROM runs
        ORDER BY timestamp ASC
        """,
        conn,
    )
    return df


def _fetch_test_results_v2(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch per-test data from schema_v2 database."""
    return pd.read_sql_query(
        """
        SELECT
            tr.result_id,
            tr.run_id,
            r.team,
            r.suite_name,
            r.timestamp                   AS run_timestamp,
            r.pass_rate_pct               AS run_pass_rate,
            tr.test_name,
            tr.status,
            tr.duration_s,
            tr.failure_msg,
            tr.failure_kw,
            tr.tags
        FROM test_results tr
        JOIN runs r ON tr.run_id = r.run_id
        ORDER BY r.timestamp ASC, tr.test_name ASC
        """,
        conn,
    )


def _fetch_test_results_v1(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch per-test data from schema_v1 (pipeline.py extended schema)."""
    return pd.read_sql_query(
        """
        SELECT
            CAST(t.run_id AS TEXT) || '_' || t.test_name  AS result_id,
            CAST(t.run_id AS TEXT)        AS run_id,
            'unknown'                     AS team,
            'unknown'                     AS suite_name,
            r.timestamp                   AS run_timestamp,
            r.pass_rate                   AS run_pass_rate,
            t.test_name,
            t.status,
            t.duration                    AS duration_s,
            COALESCE(f.message, NULL)     AS failure_msg,
            COALESCE(f.keyword_name, NULL) AS failure_kw,
            NULL                          AS tags
        FROM tests t
        JOIN runs r         ON t.run_id  = r.run_id
        JOIN test_results tr ON t.test_id = tr.test_id
        LEFT JOIN failures f ON t.test_id = f.test_id
        ORDER BY r.timestamp ASC, t.test_name ASC
        """,
        conn,
    )


def open_connections(db_paths: list[str]) -> list[tuple[str, sqlite3.Connection, str]]:
    """Open a separate connection for each database path."""
    connections = []
    for path in db_paths:
        if not Path(path).exists():
            print(f"⚠  Database not found, skipping: {path}")
            continue
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        variant = detect_schema_variant(conn)
        print(f"  ✓  Opened {path}  [{variant}]")
        connections.append((path, conn, variant))
    return connections


def load_multi_db(db_paths: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Open each database independently, fetch data using the correct schema."""
    all_runs    = []
    all_results = []

    connections = open_connections(db_paths)

    for db_path, conn, variant in connections:
        label = Path(db_path).stem

        try:
            if variant == "schema_v2":
                df_r  = _fetch_runs_v2(conn)
                df_tr = _fetch_test_results_v2(conn)
            else:
                df_r  = _fetch_runs_v1(conn)
                df_tr = _fetch_test_results_v1(conn)

            df_r["_source_db"]  = label
            df_tr["_source_db"] = label

            all_runs.append(df_r)
            all_results.append(df_tr)
            print(f"  Loaded {label}: {len(df_r)} runs, {len(df_tr)} test results")

        except Exception as exc:
            print(f"  ✗ Error reading {db_path}: {exc}")


    if not all_runs:
        return pd.DataFrame(), pd.DataFrame()

    df_runs    = pd.concat(all_runs,    ignore_index=True)
    df_results = pd.concat(all_results, ignore_index=True)

    df_runs    = df_runs.drop_duplicates(subset=["run_id", "_source_db"])
    df_results = df_results.drop_duplicates(subset=["result_id", "_source_db"])

    df_runs["pass_rate_pct"]    = pd.to_numeric(df_runs["pass_rate_pct"],    errors="coerce")
    df_results["run_pass_rate"] = pd.to_numeric(df_results["run_pass_rate"], errors="coerce")

    return df_runs, df_results


def close_connections(connections: list[tuple[str, sqlite3.Connection, str]]) -> None:
    """Close all open connections returned by open_connections()."""
    for _, conn, _ in connections:
        try:
            conn.close()
        except Exception:
            pass


def get_runs_for_team(df_runs: pd.DataFrame, team: str) -> pd.DataFrame:
    """Filter merged runs DataFrame to a specific team."""
    return df_runs[df_runs["team"] == team].copy()


def get_flaky_scores(df_results: pd.DataFrame) -> pd.DataFrame:
    """Compute per-test flip count and failure rate from merged test results."""
    df = df_results.sort_values(["test_name", "run_timestamp"]).copy()
    df["prev_status"] = df.groupby(["test_name", "_source_db"])["status"].shift(1)

    result = (
        df.groupby(["test_name", "_source_db"])
        .agg(
            flip_count=("status",
                        lambda s: (s != s.shift(1)).sum() - 1),
            fail_count=("status",  lambda s: (s == "FAIL").sum()),
            total_runs=("status",  "count"),
        )
        .reset_index()
    )
    result["failure_rate"] = (result["fail_count"] / result["total_runs"] * 100).round(1)
    result["flip_count"]   = result["flip_count"].clip(lower=0)
    return result.sort_values("flip_count", ascending=False)


def get_heatmap_matrix(df_results: pd.DataFrame,
                       source_db: Optional[str] = None) -> pd.DataFrame:
    """Pivot test results into a matrix for the heatmap chart."""
    df = df_results.copy()
    if source_db:
        df = df[df["_source_db"] == source_db]

    df["pass_int"] = (df["status"] == "PASS").astype(int)

    run_order = (
        df[["run_id", "run_timestamp"]]
        .drop_duplicates()
        .sort_values("run_timestamp")["run_id"]
        .tolist()
    )

    pivot = df.pivot_table(
        index="test_name",
        columns="run_id",
        values="pass_int",
        aggfunc="first",
    )

    pivot = pivot.reindex(columns=[r for r in run_order if r in pivot.columns])

    pivot = pivot.sort_index()

    return pivot


def get_sankey_data(df_results: pd.DataFrame,
                    source_db: Optional[str] = None) -> dict:
    """Prepare node/link data for the Sankey failure-flow chart."""
    df = df_results.copy()
    if source_db:
        df = df[df["_source_db"] == source_db]

    failures = df[df["status"] == "FAIL"].copy()
    if failures.empty:
        return {"labels": [], "source": [], "target": [], "value": [], "colors": []}

    def classify(msg: str) -> str:
        if not msg:
            return "unknown"
        m = str(msg).lower()
        if "still visible after" in m:  return "timeout"
        if "not found after"     in m:  return "element"
        if "expected http"       in m:  return "assertion"
        if "csv export"          in m:  return "data"
        if "environment"         in m or "unreachable" in m: return "environment"
        return "unknown"

    failures["fail_cat"] = failures["failure_msg"].apply(classify)

    def run_phase(run_id: str) -> str:
        try:
            n = int(str(run_id).rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return "Phase 4"
        if n <= 25:   return "Phase 1 (1–25)"
        elif n <= 45: return "Phase 2 (26–45)"
        elif n <= 75: return "Phase 3 (46–75)"
        else:         return "Phase 4 (76–100)"

    failures["phase"] = failures["run_id"].apply(run_phase)

    cats   = sorted(failures["fail_cat"].unique())
    tests  = sorted(failures["test_name"].unique())
    phases = ["Phase 1 (1–25)", "Phase 2 (26–45)", "Phase 3 (46–75)", "Phase 4 (76–100)"]
    phases = [p for p in phases if p in failures["phase"].values]

    all_nodes = cats + tests + phases
    node_idx  = {name: i for i, name in enumerate(all_nodes)}

    ct_links = (
        failures.groupby(["fail_cat", "test_name"])
        .size()
        .reset_index(name="count")
    )
    tp_links = (
        failures.groupby(["test_name", "phase"])
        .size()
        .reset_index(name="count")
    )

    source, target, value = [], [], []

    for _, row in ct_links.iterrows():
        if row["fail_cat"] in node_idx and row["test_name"] in node_idx:
            source.append(node_idx[row["fail_cat"]])
            target.append(node_idx[row["test_name"]])
            value.append(int(row["count"]))

    for _, row in tp_links.iterrows():
        if row["test_name"] in node_idx and row["phase"] in node_idx:
            source.append(node_idx[row["test_name"]])
            target.append(node_idx[row["phase"]])
            value.append(int(row["count"]))

    CAT_COLORS  = {
        "timeout": "#D29922", "element": "#58A6FF",
        "assertion": "#BC8CFF", "data": "#FFA657",
        "environment": "#39D353", "unknown": "#8B949E",
    }
    PHASE_COLORS = {
        "Phase 1 (1–25)": "#58A6FF44", "Phase 2 (26–45)": "#D2992244",
        "Phase 3 (46–75)": "#3FB95044", "Phase 4 (76–100)": "#BC8CFF44",
    }
    TEST_COLOR  = "#1C2128"

    node_colors = []
    for name in all_nodes:
        if name in CAT_COLORS:
            node_colors.append(CAT_COLORS[name])
        elif name in PHASE_COLORS:
            node_colors.append(PHASE_COLORS[name])
        else:
            node_colors.append("#30363D")

    return {
        "labels":      all_nodes,
        "source":      source,
        "target":      target,
        "value":       value,
        "node_colors": node_colors,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 2 Ingestion Pipeline — schema.sql-aligned",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Single-DB ingestion:\n"
            "  python pipeline2.py\n"
            "  python pipeline2.py --runs-dir ./runs2 --db ./teambravo.db\n\n"
            "Multi-DB merge test (prints DataFrame info):\n"
            "  python pipeline2.py --db-list ./analytics.db ./teambravo.db\n"
        ),
    )
    p.add_argument("--runs-dir",  default=DEFAULT_CONFIG["runs_dir"])
    p.add_argument("--db",        default=DEFAULT_CONFIG["database_path"],
                   dest="database_path")
    p.add_argument("--schema",    default=DEFAULT_CONFIG["schema_path"],
                   dest="schema_path")
    p.add_argument("--batch-size", type=int, default=DEFAULT_CONFIG["batch_size"])
    p.add_argument("--force",     action="store_true", default=False)
    p.add_argument(
        "--db-list", nargs="+", metavar="DB",
        help="Test multi-DB merge: open each DB independently and print merged DataFrame info",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.db_list:
        print("=" * 70)
        print("  Option B — Multi-DB merge test")
        print("=" * 70)
        print()
        df_runs, df_results = load_multi_db(args.db_list)
        print()
        print(f"Merged runs    : {len(df_runs)} rows")
        print(f"Merged results : {len(df_results)} rows")
        if not df_runs.empty:
            print(f"Teams found    : {sorted(df_runs['team'].unique())}")
            print(f"DBs found      : {sorted(df_runs['_source_db'].unique())}")
            print(f"Columns        : {list(df_runs.columns)}")
        sys.exit(0)

    cfg = {
        "runs_dir":      args.runs_dir,
        "database_path": args.database_path,
        "schema_path":   args.schema_path,
        "batch_size":    args.batch_size,
        "force":         args.force,
    }
    result = run_pipeline(cfg)
    sys.exit(0 if result["errors"] == 0 else 1)