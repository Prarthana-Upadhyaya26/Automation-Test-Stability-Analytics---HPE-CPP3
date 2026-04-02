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

# Design constants
MIN_RUNS = 100          # Hard minimum — fewer runs = FAIL
TESTS_PER_RUN = 20      # Fixed by design
ANOMALY_RUNS = (36, 37) # Fixed build numbers for the pass-rate dip

# Per-run expected rates (derived from 100-run baseline)
FAILURES_PER_RUN_MIN = 4.0   # ~20% of 20 tests
FAILURES_PER_RUN_MAX = 5.5   # ~27.5% of 20 tests
TAGS_PER_RUN_MIN = 50        # ~2.5 tags per test × 20 tests
TAGS_PER_RUN_MAX = 70        # ~3.5 tags per test × 20 tests

# Proportional window fractions
LATE_WINDOW_START_FRAC = 0.75   # Top 25% of runs = "late"
STEP_BOUNDARY_FRAC    = 0.50   # Step change midpoint
PROGRESSIVE_EARLY_FRAC = 0.20  # Bottom 20% = "early"
PROGRESSIVE_LATE_FRAC  = 0.80  # Top 20% = "late"


# HELPERS

def parse_rf_timestamp(ts_str):
    """Parse Robot Framework timestamp string to datetime."""
    from datetime import datetime
    # Format: YYYYMMDD HH:MM:SS.mmm
    return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S.%f")


def _window_label(frac, n_runs):
    """Return the first run number of a proportional window."""
    return int(n_runs * frac)


# VALIDATION CHECKS

def validate_structure(runs_dir):
    """
    Validate that output directory structure is correct.

    Checks:
      - runs/ directory exists
      - Contains at least MIN_RUNS (100) TeamAlpha_build_XXX folders
      - Each folder has output.xml and ci_metadata.json

    Returns:
        tuple: (success, message, folder_list, n_runs)
    """
    if not os.path.exists(runs_dir):
        return False, f"Directory '{runs_dir}' not found", [], 0

    folders = sorted([
        f for f in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, f)) and f.startswith("TeamAlpha_build_")
    ])

    n_runs = len(folders)

    if n_runs < MIN_RUNS:
        return (
            False,
            f"Expected at least {MIN_RUNS} run folders, found {n_runs} — FAIL",
            folders,
            n_runs,
        )
    
    # Check each folder has required files
    for folder in folders:
        folder_path = os.path.join(runs_dir, folder)

        if not os.path.exists(os.path.join(folder_path, "output.xml")):
            return False, f"Missing output.xml in {folder}", folders, n_runs

        if not os.path.exists(os.path.join(folder_path, "ci_metadata.json")):
            return False, f"Missing ci_metadata.json in {folder}", folders, n_runs

    return (
        True,
        f"✓ {n_runs} run folders present with required files (minimum {MIN_RUNS})",
        folders,
        n_runs,
    )


