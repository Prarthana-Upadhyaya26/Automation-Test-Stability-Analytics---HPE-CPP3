"""
Phase 2 Data Ingestion Pipeline

Parses Robot Framework output.xml files from Phase 1 and loads them into
a structured SQLite database for analysis.

"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
import xml.etree.ElementTree as ET
from pathlib import Path

# CONFIGURATION

DEFAULT_CONFIG = {
    "runs_dir": "./runs",
    "database_path": "./analytics.db",
    "schema_path": "./schema.sql",
    "batch_size": 50,  # Commit every N runs
}

# DATABASE INITIALIZATION

def create_database(db_path, schema_path):
    """
    Create database and apply schema.
    
    Args:
        db_path: Path to SQLite database file
        schema_path: Path to schema.sql file
    
    Returns:
        sqlite3.Connection: Database connection
    """
    # Check if schema file exists
    if not os.path.exists(schema_path):
        print(f"✗ Schema file not found: {schema_path}")
        print("  Please ensure schema.sql is in the current directory")
        sys.exit(1)
    
    # Read schema
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema_sql = f.read()
    
    # Create/connect to database
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")  # Enable foreign key constraints
    
    # Apply schema
    try:
        conn.executescript(schema_sql)
        conn.commit()
        print(f"✓ Database schema applied: {db_path}")
    except sqlite3.Error as e:
        print(f"✗ Error applying schema: {e}")
        sys.exit(1)
    
    return conn


# XML PARSING

def parse_rf_timestamp(ts_str):
    """
    Parse Robot Framework timestamp string to datetime.
    
    Format: YYYYMMDD HH:MM:SS.mmm
    Example: 20241001 14:23:45.123
    
    Args:
        ts_str: Timestamp string from Robot Framework XML
    
    Returns:
        datetime: Parsed datetime object
    """
    try:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S.%f")
    except ValueError:
        # Fallback for timestamps without milliseconds
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S")


def calculate_duration(start_str, end_str):
    """
    Calculate duration between two Robot Framework timestamps.
    
    Args:
        start_str: Start timestamp string
        end_str: End timestamp string
    
    Returns:
        float: Duration in seconds
    """
    start_dt = parse_rf_timestamp(start_str)
    end_dt = parse_rf_timestamp(end_str)
    return (end_dt - start_dt).total_seconds()


def extract_failure_info(test_el):
    """
    Extract failure information from a failed test element.
    
    Args:
        test_el: ElementTree.Element for <test>
    
    Returns:
        dict: {category, message, keyword_name} or None if test passed
    """
    # Find failure message
    fail_msg_el = test_el.find(".//msg[@level='FAIL']")
    if fail_msg_el is None:
        return None
    
    message = fail_msg_el.text or ""
    
    # Categorize failure by message pattern
    if "still visible after" in message and "timeout" in message:
        category = "timeout"
    elif "not found after" in message and "retries" in message:
        category = "element"
    elif "Expected HTTP status" in message:
        category = "assertion"
    elif "CSV export contained" in message and "rows" in message:
        category = "data"
    elif "environment" in message.lower() or "unreachable" in message.lower():
        category = "environment"
    else:
        # Default to element if can't categorize
        category = "element"
    
    # Find the keyword that failed
    kw_el = None
    for kw in test_el.findall(".//kw"):
        status = kw.find("status")
        if status is not None and status.get("status") == "FAIL":
            kw_el = kw
        break    
    keyword_name = kw_el.get("name") if kw_el is not None else None
    
    return {
        "category": category,
        "message": message,
        "keyword_name": keyword_name
    }


def get_test_category_from_config(test_name):
    """
    Get test category from config based on test name.
    
    This maps test names to their designed categories from config.py.
    Used to populate test_results.category field.
    
    Args:
        test_name: Name of the test
    
    Returns:
        tuple: (category, fail_probability)
    """
    # Map from config.py - Design Question 1
    test_categories = {
        # Stable tests (12)
        "TC_Login_ValidCredentials": ("stable", 0.00),
        "TC_Login_InvalidPassword": ("stable", 0.00),
        "TC_Login_SessionTimeout": ("stable", 0.00),
        "TC_Login_AccountLockout": ("stable", 0.00),
        "TC_Dashboard_FilterByDate": ("stable", 0.00),
        "TC_Dashboard_Pagination": ("stable", 0.00),
        "TC_Dashboard_ExportChart": ("stable", 0.00),
        "TC_Dashboard_SearchBar": ("stable", 0.00),
        "TC_User_CreateAccount": ("stable", 0.00),
        "TC_User_EditProfile": ("stable", 0.00),
        "TC_User_DeleteAccount": ("stable", 0.00),
        "TC_User_PasswordReset": ("stable", 0.00),
        
        # Flaky-mild (2)
        "TC_Login_MFAVerification": ("flaky-mild", 0.30),
        "TC_Login_SSORedirect": ("flaky-mild", 0.35),
        
        # Flaky-moderate (2)
        "TC_Dashboard_LoadWidget": ("flaky-moderate", 0.50),
        "TC_Dashboard_RefreshData": ("flaky-moderate", 0.55),
        
        # Flaky-heavy (1)
        "TC_User_BulkImport": ("flaky-heavy", 0.65),
        
        # Consistently-failing (3)
        "TC_User_RoleAssignment": ("consistently_failing", 0.80),
        "TC_User_BatchExport": ("consistently_failing", 0.75),
        "TC_Login_OAuthCallback": ("consistently_failing", 0.70),
    }
    
    return test_categories.get(test_name, ("unknown", None))


def parse_run(run_folder, run_id):
    """
    Parse a single run folder and extract all data.
    
    Args:
        run_folder: Path to run folder (e.g., ./runs/TeamAlpha_build_001)
        run_id: Run number (1-100)
    
    Returns:
        dict: {
            'run': {...},          # Run metadata
            'tests': [...],        # List of test records
            'failures': [...],     # List of failure records
            'tags': [...]          # List of tag records
        }
    """
    # Parse metadata JSON
    meta_path = os.path.join(run_folder, "ci_metadata.json")
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    
    # Parse XML
    xml_path = os.path.join(run_folder, "output.xml")
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    # Extract run metadata
    run_data = {
        'run_id': run_id,
        'build_number': meta['build_no'],
        'timestamp': meta['timestamp'],
        'total_tests': meta['total'],
        'passed': meta['passed'],
        'failed': meta['failed'],
        'pass_rate': meta['pass_rate_pct'],
        'environment': meta['environment'],
        'executor': meta['executor']
    }
    
    # Extract test data
    tests = []
    failures = []
    tags = []
    
    for test_el in root.findall(".//test"):
        test_name = test_el.get("name")
        status_el = test_el.find("status")
        
        if status_el is None:
            continue
        
        status = status_el.get("status")
        start_time = status_el.get("starttime")
        end_time = status_el.get("endtime")
        duration = calculate_duration(start_time, end_time)
        
        # Get test category and fail probability from design
        category, fail_prob = get_test_category_from_config(test_name)
        
        # Extract feature and priority tags
        tag_els = test_el.findall("tag")
        feature = None
        priority = None
        
        for tag_el in tag_els:
            tag_text = tag_el.text or ""
            if tag_text.startswith("feature_"):
                feature = tag_text
            elif tag_text.startswith("priority_"):
                priority = tag_text
        
        # Create test record
        test_record = {
            'run_id': run_id,
            'test_name': test_name,
            'status': status,
            'duration': duration,
            'start_time': start_time,
            'end_time': end_time,
            'feature': feature or "unknown",
            'priority': priority or "unknown",
            'category': category,
            'fail_probability': fail_prob
        }
        tests.append(test_record)
        
        # Extract tags for tags table
        for tag_el in tag_els:
            tags.append({
                'test_name': test_name,  # Will be replaced with test_id after insert
                'tag_name': tag_el.text or ""
            })
        
        # Extract failure information if test failed
        if status == "FAIL":
            failure_info = extract_failure_info(test_el)
            if failure_info:
                failures.append({
                    'test_name': test_name,  # Will be replaced with test_id after insert
                    'category': failure_info['category'],
                    'message': failure_info['message'],
                    'keyword_name': failure_info['keyword_name']
                })
    
    return {
        'run': run_data,
        'tests': tests,
        'failures': failures,
        'tags': tags
    }


# DATABASE LOADING

def load_run_data(conn, run_data):
    """
    Load parsed run data into database.
    
    Uses transactions for atomicity and batch inserts for performance.
    
    Args:
        conn: SQLite database connection
        run_data: Parsed run data from parse_run()
    
    Returns:
        dict: {tests_inserted, failures_inserted, tags_inserted}
    """
    cursor = conn.cursor()
    
    try:
        # Insert run metadata
        cursor.execute("""
            INSERT INTO runs (run_id, build_number, timestamp, total_tests, 
                            passed, failed, pass_rate, environment, executor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_data['run']['run_id'],
            run_data['run']['build_number'],
            run_data['run']['timestamp'],
            run_data['run']['total_tests'],
            run_data['run']['passed'],
            run_data['run']['failed'],
            run_data['run']['pass_rate'],
            run_data['run']['environment'],
            run_data['run']['executor']
        ))
        
        # Insert tests and get test_id mapping
        test_id_map = {}  # test_name -> test_id
        
        for test in run_data['tests']:
            cursor.execute("""
                INSERT INTO tests (run_id, test_name, status, duration, 
                                 start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                test['run_id'],
                test['test_name'],
                test['status'],
                test['duration'],
                test['start_time'],
                test['end_time']
            ))
            
            test_id = cursor.lastrowid
            test_id_map[test['test_name']] = test_id
            
            # Insert test_results (one-to-one with tests)
            cursor.execute("""
                INSERT INTO test_results (test_id, feature, priority, category, 
                                        fail_probability)
                VALUES (?, ?, ?, ?, ?)
            """, (
                test_id,
                test['feature'],
                test['priority'],
                test['category'],
                test['fail_probability']
            ))
        
        # Insert failures
        failures_inserted = 0
        for failure in run_data['failures']:
            test_id = test_id_map.get(failure['test_name'])
            if test_id:
                cursor.execute("""
                    INSERT INTO failures (test_id, category, message, keyword_name)
                    VALUES (?, ?, ?, ?)
                """, (
                    test_id,
                    failure['category'],
                    failure['message'],
                    failure['keyword_name']
                ))
                failures_inserted += 1
        
        # Insert tags
        tags_inserted = 0
        for tag in run_data['tags']:
            test_id = test_id_map.get(tag['test_name'])
            if test_id:
                cursor.execute("""
                    INSERT INTO tags (test_id, tag_name)
                    VALUES (?, ?)
                """, (test_id, tag['tag_name']))
                tags_inserted += 1
        
        return {
            'tests_inserted': len(run_data['tests']),
            'failures_inserted': failures_inserted,
            'tags_inserted': tags_inserted
        }
    
    except sqlite3.Error as e:
        conn.rollback()
        raise Exception(f"Database error loading run {run_data['run']['run_id']}: {e}")


# MAIN PIPELINE

def run_pipeline(config):
    """
    Main pipeline execution.
    
    Process:
      1. Validate input directory
      2. Create/connect to database
      3. Parse and load each run
      4. Commit and validate
    
    Args:
        config: Configuration dictionary
    
    Returns:
        dict: Pipeline statistics
    """
    runs_dir = config['runs_dir']
    db_path = config['database_path']
    schema_path = config['schema_path']
    batch_size = config['batch_size']
    
    print("="*70)
    print("PHASE 2 DATA INGESTION PIPELINE")
    print("="*70)
    print()
    
    # Validate input directory
    if not os.path.exists(runs_dir):
        print(f"✗ Input directory not found: {runs_dir}")
        sys.exit(1)
    
    # Get list of run folders
    folders = sorted([
        f for f in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, f)) and f.startswith("TeamAlpha_build_")
    ])
    
    if len(folders) == 0:
        print(f"✗ No run folders found in {runs_dir}")
        print("  Expected folders like: TeamAlpha_build_001")
        sys.exit(1)
    
    print(f"Input directory: {runs_dir}/")
    print(f"Found {len(folders)} run folders")
    print(f"Database: {db_path}")
    print()
    
    # Create database and apply schema
    conn = create_database(db_path, schema_path)
    print()
    
    # Process each run
    print(f"Processing {len(folders)} runs...")
    print()
    
    stats = {
        'runs_processed': 0,
        'tests_inserted': 0,
        'failures_inserted': 0,
        'tags_inserted': 0,
        'errors': 0
    }
    
    for i, folder in enumerate(folders, 1):
        run_id = int(folder.split("_")[-1])  # Extract run number from folder name
        folder_path = os.path.join(runs_dir, folder)
        
        try:
            # Parse run
            run_data = parse_run(folder_path, run_id)
            
            # Load into database
            result = load_run_data(conn, run_data)
            
            # Update statistics
            stats['runs_processed'] += 1
            stats['tests_inserted'] += result['tests_inserted']
            stats['failures_inserted'] += result['failures_inserted']
            stats['tags_inserted'] += result['tags_inserted']
            
            # Commit periodically for performance
            if i % batch_size == 0 or i == len(folders):
                conn.commit()
                print(f"  ✓ Processed {i:3d}/{len(folders)} runs  "
                      f"({stats['tests_inserted']:4d} tests, "
                      f"{stats['failures_inserted']:3d} failures)")
        
        except Exception as e:
            stats['errors'] += 1
            print(f"  ✗ Error processing {folder}: {e}")
            continue
    
    # Final commit
    conn.commit()
    print()
    
    # Print summary
    print("="*70)
    print("INGESTION COMPLETE")
    print("="*70)
    print()
    print(f"  Runs processed:      {stats['runs_processed']:4d}")
    print(f"  Tests inserted:      {stats['tests_inserted']:4d}")
    print(f"  Failures inserted:   {stats['failures_inserted']:4d}")
    print(f"  Tags inserted:       {stats['tags_inserted']:4d}")
    if stats['errors'] > 0:
        print(f"  Errors:              {stats['errors']:4d}")
    print()
    
    # Verify database
    print("Verifying database integrity...")
    cursor = conn.cursor()
    
    # Check row counts
    checks = [
        ("runs", stats['runs_processed']),
        ("tests", stats['tests_inserted']),
        ("failures", stats['failures_inserted']),
        ("tags", stats['tags_inserted'])
    ]
    
    all_good = True
    for table, expected in checks:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        actual = cursor.fetchone()[0]
        
        if actual == expected:
            print(f"  ✓ {table:12s}: {actual:4d} rows")
        else:
            print(f"  ✗ {table:12s}: {actual:4d} rows (expected {expected})")
            all_good = False
    
    print()
    
    if all_good:
        print("✓ Database verification passed")
        print()
    else:
        print("✗ Database verification failed")
        print("  Please check errors above and re-run pipeline")
    
    conn.close()
    return stats


# COMMAND LINE INTERFACE

def parse_args():
    """Parse command line arguments."""
    p = argparse.ArgumentParser(
        description="Phase 2 Data Ingestion Pipeline",
        epilog="Example: python pipeline.py --input ./runs --database ./analytics.db"
    )
    p.add_argument("--input", "--runs-dir", dest="runs_dir",
                   default=DEFAULT_CONFIG['runs_dir'],
                   help=f"Input directory with run folders (default: {DEFAULT_CONFIG['runs_dir']})")
    p.add_argument("--database", "--db", dest="database_path",
                   default=DEFAULT_CONFIG['database_path'],
                   help=f"Output database path (default: {DEFAULT_CONFIG['database_path']})")
    p.add_argument("--schema", dest="schema_path",
                   default=DEFAULT_CONFIG['schema_path'],
                   help=f"Schema SQL file (default: {DEFAULT_CONFIG['schema_path']})")
    p.add_argument("--batch-size", type=int, dest="batch_size",
                   default=DEFAULT_CONFIG['batch_size'],
                   help=f"Commit every N runs (default: {DEFAULT_CONFIG['batch_size']})")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    
    config = {
        'runs_dir': args.runs_dir,
        'database_path': args.database_path,
        'schema_path': args.schema_path,
        'batch_size': args.batch_size
    }
    
    try:
        stats = run_pipeline(config)
        sys.exit(0 if stats['errors'] == 0 else 1)
    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Pipeline failed: {e}")
        sys.exit(1)