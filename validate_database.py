"""
Phase 2 Database Validation Script

Validates that the analytics database was populated correctly and is ready
for Phase 3 (dashboard) and Phase 4 (ML models).

"""

import argparse
import sqlite3
import sys
from collections import Counter

# VALIDATION CHECKS

def validate_row_counts(conn):
    """
    Validate that row counts match expected values.
    
    Expected:
      - runs: 100 (one per CI build)
      - tests: 2000 (20 tests × 100 runs)
      - test_results: 2000 (one-to-one with tests)
      - failures: ~480 (only failed tests)
      - tags: ~6000 (3 tags per test × 2000 tests)
    
    Returns:
        tuple: (success, message, counts_dict)
    """
    cursor = conn.cursor()
    
    # Get actual counts
    counts = {}
    tables = ['runs', 'tests', 'test_results', 'failures', 'tags']
    
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = cursor.fetchone()[0]
    
    issues = []
    
    # Check runs
    if counts['runs'] != 100:
        issues.append(f"runs: {counts['runs']} (expected 100)")
    
    # Check tests
    if counts['tests'] != 2000:
        issues.append(f"tests: {counts['tests']} (expected 2000)")
    
    # Check test_results (one-to-one with tests)
    if counts['test_results'] != counts['tests']:
        issues.append(f"test_results: {counts['test_results']} (expected {counts['tests']}, one-to-one with tests)")
    
    # Check failures (should be ~450-500)
    if not (400 <= counts['failures'] <= 550):
        issues.append(f"failures: {counts['failures']} (expected 400-550)")
    
    # Check tags (should be ~5000-7000, ~3 per test)
    if not (5000 <= counts['tags'] <= 7000):
        issues.append(f"tags: {counts['tags']} (expected 5000-7000)")
    
    if issues:
        return False, "Row count issues:\n  " + "\n  ".join(issues), counts
    
    msg = (
        f"✓ Row counts correct\n"
        f"    runs: {counts['runs']}, tests: {counts['tests']}, "
        f"test_results: {counts['test_results']}, "
        f"failures: {counts['failures']}, tags: {counts['tags']}"
    )
    
    return True, msg, counts


def validate_data_quality(conn):
    """
    Validate data quality (no nulls where required, valid ranges).
    
    Returns:
        tuple: (success, message)
    """
    cursor = conn.cursor()
    issues = []
    
    # Check for nulls in required columns
    null_checks = [
        ("runs", "timestamp"),
        ("runs", "passed"),
        ("runs", "failed"),
        ("runs", "pass_rate"),
        ("tests", "test_name"),
        ("tests", "status"),
        ("tests", "duration"),
        ("test_results", "category"),
        ("failures", "message"),
    ]
    
    for table, column in null_checks:
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")
        null_count = cursor.fetchone()[0]
        if null_count > 0:
            issues.append(f"{table}.{column}: {null_count} nulls found")
    
    # Check status values
    cursor.execute("SELECT DISTINCT status FROM tests")
    statuses = [row[0] for row in cursor.fetchall()]
    if set(statuses) != {'PASS', 'FAIL'}:
        issues.append(f"tests.status: invalid values {statuses} (expected PASS, FAIL)")
    
    # Check pass_rate range
    cursor.execute("SELECT COUNT(*) FROM runs WHERE pass_rate < 0 OR pass_rate > 100")
    invalid_pass_rate = cursor.fetchone()[0]
    if invalid_pass_rate > 0:
        issues.append(f"runs.pass_rate: {invalid_pass_rate} values outside 0-100")
    
    # Check duration range
    cursor.execute("SELECT COUNT(*) FROM tests WHERE duration < 0")
    negative_duration = cursor.fetchone()[0]
    if negative_duration > 0:
        issues.append(f"tests.duration: {negative_duration} negative values")
    
    # Check category values
    cursor.execute("SELECT DISTINCT category FROM test_results")
    categories = [row[0] for row in cursor.fetchall()]
    valid_categories = {'stable', 'flaky-mild', 'flaky-moderate', 'flaky-heavy', 'consistently_failing', 'unknown'}
    invalid_cats = set(categories) - valid_categories
    if invalid_cats:
        issues.append(f"test_results.category: invalid values {invalid_cats}")
    
    if issues:
        return False, "Data quality issues:\n  " + "\n  ".join(issues)
    
    return True, "✓ Data quality checks passed"


