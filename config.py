"""
Phase 1 Configuration — Test Definitions and Dependencies

This module defines all test configurations that implement the design decisions
from design_doc.md:
  - Design Question 1: Class balance with 5 flaky probability levels
  - Design Question 2: Category balance with 70/30 primary/secondary split
  - Design Question 3: Duration patterns (seasonal, step-change, progressive)

Each test is defined as a tuple with:
  (id, name, feature_tag, priority_tag, category, fail_prob, duration_pattern,
   primary_fail, secondary_fail, primary_prob)

"""

# DEFAULT CONFIGURATION

DEFAULT_CONFIG = {
    "team_name":         "TeamAlpha",
    "suite_name":        "Suite_Regression_TeamAlpha",
    "num_runs":          100,
    "anomaly_runs":      [36, 37],           # Runs with V-shape dip in pass rate
    "anomaly_pass_rate": 0.27,               # ~27% pass rate during anomaly
    "start_date":        "2024-10-01",       # First run timestamp
    "interval_hours":    24,                 # 24 hours between runs (daily builds)
    "output_dir":        "./runs",           # Output directory for generated files
    "seed":              42,                 # Random seed for reproducibility
}


TESTS = [
    # Format: (id, name, feature_tag, priority_tag, category, fail_prob, duration_pattern, primary_fail, secondary_fail, primary_prob)
    

    # STABLE TESTS (12 tests, 60% of suite) — 0% fail probability
    
    ("s1-t1",  "TC_Login_ValidCredentials",   "feature_login",      "priority_high",   "stable", 0.00, "seasonal",    None,        None,        None),
    ("s1-t2",  "TC_Login_InvalidPassword",    "feature_login",      "priority_high",   "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t3",  "TC_Login_SessionTimeout",     "feature_login",      "priority_high",   "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t4",  "TC_Login_AccountLockout",     "feature_login",      "priority_medium", "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t6",  "TC_Dashboard_FilterByDate",   "feature_dashboard",  "priority_medium", "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t7",  "TC_Dashboard_Pagination",     "feature_dashboard",  "priority_medium", "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t8",  "TC_Dashboard_ExportChart",    "feature_dashboard",  "priority_medium", "stable", 0.00, "step_change", None,        None,        None),
    ("s1-t9",  "TC_Dashboard_SearchBar",      "feature_dashboard",  "priority_medium", "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t10", "TC_User_CreateAccount",       "feature_usermgmt",   "priority_high",   "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t11", "TC_User_EditProfile",         "feature_usermgmt",   "priority_medium", "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t12", "TC_User_DeleteAccount",       "feature_usermgmt",   "priority_high",   "stable", 0.00, "normal",      None,        None,        None),
    ("s1-t13", "TC_User_PasswordReset",       "feature_usermgmt",   "priority_medium", "stable", 0.00, "normal",      None,        None,        None),
    

    # FLAKY-MILD TESTS (2 tests, 10% of suite) — 30%, 35% fail probability
    
    ("s1-t5",  "TC_Login_MFAVerification",    "feature_login",      "priority_high",   "flaky-mild", 0.30, "normal", "timeout",   "assertion", 0.70),
    ("s1-t14", "TC_Login_SSORedirect",        "feature_login",      "priority_high",   "flaky-mild", 0.35, "normal", "timeout",   "element",   0.70),
    
    
    # FLAKY-MODERATE TESTS (2 tests, 10% of suite) — 50%, 55% fail probability
    
    ("s1-t15", "TC_Dashboard_LoadWidget",     "feature_dashboard",  "priority_medium", "flaky-moderate", 0.50, "normal", "element",   "timeout",   0.70),
    ("s1-t16", "TC_Dashboard_RefreshData",    "feature_dashboard",  "priority_medium", "flaky-moderate", 0.55, "normal", "assertion", "data",      0.70),
    

    # FLAKY-HEAVY TEST (1 test, 5% of suite) — 65% fail probability
    
    ("s1-t17", "TC_User_BulkImport",          "feature_usermgmt",   "priority_medium", "flaky-heavy", 0.65, "progressive", "data", "assertion", 0.70),
    
    
    # CONSISTENTLY-FAILING TESTS (3 tests, 15% of suite) — 70%, 75%, 80%
    
    ("s1-t18", "TC_User_RoleAssignment",      "feature_usermgmt",   "priority_high",   "consistently_failing", 0.80, "normal", "assertion", "data",    0.65),
    ("s1-t19", "TC_User_BatchExport",         "feature_usermgmt",   "priority_medium", "consistently_failing", 0.75, "normal", "data",      "element", 0.65),
    ("s1-t20", "TC_Login_OAuthCallback",      "feature_login",      "priority_high",   "consistently_failing", 0.70, "normal", "timeout",   "element", 0.70),
]