def validate_test_counts(runs_dir, folders):
    """
    Validate that each run has exactly TESTS_PER_RUN (20) tests.

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

        if count != TESTS_PER_RUN:
            return (
                False,
                f"{folder}: Expected {TESTS_PER_RUN} tests, found {count}",
                test_counts,
            )

    return True, f"✓ All {len(folders)} runs have exactly {TESTS_PER_RUN} tests", test_counts


def validate_pass_rate_curve(runs_dir, folders, n_runs):
    """
    Validate that pass rate curve matches design.

    Checks:
      - Runs 36-37 have ~20-35% pass rate (anomaly — fixed design artifact)
      - Late runs (top 25%) average >80%

    The late-run window scales with N so runs beyond 100 are fully included.

    Returns:
        tuple: (success, message, pass_rates)
    """
    pass_rates = []

    for folder in folders:
        meta_path = os.path.join(runs_dir, folder, "ci_metadata.json")
        with open(meta_path, "r") as f:
            meta = json.load(f)
            pass_rates.append((meta["build_no"], meta["pass_rate_pct"]))

    issues = []

    # --- Anomaly check (fixed design artifact at runs 36-37) ---
    anomaly_results = {}
    for run_no in ANOMALY_RUNS:
        match = [pr for bn, pr in pass_rates if bn == run_no]
        if not match:
            issues.append(f"Run {run_no} not found in dataset")
        else:
            anomaly_results[run_no] = match[0]
            if not (20 <= match[0] <= 35):
                issues.append(
                    f"Run {run_no} pass rate {match[0]:.1f}% not in anomaly range (20-35%)"
                )

    # --- Late-run average (scales with N) ---
    # Late = runs in the top 25%; threshold is inclusive of the boundary run.
    late_threshold = _window_label(LATE_WINDOW_START_FRAC, n_runs)  # e.g. 75 for N=100, 150 for N=200
    late_rates = [pr for bn, pr in pass_rates if bn > late_threshold]

    if not late_rates:
        issues.append(
            f"No late runs found (expected runs > {late_threshold} for N={n_runs})"
        )
    else:
        late_avg = statistics.mean(late_rates)
        if late_avg < 80:
            issues.append(
                f"Late runs (>{late_threshold}) average {late_avg:.1f}%, expected >80%"
            )

    if issues:
        return False, "Pass rate curve issues:\n  " + "\n  ".join(issues), pass_rates

    late_avg = statistics.mean(late_rates)
    anomaly_str = ", ".join(
        f"run {rn}: {anomaly_results[rn]:.1f}%" for rn in ANOMALY_RUNS
    )
    return (
        True,
        (
            f"✓ Pass rate curve correct\n"
            f"  Anomaly ({anomaly_str}); "
            f"late avg (runs >{late_threshold}): {late_avg:.1f}%"
        ),
        pass_rates,
    )


def validate_category_balance(runs_dir, folders, n_runs):
    """
    Validate that failure categories are balanced.

    Checks:
      - All main categories are 22-34% of total failures
      - Total failures in expected proportional range:
            [FAILURES_PER_RUN_MIN × N, FAILURES_PER_RUN_MAX × N]

    Returns:
        tuple: (success, message, category_counts)
    """
    category_counts = Counter()

    for folder in folders:
        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()

        for msg in root.findall(".//msg[@level='FAIL']"):
            text = msg.text or ""

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

    # Proportional failure-count bounds
    expected_min = int(FAILURES_PER_RUN_MIN * n_runs)
    expected_max = int(FAILURES_PER_RUN_MAX * n_runs)

    if not (expected_min <= total_failures <= expected_max):
        return (
            False,
            (
                f"Total failures {total_failures} outside expected range "
                f"({expected_min}–{expected_max}) for N={n_runs} runs"
            ),
            category_counts,
        )

    # Category balance check
    issues = []

    for category in ["timeout", "element", "assertion", "data"]:
        count = category_counts.get(category, 0)
        pct = (count / total_failures * 100) if total_failures > 0 else 0

        if pct < 20:
            issues.append(f"{category}: {count} ({pct:.1f}%) BELOW 22% minimum")
        elif pct > 36:
            issues.append(f"{category}: {count} ({pct:.1f}%) ABOVE 34% maximum")

    if issues:
        return False, "Category balance issues:\n  " + "\n  ".join(issues), category_counts

    breakdown = ", ".join([
        f"{cat}: {category_counts.get(cat, 0)} "
        f"({category_counts.get(cat, 0) / total_failures * 100:.1f}%)"
        for cat in ["timeout", "element", "assertion", "data"]
    ])

    return (
        True,
        f"✓ Categories balanced ({total_failures} total failures across {n_runs} runs)\n  {breakdown}",
        category_counts,
    )


def validate_duration_patterns(runs_dir, folders, n_runs):
    """
    Validate that all three duration patterns are present.

    All window boundaries scale proportionally with N:
      - TC_Login_ValidCredentials:   Seasonal (even/odd — all runs)
      - TC_Dashboard_ExportChart:    Step change at run N//2
      - TC_User_BulkImport:          Progressive drift
                                       early = runs 1 … floor(N×0.20)
                                       late  = runs ceil(N×0.80) … N

    Returns:
        tuple: (success, message, pattern_data)
    """
    # Proportional boundaries
    step_boundary    = n_runs // 2                        # e.g. 50 for N=100, 100 for N=200
    early_cutoff     = int(n_runs * PROGRESSIVE_EARLY_FRAC)  # e.g. 20 for N=100, 40 for N=200
    late_cutoff      = int(n_runs * PROGRESSIVE_LATE_FRAC)   # e.g. 80 for N=100, 160 for N=200

    seasonal_durations = {"even": [], "odd": []}
    step_before = []
    step_after  = []
    progressive_early = []
    progressive_late  = []

    for folder in folders:
        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()

        run_num = int(folder.split("_")[-1])

        for test_el in root.findall(".//test"):
            test_name = test_el.get("name")
            status_el = test_el.find("status")

            if status_el is None:
                continue

            start_str = status_el.get("starttime", "")
            end_str   = status_el.get("endtime", "")

            if not start_str or not end_str:
                continue

            try:
                start_dt = parse_rf_timestamp(start_str)
                end_dt   = parse_rf_timestamp(end_str)
                duration = (end_dt - start_dt).total_seconds()
            except Exception:
                continue

            if test_name == "TC_Login_ValidCredentials":
                key = "even" if run_num % 2 == 0 else "odd"
                seasonal_durations[key].append(duration)

            elif test_name == "TC_Dashboard_ExportChart":
                if run_num <= step_boundary:
                    step_before.append(duration)
                else:
                    step_after.append(duration)

            elif test_name == "TC_User_BulkImport":
                if run_num <= early_cutoff:
                    progressive_early.append(duration)
                elif run_num >= late_cutoff:
                    progressive_late.append(duration)

    issues = []

    # --- Seasonal ---
    if seasonal_durations["even"] and seasonal_durations["odd"]:
        avg_even = statistics.mean(seasonal_durations["even"])
        avg_odd  = statistics.mean(seasonal_durations["odd"])
        seasonal_ratio = avg_odd / avg_even if avg_even > 0 else 0

        if seasonal_ratio < 1.5:
            issues.append(
                f"Seasonal: odd/even ratio {seasonal_ratio:.2f}× (expected ≥1.5×)"
            )
    else:
        issues.append("Seasonal: No durations found for TC_Login_ValidCredentials")

    # --- Step change ---
    if step_before and step_after:
        avg_before = statistics.mean(step_before)
        avg_after  = statistics.mean(step_after)
        step_ratio = avg_after / avg_before if avg_before > 0 else 0

        if step_ratio < 2.5:
            issues.append(
                f"Step change: after/before ratio {step_ratio:.2f}× (expected ≥2.5×) "
                f"[boundary: run {step_boundary}]"
            )
    else:
        issues.append(
            f"Step change: No durations found for TC_Dashboard_ExportChart "
            f"(boundary: run {step_boundary})"
        )

    # --- Progressive drift ---
    if progressive_early and progressive_late:
        avg_early = statistics.mean(progressive_early)
        avg_late  = statistics.mean(progressive_late)
        prog_ratio = avg_late / avg_early if avg_early > 0 else 0

        if prog_ratio < 2.0:
            issues.append(
                f"Progressive: late/early ratio {prog_ratio:.2f}× (expected ≥2.0×) "
                f"[early ≤{early_cutoff}, late ≥{late_cutoff}]"
            )
    else:
        issues.append(
            f"Progressive: No durations found for TC_User_BulkImport "
            f"(early ≤{early_cutoff}, late ≥{late_cutoff})"
        )

    if issues:
        return False, "Duration pattern issues:\n  " + "\n  ".join(issues), {}

    msg = (
        f"✓ All duration patterns detected (N={n_runs})\n"
        f"  Seasonal:   {avg_odd:.1f}s (odd) / {avg_even:.1f}s (even) = {seasonal_ratio:.2f}×\n"
        f"  Step change [{step_boundary}]: "
        f"{avg_after:.1f}s (after) / {avg_before:.1f}s (before) = {step_ratio:.2f}×\n"
        f"  Progressive [≤{early_cutoff}/≥{late_cutoff}]: "
        f"{avg_late:.1f}s (late) / {avg_early:.1f}s (early) = {prog_ratio:.2f}×"
    )

    return True, msg, {}


# SUMMARY STATISTICS

def print_summary_statistics(runs_dir, folders, n_runs):
    """Print detailed summary statistics."""
    print("\n" + "=" * 70)
    print("SUMMARY STATISTICS")
    print("=" * 70)

    all_pass_rates = []
    test_outcomes  = defaultdict(lambda: {"pass": 0, "fail": 0})

    for folder in folders:
        meta_path = os.path.join(runs_dir, folder, "ci_metadata.json")
        with open(meta_path, "r") as f:
            meta = json.load(f)
            all_pass_rates.append(meta["pass_rate_pct"])

        xml_path = os.path.join(runs_dir, folder, "output.xml")
        tree = ET.parse(xml_path)
        root = tree.getroot()

        for test_el in root.findall(".//test"):
            test_name = test_el.get("name")
            status_el = test_el.find("status")
            if status_el is not None:
                status = status_el.get("status")
                if status == "PASS":
                    test_outcomes[test_name]["pass"] += 1
                else:
                    test_outcomes[test_name]["fail"] += 1

    print(f"\nRun count: {n_runs} (minimum required: {MIN_RUNS})")
    print("\nPass Rate Statistics:")
    print(f"  Mean:   {statistics.mean(all_pass_rates):.1f}%")
    print(f"  Median: {statistics.median(all_pass_rates):.1f}%")
    print(f"  Min:    {min(all_pass_rates):.1f}%")
    print(f"  Max:    {max(all_pass_rates):.1f}%")
    print(f"  StdDev: {statistics.stdev(all_pass_rates):.1f}%")

    print("\nTest Failure Rates (sorted by failure %):")
    print(f"  {'Test Name':<35} {'Fail%':>7}  {'Pass':>5}  {'Fail':>5}")
    print(f"  {'-'*35} {'-'*7}  {'-'*5}  {'-'*5}")

    test_stats = []
    for test_name, outcomes in test_outcomes.items():
        total    = outcomes["pass"] + outcomes["fail"]
        fail_pct = (outcomes["fail"] / total * 100) if total > 0 else 0
        test_stats.append((test_name, fail_pct, outcomes["pass"], outcomes["fail"]))

    test_stats.sort(key=lambda x: x[1], reverse=True)

    for test_name, fail_pct, passes, fails in test_stats:
        display_name = test_name[:35]
        print(f"  {display_name:<35} {fail_pct:>6.1f}%  {passes:>5}  {fails:>5}")


# MAIN VALIDATION FUNCTION

def validate(runs_dir):
    """
    Run all validation checks.

    Returns:
        bool: True if all checks pass
    """
    print("=" * 70)
    print("PHASE 1 VALIDATION — Synthetic Test Data")
    print("=" * 70)
    print(f"\nValidating: {runs_dir}/\n")

    all_passed = True

    # Check 1: Directory structure (also derives N)
    print("[1/5] Validating directory structure...")
    success, message, folders, n_runs = validate_structure(runs_dir)
    print(f"      {message}")
    if not success:
        # Cannot continue — n_runs unknown or below minimum
        print("\n✗ VALIDATION ABORTED: fix directory structure first.")
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
    success, message, _ = validate_pass_rate_curve(runs_dir, folders, n_runs)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()

    # Check 4: Category balance
    print("[4/5] Validating category balance...")
    success, message, _ = validate_category_balance(runs_dir, folders, n_runs)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()

    # Check 5: Duration patterns
    print("[5/5] Validating duration patterns...")
    success, message, _ = validate_duration_patterns(runs_dir, folders, n_runs)
    print(f"      {message}")
    if not success:
        all_passed = False
    print()

    # Summary statistics
    print_summary_statistics(runs_dir, folders, n_runs)

    # Final verdict
    print("\n" + "=" * 70)
    if all_passed:
        print("✓ ALL VALIDATION CHECKS PASSED")
        print("=" * 70)
        print()
    else:
        print("✗ SOME VALIDATION CHECKS FAILED")
        print("=" * 70)
        print("\nPlease review errors above and regenerate data if needed.")

    return all_passed


# COMMAND LINE INTERFACE

def parse_args():
    """Parse command line arguments."""
    p = argparse.ArgumentParser(
        description="Phase 1 Validation Script",
        epilog=(
            "Validates synthetic test data against design_doc.md specifications. "
            f"Requires at least {MIN_RUNS} runs; all thresholds scale with N."
        ),
    )
    p.add_argument(
        "--runs-dir",
        default="./runs",
        help="Directory containing generated runs (default: ./runs)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    success = validate(args.runs_dir)
    exit(0 if success else 1)