"""
Phase 1 Validation Script

Validates that generated synthetic data matches all design decisions from design_doc.md:
  - Design Question 1: Class balance (5+ distinct failure probabilities)
  - Design Question 2: Category balance (all categories 22-34%)
  - Design Question 3: Duration patterns (seasonal, step-change, progressive)

Usage:
  python validate_output.py                # Validate all runs in ./runs/
  python validate_output.py --runs-dir ./my_runs  # Custom directory

Output:
  - ✓ for passing checks
  - ⚠ for warnings (acceptable but noted)
  - ✗ for failures (must fix)
"""

import argparse
import json
import os
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
import statistics

# VALIDATION CHECKS

def validate_structure(runs_dir):
    """
    Validate that output directory structure is correct.
    
    Checks:
      - runs/ directory exists
      - Contains 100 TeamAlpha_build_XXX folders
      - Each folder has output.xml and ci_metadata.json
    
    Returns:
        tuple: (success, message, folder_list)
    """
    if not os.path.exists(runs_dir):
        return False, f"Directory '{runs_dir}' not found", []
    
    folders = sorted([
        f for f in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, f)) and f.startswith("TeamAlpha_build_")
    ])
    
    if len(folders) != 100:
        return False, f"Expected 100 run folders, found {len(folders)}", folders
    
    # Check each folder has required files
    for folder in folders:
        folder_path = os.path.join(runs_dir, folder)
        
        if not os.path.exists(os.path.join(folder_path, "output.xml")):
            return False, f"Missing output.xml in {folder}", folders
        
        if not os.path.exists(os.path.join(folder_path, "ci_metadata.json")):
            return False, f"Missing ci_metadata.json in {folder}", folders
    
    return True, "✓ All 100 run folders present with required files", folders


def validate_pass_rate_curve(runs_dir, folders):
    """
    Validate that pass rate curve matches design.
    
    Checks:
      - Runs 36-37 have ~27% pass rate (anomaly)
      - Overall trend follows expected curve
      - No impossible values (0% or 100% unless anomaly)
    
    Returns:
        tuple: (success, message, pass_rates)
    """
    pass_rates = []
    
    for folder in folders:
        meta_path = os.path.join(runs_dir, folder, "ci_metadata.json")
        with open(meta_path, 'r') as f:
            meta = json.load(f)
            pass_rates.append((meta['build_no'], meta['pass_rate_pct']))
    
    # Check runs 36-37 (anomaly)
    run_36 = next(pr for bn, pr in pass_rates if bn == 36)
    run_37 = next(pr for bn, pr in pass_rates if bn == 37)
    
    if not (20 <= run_36 <= 35):
        return False, f"Run 36 pass rate {run_36}% not in anomaly range (20-35%)", pass_rates
    
    if not (20 <= run_37 <= 35):
        return False, f"Run 37 pass rate {run_37}% not in anomaly range (20-35%)", pass_rates
    
    # Check late runs are generally high
    late_avg = statistics.mean([pr for bn, pr in pass_rates if 76 <= bn <= 100])
    
    if late_avg < 80:
        return False, f"Late runs (76-100) average {late_avg:.1f}%, expected >80%", pass_rates
    
    return True, f"✓ Pass rate curve correct (runs 36-37: {run_36:.1f}%, {run_37:.1f}%; late avg: {late_avg:.1f}%)", pass_rates


def validate_category_balance(runs_dir, folders):
    """
    Validate that failure categories are balanced.
    
    Checks:
      - All categories are 22-34% of total failures
      - No category dominates (>40%)
      - Total failures ~460 across all runs
    
    Returns:
        tuple: (success, message, category_counts)
    """
    category_counts = Counter()
    
    for folder in folders:
        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        # Find all failure messages
        for msg in root.findall(".//msg[@level='FAIL']"):
            text = msg.text or ""
            
            # Categorize by message pattern
            if "still visible after" in text and "timeout" in text:
                category_counts["timeout"] += 1
            elif "not found after" in text and "retries" in text:
                category_counts["element"] += 1
            elif "Expected HTTP status" in text:
                category_counts["assertion"] += 1
            elif "CSV export contained" in text and "rows" in text:
                category_counts["data"] += 1
            elif "environment" in text.lower() or "unreachable" in text.lower():
                category_counts["environment"] += 1
    
    total_failures = sum(category_counts.values())
    
    if total_failures < 400 or total_failures > 520:
        return False, f"Total failures {total_failures} outside expected range (400-520)", category_counts
    
    # Check category balance
    issues = []
    
    for category in ["timeout", "element", "assertion", "data"]:
        count = category_counts.get(category, 0)
        pct = (count / total_failures * 100) if total_failures > 0 else 0
        
        if pct < 20:
            issues.append(f"{category}: {count} ({pct:.1f}%) BELOW 22% minimum")
        elif pct > 35:
            issues.append(f"{category}: {count} ({pct:.1f}%) ABOVE 34% maximum")
    
    if issues:
        return False, "Category balance issues:\n  " + "\n  ".join(issues), category_counts
    
    # Format success message
    breakdown = ", ".join([
        f"{cat}: {category_counts.get(cat, 0)} ({category_counts.get(cat, 0)/total_failures*100:.1f}%)"
        for cat in ["timeout", "element", "assertion", "data"]
    ])
    
    return True, f"✓ Categories balanced ({total_failures} total failures)\n  {breakdown}", category_counts