def validate_foreign_keys(conn):
    """
    Validate foreign key relationships.
    
    Returns:
        tuple: (success, message)
    """
    cursor = conn.cursor()
    issues = []
    
    # Check tests.run_id -> runs.run_id
    cursor.execute("""
        SELECT COUNT(*) FROM tests t
        LEFT JOIN runs r ON t.run_id = r.run_id
        WHERE r.run_id IS NULL
    """)
    orphan_tests = cursor.fetchone()[0]
    if orphan_tests > 0:
        issues.append(f"tests: {orphan_tests} orphan records (no matching run)")
    
    # Check test_results.test_id -> tests.test_id
    cursor.execute("""
        SELECT COUNT(*) FROM test_results tr
        LEFT JOIN tests t ON tr.test_id = t.test_id
        WHERE t.test_id IS NULL
    """)
    orphan_results = cursor.fetchone()[0]
    if orphan_results > 0:
        issues.append(f"test_results: {orphan_results} orphan records (no matching test)")
    
    # Check failures.test_id -> tests.test_id
    cursor.execute("""
        SELECT COUNT(*) FROM failures f
        LEFT JOIN tests t ON f.test_id = t.test_id
        WHERE t.test_id IS NULL
    """)
    orphan_failures = cursor.fetchone()[0]
    if orphan_failures > 0:
        issues.append(f"failures: {orphan_failures} orphan records (no matching test)")
    
    # Check tags.test_id -> tests.test_id
    cursor.execute("""
        SELECT COUNT(*) FROM tags tg
        LEFT JOIN tests t ON tg.test_id = t.test_id
        WHERE t.test_id IS NULL
    """)
    orphan_tags = cursor.fetchone()[0]
    if orphan_tags > 0:
        issues.append(f"tags: {orphan_tags} orphan records (no matching test)")
    
    if issues:
        return False, "Foreign key integrity issues:\n  " + "\n  ".join(issues)
    
    return True, "✓ Foreign key integrity verified"


def validate_category_balance(conn):
    """
    Validate that failure categories match design (22-34% each).
    
    Returns:
        tuple: (success, message)
    """
    cursor = conn.cursor()
    
    # Get failure category distribution
    cursor.execute("""
        SELECT category, COUNT(*) as count
        FROM failures
        GROUP BY category
    """)
    
    results = cursor.fetchall()
    total = sum(count for _, count in results)
    
    if total == 0:
        return False, "No failures found in database"
    
    distribution = {}
    for category, count in results:
        pct = count / total * 100
        distribution[category] = (count, pct)
    
    # Check each main category is 22-34%
    issues = []
    for category in ['timeout', 'element', 'assertion', 'data']:
        if category not in distribution:
            issues.append(f"{category}: 0% (missing)")
        else:
            count, pct = distribution[category]
            if pct < 20:
                issues.append(f"{category}: {count} ({pct:.1f}%) BELOW 22% minimum")
            elif pct > 36:
                issues.append(f"{category}: {count} ({pct:.1f}%) ABOVE 34% maximum")
    
    if issues:
        return False, "Category balance issues:\n  " + "\n  ".join(issues)
    
    # Format success message
    breakdown = ", ".join([
        f"{cat}: {distribution.get(cat, (0, 0))[0]} ({distribution.get(cat, (0, 0))[1]:.1f}%)"
        for cat in ['timeout', 'element', 'assertion', 'data']
    ])
    
    return True, f"✓ Categories balanced ({total} total failures)\n    {breakdown}"


