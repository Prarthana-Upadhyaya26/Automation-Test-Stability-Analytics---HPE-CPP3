"""
Phase 2 Data Ingestion Pipeline
================================

Parses every Robot Framework run folder produced by generate.py and loads the
data into analytics.db (Phase 2 schema).

What it populates
-----------------
  runs          — one row per CI run (from ci_metadata.json)
  test_results  — one row per test per run (from output.xml)
  ingestion_log — tracks which runs have been processed (idempotency)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

# CONFIGURATION

DEFAULT_CONFIG = {
    "runs_dir":      "./runs",
    "database_path": "./analytics.db",
    "schema_path":   "./schema.sql",
    "batch_size":    50,   # commit every N runs for performance
    "force":         False,
}

# DATABASE INITIALISATION

def create_database(db_path: str, schema_path: str) -> sqlite3.Connection:
    """
    Create (or open) analytics.db and apply schema.sql.

    Using CREATE TABLE IF NOT EXISTS throughout the schema makes this safe to
    call on an existing database — tables that are already present are left
    untouched and no data is lost.

    Returns
    -------
    sqlite3.Connection
        Open connection with foreign keys enabled.
    """
    if not os.path.exists(schema_path):
        print(f"✗ Schema file not found: {schema_path}")
        print("  Ensure schema.sql is in the current directory or pass --schema <path>")
        sys.exit(1)

    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        conn.executescript(schema_sql)
        conn.commit()
        print(f"✓ Database ready: {db_path}")
    except sqlite3.Error as exc:
        print(f"✗ Error applying schema: {exc}")
        conn.close()
        sys.exit(1)

    return conn


# TIMESTAMP HELPERS

def parse_rf_timestamp(ts_str: str) -> datetime:
    """
    Parse a Robot Framework timestamp string to a datetime object.

    RF format: ``YYYYMMDD HH:MM:SS.mmm``
    Example:   ``20241001 14:23:45.123``
    """
    try:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S.%f")
    except ValueError:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S")


def calculate_duration(start_str: str, end_str: str) -> float:
    """
    Return elapsed seconds between two RF timestamp strings.

    Returns 0.0 on any parse error so the row is still inserted.
    """
    try:
        return (parse_rf_timestamp(end_str) - parse_rf_timestamp(start_str)).total_seconds()
    except Exception:
        return 0.0


# RUN FOLDER PARSER

def parse_run(run_folder: str, folder_name: str) -> dict:
    """
    Parse one run folder and return all data ready for database insertion.

    Parameters
    ----------
    run_folder : str
        Path to a folder that contains ``output.xml`` and ``ci_metadata.json``.
    folder_name : str
        The folder name itself, used as run_id (e.g. "TeamAlpha_build_001").

    Returns
    -------
    dict
        ``{'run': {...}, 'tests': [...]}``
    """
    # ── metadata JSON ────────────────────────────────────────────────────────
    meta_path = os.path.join(run_folder, "ci_metadata.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)

    # Accept both key names produced by different generator versions
    pass_rate = meta.get("pass_rate_pct") if "pass_rate_pct" in meta else meta.get("pass_rate")
    if pass_rate is None:
        raise ValueError(
            f"ci_metadata.json in {run_folder} has neither 'pass_rate_pct' "
            f"nor 'pass_rate' key.  Available keys: {list(meta.keys())}"
        )

    run_data = {
        "run_id":       folder_name,
        "team":         meta.get("team", "TeamAlpha"),
        "suite_name":   meta.get("suite", meta.get("suite_name", "Suite_Regression")),
        "job_name":     meta.get("job_name"),
        "build_no":     meta.get("build_no"),
        "timestamp":    meta["timestamp"],
        "duration_s":   meta.get("duration_s"),
        "total":        meta["total"],
        "passed":       meta["passed"],
        "failed":       meta["failed"],
        "pass_rate_pct": pass_rate,
        "environment":  meta.get("environment", "staging"),
        "executor":     meta.get("executor", "jenkins-agent-01"),
    }

    # ── XML ──────────────────────────────────────────────────────────────────
    xml_path = os.path.join(run_folder, "output.xml")
    root = ET.parse(xml_path).getroot()

    # Derive suite name from the XML root suite element if available
    suite_el = root.find(".//suite")
    xml_suite_name = suite_el.get("name", run_data["suite_name"]) if suite_el is not None else run_data["suite_name"]

    tests: list[dict] = []

    for test_el in root.findall(".//test"):
        test_name = test_el.get("name", "")
        status_el = test_el.find("status")
        if status_el is None:
            continue

        status     = status_el.get("status", "FAIL")
        start_time = status_el.get("starttime", "")
        end_time   = status_el.get("endtime", "")
        duration_s = calculate_duration(start_time, end_time)

        # Collect all tags as a JSON array string
        tag_names: list[str] = []
        for tag_el in test_el.findall("tag"):
            tag_text = (tag_el.text or "").strip()
            if tag_text:
                tag_names.append(tag_text)
        tags_json = json.dumps(tag_names)

        # Extract failure info (NULL on PASS)
        failure_msg = None
        failure_kw  = None
        if status == "FAIL":
            failure_msg, failure_kw = _extract_failure_info(test_el)

        # Construct the composite primary key exactly as the spec requires:
        # run_id + "_" + test_name
        result_id = f"{folder_name}_{test_name}"

        tests.append({
            "result_id":   result_id,
            "run_id":      folder_name,
            "suite_name":  xml_suite_name,
            "test_name":   test_name,
            "status":      status,
            "duration_s":  duration_s,
            "failure_msg": failure_msg,
            "failure_kw":  failure_kw,
            "tags":        tags_json,
        })

    return {"run": run_data, "tests": tests}


def _extract_failure_info(test_el) -> tuple[str | None, str | None]:
    """
    Extract the failure message and failing keyword name from a <test> element.

    Returns
    -------
    tuple[str | None, str | None]
        (failure_msg, failure_kw) — both None if no failure message is found.
    """
    status_el = test_el.find("status")
    if status_el is None or status_el.get("status") != "FAIL":
        return None, None

    message = (status_el.text or "").strip()
    if not message:
        msg_el = test_el.find(".//msg[@level='FAIL']")
        message = (msg_el.text or "").strip() if msg_el is not None else ""

    if not message:
        return None, None

    # Walk innermost failing keyword
    keyword_name = None
    for kw_el in reversed(test_el.findall(".//kw")):
        kw_status = kw_el.find("status")
        if kw_status is not None and kw_status.get("status") == "FAIL":
            keyword_name = kw_el.get("name")
            break

    return message or None, keyword_name


# DATABASE LOADING (single run, inside a transaction)

def load_run_data(conn: sqlite3.Connection, run_data: dict) -> dict:
    """
    Insert one run's data into runs and test_results inside a single transaction.

    The transaction is NOT committed here; the caller controls commit
    frequency (batch commits every N runs for performance).

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    run_data : dict
        As returned by ``parse_run()``.

    Returns
    -------
    dict
        ``{'tests_inserted': int}``
    """
    cursor = conn.cursor()

    try:
        # ── runs ─────────────────────────────────────────────────────────────
        cursor.execute(
            """
            INSERT INTO runs
                (run_id, team, suite_name, job_name, build_no, timestamp,
                 duration_s, total, passed, failed, pass_rate_pct,
                 environment, executor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_data["run"]["run_id"],
                run_data["run"]["team"],
                run_data["run"]["suite_name"],
                run_data["run"]["job_name"],
                run_data["run"]["build_no"],
                run_data["run"]["timestamp"],
                run_data["run"]["duration_s"],
                run_data["run"]["total"],
                run_data["run"]["passed"],
                run_data["run"]["failed"],
                run_data["run"]["pass_rate_pct"],
                run_data["run"]["environment"],
                run_data["run"]["executor"],
            ),
        )

        # ── test_results ─────────────────────────────────────────────────────
        for test in run_data["tests"]:
            cursor.execute(
                """
                INSERT INTO test_results
                    (result_id, run_id, suite_name, test_name, status,
                     duration_s, failure_msg, failure_kw, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test["result_id"],
                    test["run_id"],
                    test["suite_name"],
                    test["test_name"],
                    test["status"],
                    test["duration_s"],
                    test["failure_msg"],
                    test["failure_kw"],
                    test["tags"],
                ),
            )

        return {"tests_inserted": len(run_data["tests"])}

    except sqlite3.Error as exc:
        conn.rollback()
        raise Exception(
            f"DB error while loading run {run_data['run']['run_id']}: {exc}"
        ) from exc


# INGESTION LOG HELPERS

def is_already_ingested(conn: sqlite3.Connection, run_id: str) -> bool:
    """Return True if run_id is in ingestion_log with status='success'."""
    row = conn.execute(
        "SELECT 1 FROM ingestion_log WHERE run_id = ? AND status = 'success'",
        (run_id,),
    ).fetchone()
    return row is not None


def log_ingestion_success(conn: sqlite3.Connection, run_id: str) -> None:
    """Record a successful ingestion in ingestion_log."""
    conn.execute(
        """
        INSERT OR REPLACE INTO ingestion_log (run_id, ingested_at, status, error_msg)
        VALUES (?, datetime('now'), 'success', NULL)
        """,
        (run_id,),
    )


def log_ingestion_error(conn: sqlite3.Connection, run_id: str, error_msg: str) -> None:
    """Record a failed ingestion in ingestion_log."""
    conn.execute(
        """
        INSERT OR REPLACE INTO ingestion_log (run_id, ingested_at, status, error_msg)
        VALUES (?, datetime('now'), 'error', ?)
        """,
        (run_id, error_msg[:2000]),  # cap length for display
    )
    conn.commit()  # error rows are committed immediately so they survive rollback


# MAIN PIPELINE

def run_pipeline(config: dict) -> dict:
    """
    Main pipeline execution:

    1. Validate input directory exists.
    2. Create / open database and apply schema.
    3. For each TeamAlpha_build_XXX folder (sorted):
       a. Skip if already in ingestion_log with status='success'.
       b. Parse output.xml + ci_metadata.json.
       c. Insert into runs / test_results.
       d. Write success row to ingestion_log.
       e. Commit every batch_size runs.
    4. Print summary statistics.
    5. Verify row counts match expectations.
    """
    runs_dir    = config["runs_dir"]
    db_path     = config["database_path"]
    schema_path = config["schema_path"]
    batch_size  = config["batch_size"]
    force       = config.get("force", False)

    print("=" * 70)
    print()

    # ── Validate input directory ──────────────────────────────────────────────
    if not os.path.exists(runs_dir):
        print(f"✗ Input directory not found: {runs_dir}")
        sys.exit(1)

    folders = sorted(
        f for f in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, f))
        and f.startswith("TeamAlpha_build_")
    )

    if not folders:
        print(f"✗ No TeamAlpha_build_XXX folders found in {runs_dir}")
        sys.exit(1)

    print(f"  Input directory : {runs_dir}/")
    print(f"  Run folders     : {len(folders)}")
    print(f"  Database        : {db_path}")
    print(f"  Force re-ingest : {'yes' if force else 'no'}")
    print()

    # ── Connect / apply schema ────────────────────────────────────────────────
    conn = create_database(db_path, schema_path)
    print()

    # ── Process folders ───────────────────────────────────────────────────────
    stats = {
        "runs_processed": 0,
        "tests_inserted": 0,
        "runs_skipped":   0,
        "errors":         0,
    }

    print(f"Processing {len(folders)} folders...")
    print()

    for i, folder in enumerate(folders, 1):
        # The folder name IS the run_id (e.g. "TeamAlpha_build_001")
        run_id      = folder
        folder_path = os.path.join(runs_dir, folder)

        # ── Idempotency check ─────────────────────────────────────────────────
        if not force and is_already_ingested(conn, run_id):
            stats["runs_skipped"] += 1
            continue

        # ── Parse ─────────────────────────────────────────────────────────────
        try:
            run_data = parse_run(folder_path, folder)
        except Exception as exc:
            msg = str(exc)
            print(f"  ✗ Parse error  — {folder}: {msg[:120]}")
            log_ingestion_error(conn, run_id, f"parse: {msg}")
            stats["errors"] += 1
            continue

        # ── Load ──────────────────────────────────────────────────────────────
        try:
            result = load_run_data(conn, run_data)
        except Exception as exc:
            msg = str(exc)
            print(f"  ✗ Load error   — {folder}: {msg[:120]}")
            log_ingestion_error(conn, run_id, f"load: {msg}")
            stats["errors"] += 1
            continue

        # ── Success ───────────────────────────────────────────────────────────
        log_ingestion_success(conn, run_id)

        stats["runs_processed"] += 1
        stats["tests_inserted"] += result["tests_inserted"]

        # Batch commit
        if stats["runs_processed"] % batch_size == 0 or i == len(folders):
            conn.commit()
            print(
                f"  ✓  {stats['runs_processed']:3d}/{len(folders)} runs  "
                f"| {stats['tests_inserted']:5d} tests"
            )

    conn.commit()
    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("=" * 70)
    print("INGESTION COMPLETE")
    print("=" * 70)
    print()
    print(f"  Runs processed  : {stats['runs_processed']:4d}")
    print(f"  Runs skipped    : {stats['runs_skipped']:4d}  (already ingested)")
    print(f"  Tests inserted  : {stats['tests_inserted']:4d}")
    if stats["errors"] > 0:
        print(f"  ✗ Errors        : {stats['errors']:4d}  (check output above)")
    print()

    # ── Row-count verification ────────────────────────────────────────────────
    print("Verifying database row counts...")
    checks = [
        ("runs",         stats["runs_processed"] + stats["runs_skipped"], True),
        ("test_results", stats["tests_inserted"],                         False),
    ]

    all_good = True
    cursor = conn.cursor()
    for table, expected, is_cumulative in checks:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        actual = cursor.fetchone()[0]
        note = " (cumulative total)" if is_cumulative else ""
        if actual == expected or (is_cumulative and actual >= expected):
            print(f"  ✓  {table:<14}: {actual:5d} rows{note}")
        else:
            print(f"  ✗  {table:<14}: {actual:5d} rows  (expected {expected}){note}")
            all_good = False

    print()
    if all_good:
        print("✓ Database verification passed")
    else:
        print("✗ Some row counts are unexpected — check the output above for errors")

    print()
    conn.close()
    return stats


# CLI

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Phase 2 — Data Ingestion Pipeline",
        epilog=(
            "Reads all TeamAlpha_build_XXX folders and loads them into analytics.db.\n"
            "The pipeline is idempotent: re-running it only processes new folders."
        ),
    )
    p.add_argument(
        "--runs-dir", default=DEFAULT_CONFIG["runs_dir"],
        help="Directory containing generated run folders (default: %(default)s)",
    )
    p.add_argument(
        "--db", "--database", dest="database_path",
        default=DEFAULT_CONFIG["database_path"],
        help="SQLite database path (default: %(default)s)",
    )
    p.add_argument(
        "--schema", dest="schema_path",
        default=DEFAULT_CONFIG["schema_path"],
        help="schema.sql path (default: %(default)s)",
    )
    p.add_argument(
        "--batch-size", type=int, default=DEFAULT_CONFIG["batch_size"],
        help="Commit every N runs (default: %(default)s)",
    )
    p.add_argument(
        "--force", action="store_true", default=False,
        help="Re-ingest all runs even if already in ingestion_log",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = {
        "runs_dir":      args.runs_dir,
        "database_path": args.database_path,
        "schema_path":   args.schema_path,
        "batch_size":    args.batch_size,
        "force":         args.force,
    }
    result = run_pipeline(cfg)
    sys.exit(0 if result["errors"] == 0 else 1)