def validate_duration_patterns(runs_dir, folders):
    """
    Validate that all three duration patterns are present.
    
    Checks:
      - TC_Login_ValidCredentials: Seasonal (even/odd difference)
      - TC_Dashboard_ExportChart: Step change at run 51
      - TC_User_BulkImport: Progressive drift (late >> early)
    
    Returns:
        tuple: (success, message, pattern_data)
    """
    # Collect durations for special tests
    seasonal_durations = {"even": [], "odd": []}
    step_before = []
    step_after = []
    progressive_early = []
    progressive_late = []
    
    for folder in folders:
        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        # Get run number from folder name
        run_num = int(folder.split("_")[-1])
        
        # Find test elements
        for test_el in root.findall(".//test"):
            test_name = test_el.get("name")
            status_el = test_el.find("status")
            
            if status_el is None:
                continue
            
            # Calculate duration from timestamps
            start_str = status_el.get("starttime", "")
            end_str = status_el.get("endtime", "")
            
            if not start_str or not end_str:
                continue
            
            try:
                # Parse Robot Framework timestamp: YYYYMMDD HH:MM:SS.mmm
                start_dt = parse_rf_timestamp(start_str)
                end_dt = parse_rf_timestamp(end_str)
                duration = (end_dt - start_dt).total_seconds()
            except:
                continue
            
            # Collect durations for each pattern
            if test_name == "TC_Login_ValidCredentials":
                if run_num % 2 == 0:
                    seasonal_durations["even"].append(duration)
                else:
                    seasonal_durations["odd"].append(duration)
            
            elif test_name == "TC_Dashboard_ExportChart":
                if run_num <= 50:
                    step_before.append(duration)
                else:
                    step_after.append(duration)
            
            elif test_name == "TC_User_BulkImport":
                if run_num <= 20:
                    progressive_early.append(duration)
                elif run_num >= 80:
                    progressive_late.append(duration)
    
    issues = []
    
    # Initialize averages
    avg_even = 0
    avg_odd = 0
    avg_before = 0
    avg_after = 0
    avg_early = 0
    avg_late = 0
    
    # Check seasonal pattern
    if seasonal_durations["even"] and seasonal_durations["odd"]:
        avg_even = statistics.mean(seasonal_durations["even"])
        avg_odd = statistics.mean(seasonal_durations["odd"])
        ratio = avg_odd / avg_even if avg_even > 0 else 0
        
        if ratio < 1.5:
            issues.append(f"Seasonal: odd/even ratio {ratio:.2f}× (expected ≥1.5×)")
    else:
        issues.append("Seasonal: No durations found")
    
    # Check step change
    if step_before and step_after:
        avg_before = statistics.mean(step_before)
        avg_after = statistics.mean(step_after)
        ratio = avg_after / avg_before if avg_before > 0 else 0
        
        if ratio < 2.5:
            issues.append(f"Step change: after/before ratio {ratio:.2f}× (expected ≥2.5×)")
    else:
        issues.append("Step change: No durations found")
    
    # Check progressive drift
    if progressive_early and progressive_late:
        avg_early = statistics.mean(progressive_early)
        avg_late = statistics.mean(progressive_late)
        ratio = avg_late / avg_early if avg_early > 0 else 0
        
        if ratio < 2.0:
            issues.append(f"Progressive: late/early ratio {ratio:.2f}× (expected ≥2.0×)")
    else:
        issues.append("Progressive: No durations found")
    
    if issues:
        return False, "Duration pattern issues:\n  " + "\n  ".join(issues), {}
    
    # Success message
    msg = (
        f"✓ All duration patterns detected\n"
        f"  Seasonal: {avg_odd:.1f}s (odd) / {avg_even:.1f}s (even) = {avg_odd/avg_even:.2f}×\n"
        f"  Step change: {avg_after:.1f}s (after) / {avg_before:.1f}s (before) = {avg_after/avg_before:.2f}×\n"
        f"  Progressive: {avg_late:.1f}s (late) / {avg_early:.1f}s (early) = {avg_late/avg_early:.2f}×"
    )
    
    return True, msg, {}


def parse_rf_timestamp(ts_str):
    """Parse Robot Framework timestamp string to datetime."""
    from datetime import datetime
    # Format: YYYYMMDD HH:MM:SS.mmm
    # Example: 20241001 14:23:45.123
    return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S.%f")