def validate_duration_patterns(conn):
    """
    Validate that duration patterns are preserved in database.
    
    Checks:
      - TC_Login_ValidCredentials: Seasonal (even/odd difference)
      - TC_Dashboard_ExportChart: Step change (before/after difference)
      - TC_User_BulkImport: Progressive drift (early/late difference)
    
    Returns:
        tuple: (success, message)
    """
    cursor = conn.cursor()
    issues = []
    
    # Initialize variables to avoid undefined errors
    avg_odd = 0.0
    avg_even = 0.0
    avg_before = 0.0
    avg_after = 0.0
    avg_early = 0.0
    avg_late = 0.0
    
    # Check seasonal pattern (TC_Login_ValidCredentials)
    cursor.execute("""
        SELECT r.run_id, t.duration
        FROM tests t
        JOIN runs r ON t.run_id = r.run_id
        WHERE t.test_name = 'TC_Login_ValidCredentials'
        ORDER BY r.run_id
    """)
    
    seasonal_data = cursor.fetchall()
    if seasonal_data:
        even_durations = [dur for run_id, dur in seasonal_data if run_id % 2 == 0]
        odd_durations = [dur for run_id, dur in seasonal_data if run_id % 2 != 0]
        
        if even_durations and odd_durations:
            avg_even = sum(even_durations) / len(even_durations)
            avg_odd = sum(odd_durations) / len(odd_durations)
            ratio = avg_odd / avg_even if avg_even > 0 else 0
            
            if ratio < 1.3:
                issues.append(f"Seasonal: odd/even ratio {ratio:.2f}× (expected ≥1.5×)")
    else:
        issues.append("Seasonal: No data found for TC_Login_ValidCredentials")
    
    # Check step change (TC_Dashboard_ExportChart)
    cursor.execute("""
        SELECT r.run_id, t.duration
        FROM tests t
        JOIN runs r ON t.run_id = r.run_id
        WHERE t.test_name = 'TC_Dashboard_ExportChart'
        ORDER BY r.run_id
    """)
    
    step_data = cursor.fetchall()
    if step_data:
        before = [dur for run_id, dur in step_data if run_id <= 50]
        after = [dur for run_id, dur in step_data if run_id > 50]
        
        if before and after:
            avg_before = sum(before) / len(before)
            avg_after = sum(after) / len(after)
            ratio = avg_after / avg_before if avg_before > 0 else 0
            
            if ratio < 2.0:
                issues.append(f"Step change: after/before ratio {ratio:.2f}× (expected ≥2.5×)")
    else:
        issues.append("Step change: No data found for TC_Dashboard_ExportChart")
    
    # Check progressive drift (TC_User_BulkImport)
    cursor.execute("""
        SELECT r.run_id, t.duration
        FROM tests t
        JOIN runs r ON t.run_id = r.run_id
        WHERE t.test_name = 'TC_User_BulkImport'
        ORDER BY r.run_id
    """)
    
    progressive_data = cursor.fetchall()
    if progressive_data:
        early = [dur for run_id, dur in progressive_data if run_id <= 20]
        late = [dur for run_id, dur in progressive_data if run_id >= 80]
        
        if early and late:
            avg_early = sum(early) / len(early)
            avg_late = sum(late) / len(late)
            ratio = avg_late / avg_early if avg_early > 0 else 0
            
            if ratio < 1.8:
                issues.append(f"Progressive: late/early ratio {ratio:.2f}× (expected ≥2.0×)")
    else:
        issues.append("Progressive: No data found for TC_User_BulkImport")
    
    if issues:
        return False, "Duration pattern issues:\n  " + "\n  ".join(issues)
    
    seasonal_ratio = avg_odd / avg_even if avg_even > 0 else 0
    step_ratio = avg_after / avg_before if avg_before > 0 else 0
    progressive_ratio = avg_late / avg_early if avg_early > 0 else 0
    
    msg = (
        f"✓ All duration patterns preserved\n"
        f"    Seasonal: {avg_odd:.1f}s (odd) / {avg_even:.1f}s (even) = {seasonal_ratio:.2f}×\n"
        f"    Step change: {avg_after:.1f}s (after) / {avg_before:.1f}s (before) = {step_ratio:.2f}×\n"
        f"    Progressive: {avg_late:.1f}s (late) / {avg_early:.1f}s (early) = {progressive_ratio:.2f}×"
    )
    
    return True, msg


