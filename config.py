# CONFIG
DEFAULT_CONFIG = {
    "team_name":         "TeamAlpha",
    "suite_name":        "Suite_Regression_TeamAlpha",
    "num_runs":          100,
    "anomaly_runs":      [36, 37],
    "anomaly_pass_rate": 0.27,
    "start_date":        "2024-10-01",
    "interval_hours":    24,
    "output_dir":        "./runs",
    "seed":              42,
}

# TEST DEFINITIONS
TESTS = [
    # (id, name, feature_tag, priority_tag, category, fail_prob, duration_pattern, primary_fail, secondary_fail, primary_prob)
    ("s1-t1",  "TC_Login_ValidCredentials",   "feature_login",      "priority_high",   "stable",              0.00, "seasonal",          None,        None,        None),
    ("s1-t2",  "TC_Login_InvalidPassword",    "feature_login",      "priority_high",   "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t3",  "TC_Login_SessionTimeout",     "feature_login",      "priority_high",   "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t4",  "TC_Login_AccountLockout",     "feature_login",      "priority_medium", "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t5",  "TC_Login_MFAVerification",    "feature_login",      "priority_high",   "flaky-mild",          0.30, "normal",            "timeout",   "assertion", 0.70),
    ("s1-t6",  "TC_Dashboard_FilterByDate",   "feature_dashboard",  "priority_medium", "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t7",  "TC_Dashboard_Pagination",     "feature_dashboard",  "priority_medium", "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t8",  "TC_Dashboard_ExportChart",    "feature_dashboard",  "priority_medium", "stable",              0.00, "step_change",       None,        None,        None),
    ("s1-t9",  "TC_Dashboard_SearchBar",      "feature_dashboard",  "priority_medium", "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t10", "TC_User_CreateAccount",       "feature_usermgmt",   "priority_high",   "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t11", "TC_User_EditProfile",         "feature_usermgmt",   "priority_medium", "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t12", "TC_User_DeleteAccount",       "feature_usermgmt",   "priority_high",   "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t13", "TC_User_PasswordReset",       "feature_usermgmt",   "priority_medium", "stable",              0.00, "normal",            None,        None,        None),
    ("s1-t14", "TC_Login_SSORedirect",        "feature_login",      "priority_high",   "flaky-mild",          0.35, "normal",            "timeout",   "element",   0.70),
    ("s1-t15", "TC_Dashboard_LoadWidget",     "feature_dashboard",  "priority_medium", "flaky-moderate",      0.50, "normal",            "element",   "timeout",   0.80),
    ("s1-t16", "TC_Dashboard_RefreshData",    "feature_dashboard",  "priority_medium", "flaky-moderate",      0.55, "normal",            "assertion", "data",      0.60),
    ("s1-t17", "TC_User_BulkImport",          "feature_usermgmt",   "priority_medium", "flaky-heavy",         0.65, "progressive",       "data",      "assertion", 0.70),
    ("s1-t18", "TC_User_RoleAssignment",      "feature_usermgmt",   "priority_high",   "consistently_failing",0.80, "normal",            "assertion", "data",      0.65),
    ("s1-t19", "TC_User_BatchExport",         "feature_usermgmt",   "priority_medium", "consistently_failing",0.75, "normal",            "data",      "element",   0.65),
    ("s1-t20", "TC_Login_OAuthCallback",      "feature_login",      "priority_high",   "consistently_failing",0.70, "normal",            "timeout",   "element",   0.70),
    ("s1-t21", "TC_API_UserProfile_Get",     "feature_api",        "priority_medium", "flaky-moderate",      0.40, "normal",            "assertion", "data",      0.75),
    ("s1-t22", "TC_Report_GenerateMonthly",  "feature_reports",    "priority_medium", "flaky-mild",          0.35, "normal",            "environment","timeout",  0.70),
    ("s1-t23", "TC_Export_AuditLogs",        "feature_audit",      "priority_medium", "flaky-mild",          0.30, "normal",            "data",      "assertion", 0.80),
]

# DEPENDENCY MODEL
# Each entry maps a test name to the upstream tests it depends on and a
# propagation weight.  If any dependency failed, the test's own fail_prob
# is raised via a multiplicative risk model before decide_outcome() is called:
#   effective_fail_prob = 1 - (1 - base_fail_prob) * (1 - weight * failed_dep_count)
# Result is clamped to 0.95 so no test is guaranteed to fail.
DEPENDENCIES = {
    "TC_Dashboard_FilterByDate": {"deps": ["TC_Login_ValidCredentials"],   "weight": 0.4},
    "TC_Dashboard_Pagination":   {"deps": ["TC_Login_ValidCredentials"],   "weight": 0.4},
    "TC_Dashboard_ExportChart":  {"deps": ["TC_Dashboard_FilterByDate"],   "weight": 0.5},
    "TC_Dashboard_LoadWidget":   {"deps": ["TC_Login_ValidCredentials"],   "weight": 0.6},
    "TC_User_BulkImport":        {"deps": ["TC_User_CreateAccount"],       "weight": 0.5},
}