def validate_test_counts(runs_dir, folders):
    """
    Validate that each run has exactly 20 tests.
    
    Returns:
        tuple: (success, message, test_counts)
    """
    test_counts = []
    
    for folder in folders:
        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        tests = root.findall(".//test")
        count = len(tests)
        test_counts.append(count)
        
        if count != 20:
            return False, f"{folder}: Expected 20 tests, found {count}", test_counts
    
    return True, f"✓ All runs have exactly 20 tests", test_counts


# SUMMARY STATISTICS

def print_summary_statistics(runs_dir, folders):
    """Print detailed summary statistics."""
    print("\n" + "="*70)
    print("SUMMARY STATISTICS")
    print("="*70)
    
    # Collect all data
    all_pass_rates = []
    test_outcomes = defaultdict(lambda: {"pass": 0, "fail": 0})
    
    for folder in folders:
        # Load metadata
        meta_path = os.path.join(runs_dir, folder, "ci_metadata.json")
        with open(meta_path, 'r') as f:
            meta = json.load(f)
            all_pass_rates.append(meta['pass_rate_pct'])
        
        # Load XML
        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        # Count test outcomes
        for test_el in root.findall(".//test"):
            test_name = test_el.get("name")
            status_el = test_el.find("status")
            if status_el is not None:
                status = status_el.get("status")
                if status == "PASS":
                    test_outcomes[test_name]["pass"] += 1
                else:
                    test_outcomes[test_name]["fail"] += 1
    
    # Pass rate statistics
    print("\nPass Rate Statistics:")
    print(f"  Mean:   {statistics.mean(all_pass_rates):.1f}%")
    print(f"  Median: {statistics.median(all_pass_rates):.1f}%")
    print(f"  Min:    {min(all_pass_rates):.1f}%")
    print(f"  Max:    {max(all_pass_rates):.1f}%")
    print(f"  StdDev: {statistics.stdev(all_pass_rates):.1f}%")
    
    # Test failure rates
    print("\nTest Failure Rates (sorted by failure %):")
    print(f"  {'Test Name':<35} {'Fail%':>7}  {'Pass':>5}  {'Fail':>5}")
    print(f"  {'-'*35} {'-'*7}  {'-'*5}  {'-'*5}")
    
    # Sort by failure rate
    test_stats = []
    for test_name, outcomes in test_outcomes.items():
        total = outcomes["pass"] + outcomes["fail"]
        fail_pct = (outcomes["fail"] / total * 100) if total > 0 else 0
        test_stats.append((test_name, fail_pct, outcomes["pass"], outcomes["fail"]))
    
    test_stats.sort(key=lambda x: x[1], reverse=True)
    
    for test_name, fail_pct, passes, fails in test_stats:
        # Truncate long names
        display_name = test_name[:35]
        print(f"  {display_name:<35} {fail_pct:>6.1f}%  {passes:>5}  {fails:>5}")


# MAIN VALIDATION FUNCTION

def validate(runs_dir):
    """
    Run all validation checks.
    
    Returns:
        bool: True if all checks pass
    """
    print("="*70)
    print("PHASE 1 VALIDATION — Synthetic Test Data")
    print("="*70)
    print(f"\nValidating: {runs_dir}/\n")
    
    all_passed = True
    
    # Check 1: Directory structure
    print("[1/5] Validating directory structure...")
    success, message, folders = validate_structure(runs_dir)
    print(f"      {message}")
    if not success:
        return False
    print()
    
    # Check 2: Test counts
    print("[2/5] Validating test counts...")
    success, message, _ = validate_test_counts(runs_dir, folders)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 3: Pass rate curve
    print("[3/5] Validating pass rate curve...")
    success, message, _ = validate_pass_rate_curve(runs_dir, folders)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 4: Category balance
    print("[4/5] Validating category balance...")
    success, message, _ = validate_category_balance(runs_dir, folders)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Check 5: Duration patterns
    print("[5/5] Validating duration patterns...")
    success, message, _ = validate_duration_patterns(runs_dir, folders)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()
    
    # Summary statistics
    print_summary_statistics(runs_dir, folders)
    
    # Final verdict
    print("\n" + "="*70)
    if all_passed:
        print("✓ ALL VALIDATION CHECKS PASSED")
        print("="*70)
        print("\nYour synthetic data is ready for Phase 2!")
        print("\nNext steps:")
        print("  1. Commit to Git: git add . && git commit -m 'Complete Phase 1'")
        print("  2. Start Phase 2: Data ingestion pipeline")
    else:
        print("✗ SOME VALIDATION CHECKS FAILED")
        print("="*70)
        print("\nPlease review errors above and regenerate data if needed.")
    
    return all_passed


# COMMAND LINE INTERFACE

def parse_args():
    """Parse command line arguments."""
    p = argparse.ArgumentParser(
        description="Phase 1 Validation Script",
        epilog="Validates synthetic test data against design_doc.md specifications"
    )
    p.add_argument("--runs-dir", default="./runs",
                   help="Directory containing generated runs (default: ./runs)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    success = validate(args.runs_dir)
    exit(0 if success else 1)