# DEPENDENCY MODEL
#
# Models realistic test dependencies where upstream test failures increase
# downstream test failure probability.
#
# Multiplicative risk model:
#   effective_fail_prob = 1 - (1 - base_fail_prob) * (1 - weight * failed_dep_count)
#
# Result is clamped to 0.95 so no test is guaranteed to fail.

DEPENDENCIES = {
    # Dashboard tests depend on successful login
    "TC_Dashboard_FilterByDate": {
        "deps": ["TC_Login_ValidCredentials"],
        "weight": 0.4  # 40% risk increase if login fails
    },
    "TC_Dashboard_Pagination": {
        "deps": ["TC_Login_ValidCredentials"],
        "weight": 0.4
    },
    "TC_Dashboard_LoadWidget": {
        "deps": ["TC_Login_ValidCredentials"],
        "weight": 0.6  # Higher weight because widget is flaky already
    },
    
    # Export depends on filter working
    "TC_Dashboard_ExportChart": {
        "deps": ["TC_Dashboard_FilterByDate"],
        "weight": 0.5  # 50% risk increase if filter fails
    },
    
    # Bulk import depends on basic account creation working
    "TC_User_BulkImport": {
        "deps": ["TC_User_CreateAccount"],
        "weight": 0.5
    },
}

# VALIDATION CHECKS

def validate_config():
    """
    Validate that test configuration matches design document expectations.
    
    Checks:
      1. Total test count = 20
      2. Category distribution matches design (12 stable, 2+2+1+3 flaky)
      3. Fail probabilities create 5+ distinct levels
      4. All tests with failures have primary/secondary types
      5. Primary probabilities are mostly 0.70 (70/30 split)
    
    Raises:
        AssertionError: If any validation check fails
    """
    # Check 1: Total test count
    assert len(TESTS) == 20, f"Expected 20 tests, got {len(TESTS)}"
    
    # Check 2: Category distribution
    categories = {}
    for test in TESTS:
        cat = test[4]  # category is 5th element (index 4)
        categories[cat] = categories.get(cat, 0) + 1
    
    expected = {
        "stable": 12,
        "flaky-mild": 2,
        "flaky-moderate": 2,
        "flaky-heavy": 1,
        "consistently_failing": 3
    }
    
    for cat, expected_count in expected.items():
        actual_count = categories.get(cat, 0)
        assert actual_count == expected_count, \
            f"Category '{cat}': expected {expected_count}, got {actual_count}"
    
    # Check 3: Distinct failure probability levels
    fail_probs = set()
    for test in TESTS:
        fail_prob = test[5]  # fail_prob is 6th element (index 5)
        if fail_prob > 0:
            fail_probs.add(fail_prob)
    
    assert len(fail_probs) >= 5, \
        f"Expected ≥5 distinct fail probabilities, got {len(fail_probs)}: {sorted(fail_probs)}"
    
    # Check 4: Failing tests have primary/secondary types
    for test in TESTS:
        name = test[1]
        fail_prob = test[5]
        primary = test[7]
        secondary = test[8]
        
        if fail_prob > 0:
            assert primary is not None, \
                f"Test '{name}' has fail_prob={fail_prob} but no primary failure type"
            assert secondary is not None, \
                f"Test '{name}' has fail_prob={fail_prob} but no secondary failure type"
    
    # Check 5: Most primary probabilities are 0.70
    primary_probs = []
    for test in TESTS:
        prim_prob = test[9]  # primary_prob is 10th element (index 9)
        if prim_prob is not None:
            primary_probs.append(prim_prob)
    
    # At least 50% should be 0.70 (standard 70/30 split)
    count_070 = sum(1 for p in primary_probs if abs(p - 0.70) < 0.01)
    assert count_070 / len(primary_probs) >= 0.5, \
        f"Expected ≥50% of primary_probs to be 0.70, got {count_070}/{len(primary_probs)}"
    
    print("✓ Configuration validation passed")
    print(f"  - {len(TESTS)} tests defined")
    print(f"  - {len(fail_probs)} distinct fail probability levels")
    print(f"  - {len(DEPENDENCIES)} test dependencies")
    print(f"  - Category distribution: {categories}")

# Run validation when module is imported
if __name__ == "__main__":
    validate_config()