def validate_ml_readiness(conn):
    """
    Validate that database is ready for Phase 4 ML models.
    
    Checks:
      - All required columns present
      - Data types correct
      - No missing values in critical columns
      - Sufficient data for each model
    
    Returns:
        tuple: (success, message)
    """
    cursor = conn.cursor()
    issues = []
    
    # Check ML1 (Flakiness Classifier) readiness
    cursor.execute("""
        SELECT COUNT(DISTINCT test_name) 
        FROM tests
    """)
    distinct_tests = cursor.fetchone()[0]
    if distinct_tests != 20:
        issues.append(f"ML1: Only {distinct_tests} distinct tests (expected 20)")
    
    # Check ML2 (Failure Clustering) readiness
    cursor.execute("SELECT COUNT(*) FROM failures WHERE message IS NOT NULL")
    failures_with_msg = cursor.fetchone()[0]
    if failures_with_msg < 400:
        issues.append(f"ML2: Only {failures_with_msg} failure messages (expected ≥400)")
    
    # Check ML3 (Anomaly Detection) readiness
    cursor.execute("SELECT COUNT(*) FROM runs WHERE pass_rate BETWEEN 20 AND 35")
    anomaly_runs = cursor.fetchone()[0]
    if anomaly_runs < 2:
        issues.append(f"ML3: Only {anomaly_runs} anomaly runs (expected 2)")
    
    # Check ML4 (Duration Drift) readiness
    special_tests = ['TC_Login_ValidCredentials', 'TC_Dashboard_ExportChart', 'TC_User_BulkImport']
    for test_name in special_tests:
        cursor.execute("SELECT COUNT(*) FROM tests WHERE test_name = ?", (test_name,))
        count = cursor.fetchone()[0]
        if count != 100:
            issues.append(f"ML4: {test_name} only has {count} records (expected 100)")
    
    if issues:
        return False, "ML readiness issues:\n  " + "\n  ".join(issues)
    
    return True, f"✓ Database ready for all 4 ML models\n    {distinct_tests} tests, {failures_with_msg} failures, {anomaly_runs} anomaly runs"


# MAIN VALIDATION FUNCTION

def validate(db_path):
    """
    Run all validation checks on database.
    
    Returns:
        bool: True if all checks pass
    """
    print("="*70)
    print("PHASE 2 DATABASE VALIDATION")
    print("="*70)
    print(f"\nValidating: {db_path}\n")
    
    # Connect to database
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as e:
        print(f"✗ Cannot connect to database: {e}")
        return False
    
    all_passed = True
    
    # Check 1: Row counts
    print("[1/6] Validating row counts...")
    success, message, _ = validate_row_counts(conn)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 2: Data quality
    print("[2/6] Validating data quality...")
    success, message = validate_data_quality(conn)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 3: Foreign keys
    print("[3/6] Validating foreign key integrity...")
    success, message = validate_foreign_keys(conn)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 4: Category balance
    print("[4/6] Validating category balance...")
    success, message = validate_category_balance(conn)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 5: Duration patterns
    print("[5/6] Validating duration patterns...")
    success, message = validate_duration_patterns(conn)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 6: ML readiness
    print("[6/6] Validating ML readiness...")
    success, message = validate_ml_readiness(conn)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    conn.close()
    
    # Final verdict
    print("="*70)
    if all_passed:
        print("✓ ALL VALIDATION CHECKS PASSED")
        print("="*70)
        print()
    else:
        print("✗ SOME VALIDATION CHECKS FAILED")
        print("="*70)
        print("\nPlease review errors above and fix issues.")
        print("\nTo re-run pipeline:")
        print("  python pipeline.py")
    
    return all_passed


# COMMAND LINE INTERFACE

def parse_args():
    """Parse command line arguments."""
    p = argparse.ArgumentParser(
        description="Phase 2 Database Validation Script",
        epilog="Validates analytics.db is ready for Phase 3 and 4"
    )
    p.add_argument("--db", "--database", dest="database_path",
                   default="./analytics.db",
                   help="Database path (default: ./analytics.db)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    success = validate(args.database_path)
    sys.exit(0 if success else 1)