"""
WHAT REMAINS THE SAME FROM THE ORIGINAL VERSION
-----------------------------------------------
• The TESTS definition table (20 tests with categories and fail probabilities).
• Failure message generators (timeout / element / assertion / data).
• Duration patterns for specific tests:
    - Seasonal pattern       → TC_Login_ValidCredentials
    - Step change pattern    → TC_Dashboard_ExportChart
    - Progressive drift      → TC_User_BulkImport
• Robot Framework XML structure (<suite>, <test>, <kw>, <msg>, <status>).
• Metadata generation for each CI run (ci_metadata.json).
• Pass-rate curve design using run_pass_rate().

WHAT WAS CHANGED / FIXED
------------------------
1. Suite pass rate enforcement
   Instead of letting each test fail independently (which caused pass rates
   to drift away from the target curve), the generator now:

   - Computes the target number of failures for each run
   - Estimates how many failures will occur naturally from flaky tests
   - Force-fails only the remaining gap

   This keeps the overall run pass rate aligned with the intended curve.

2. Weighted failure selection
   Forced failures are selected using weights based on each test’s
   base failure probability, so flaky or consistently failing tests
   are more likely to be chosen.

3. Sampling without replacement
   The previous implementation could select the same test multiple
   times when choosing failures. The new logic samples without
   replacement to guarantee the correct number of forced failures.

HOW FAILURE DECISIONS WORK NOW
------------------------------
For each run:

1. A target pass rate is computed using run_pass_rate().
2. The number of required failures is derived from that pass rate.
3. Flaky tests still fail randomly based on their base probabilities.
4. Additional tests are force-failed (if needed) to reach the target.

This keeps:
• realistic flaky behaviour
• controlled suite-level pass rates
• deterministic anomaly runs

ANOMALY RUNS
------------
Runs defined in DEFAULT_CONFIG["anomaly_runs"] simulate major CI incidents
by lowering the suite pass rate significantly.

USAGE
-----
python generate.py
python generate.py --output-dir ./runs --num-runs 100
"""

import argparse
import json
import os
import random
from datetime import datetime, timedelta


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
]

DEPENDENCIES = {
    "TC_Dashboard_FilterByDate": {
        "deps": ["TC_Login_ValidCredentials"],
        "weight": 0.4
    },
    "TC_Dashboard_Pagination": {
        "deps": ["TC_Login_ValidCredentials"],
        "weight": 0.4
    },
    "TC_Dashboard_ExportChart": {
        "deps": ["TC_Dashboard_FilterByDate"],
        "weight": 0.5
    },
    "TC_Dashboard_LoadWidget": {
        "deps": ["TC_Login_ValidCredentials"],
        "weight": 0.6
    },
    "TC_User_BulkImport": {
        "deps": ["TC_User_CreateAccount"],
        "weight": 0.5
    }
}

# FAILURE MESSAGE GENERATORS
def gen_timeout_msg(rng):
    elements = ["loading-spinner", "overlay-modal", "progress-bar", "auth-redirect", "session-token"]
    timeouts  = [15, 20, 30, 45]
    return f"Element '{rng.choice(elements)}' still visible after {rng.choice(timeouts)}s timeout"

def gen_element_msg(rng):
    locators = ["id=widget-container", "id=submit-btn", "css=.data-grid", "id=modal-confirm", "css=.nav-item"]
    retries  = [3, 5, 7]
    return f"Element with locator '{rng.choice(locators)}' not found after {rng.choice(retries)} retries"

def gen_assertion_msg(rng):
    pairs = [("200","500","Internal Server Error"), ("200","404","Not Found"),
             ("201","400","Bad Request"),           ("200","503","Service Unavailable")]
    exp, got, desc = rng.choice(pairs)
    return f"Expected HTTP status '{exp}' but got '{got}' — {desc}"

def gen_data_msg(rng):
    rows   = [0, 1, 2]
    mins   = [50, 100, 200]
    ranges = ["Oct 2024", "last 30 days", "Q4 2024", "last 7 days"]
    return f"CSV export contained {rng.choice(rows)} rows — expected at least {rng.choice(mins)} records for {rng.choice(ranges)}"

FAIL_GEN = {
    "timeout":   gen_timeout_msg,
    "element":   gen_element_msg,
    "assertion": gen_assertion_msg,
    "data":      gen_data_msg,
}

FAIL_KW = {
    "timeout":   "Wait Until Element Is Visible",
    "element":   "Click Element",
    "assertion": "Should Be Equal As Integers",
    "data":      "Should Not Be Empty",
}

# How much extra failure probability each flaky category gets during an anomaly run
FLAKY_ANOMALY_BOOST = {
    "flaky-mild":     0.30,
    "flaky-moderate": 0.55,
    "flaky-heavy":    0.65,
}

def run_pass_rate(n, anomaly_runs, anomaly_pass_rate):
    if n in anomaly_runs:
        return anomaly_pass_rate
    if   1  <= n <= 25: return random.uniform(0.60, 0.70)
    elif 26 <= n <= 35: return random.uniform(0.55, 0.65)
    elif 38 <= n <= 45: return random.uniform(0.50, 0.55)
    elif 46 <= n <= 75: return random.uniform(0.55, 0.80)
    else:               return random.uniform(0.82, 0.95)

def fmt_ts(dt):
    return dt.strftime("%Y%m%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

def decide_outcome(category, fail_prob, rng):
    """Return True if test passes. Anomaly character is expressed via the pass rate
    curve in build_run (forced failures), not here."""
    if category == "stable":
        return True
    if category in ("flaky-mild", "flaky-moderate", "flaky-heavy"):
        return rng.random() > fail_prob
    if category == "consistently_failing":
        return rng.random() > fail_prob
    return True

# XML BUILDERS
import math

def get_duration(test, run, rng):
    _, name, _, _, _, _, pattern, _, _, _ = test

    base = rng.uniform(1.5, 3.0)

    if pattern == "normal":
        return base + rng.uniform(-0.3, 0.3)

    if pattern == "progressive":
        value = base + 0.05 * run
        
        if run == 50:
            value *= 0.6  # simulate scaling
        
        return value + rng.uniform(-0.3, 0.3)

    elif pattern == "step_change":
        # sudden jump after some run
        if run < 40:
            return base + rng.uniform(-0.3, 0.3)
        else:
            return base * 2 + rng.uniform(-0.5, 0.5)

    elif pattern == "seasonal":
        # sine wave oscillation
        return base + 1.5 * math.sin(run / 5) + rng.uniform(-0.2, 0.2)

    else:
        return base
def build_test_xml(test, n, current_dt, rng, force_fail=False):

    tid, name, feature_tag, priority_tag, category, fail_prob, _, primary, secondary, prim_prob = test

    if force_fail:
        status = "FAIL"
    else:
        status = "PASS" if decide_outcome(category, fail_prob, rng) else "FAIL"

    dur = get_duration(test, n, rng)

    if status == "FAIL":
        dur += rng.uniform(5.0, 10.0)  # failures still take longer

    start_dt = current_dt
    end_dt   = start_dt + timedelta(seconds=dur)

    start_s = fmt_ts(start_dt)
    end_s   = fmt_ts(end_dt)
    info_ts = fmt_ts(start_dt + timedelta(milliseconds=200))

    tags_xml = (
        f'      <tag>alpha_regression</tag>\n'
        f'      <tag>{feature_tag}</tag>\n'
        f'      <tag>{priority_tag}</tag>\n'
    )

    if status == "PASS":
        kw_xml = (
            f'      <kw name="Run Test Steps" library="SeleniumLibrary">\n'
            f'        <msg timestamp="{info_ts}" level="INFO">Executing {name}</msg>\n'
            f'        <status status="PASS" starttime="{start_s}" endtime="{end_s}"/>\n'
            f'      </kw>\n'
        )
        outer_status = f'      <status status="PASS" starttime="{start_s}" endtime="{end_s}"/>\n'
    else:
        if prim_prob is None:
            ftype = primary or "timeout"
        else:
            ftype = primary if rng.random() < prim_prob else secondary

        fail_msg = FAIL_GEN[ftype](rng)

        kw_xml = (
            f'      <kw name="Run Test Steps" library="SeleniumLibrary">\n'
            f'        <msg timestamp="{info_ts}" level="INFO">Executing {name}</msg>\n'
            f'        <msg level="FAIL">{fail_msg}</msg>\n'
            f'        <status status="FAIL" starttime="{start_s}" endtime="{end_s}"/>\n'
            f'      </kw>\n'
        )
        outer_status = f'      <status status="FAIL" starttime="{start_s}" endtime="{end_s}">{fail_msg}</status>\n'

    xml = (
        f'    <test id="{tid}" name="{name}">\n'
        f'{tags_xml}'
        f'{kw_xml}'
        f'{outer_status}'
        f'    </test>\n'
    )
    return xml, status, end_dt


def build_run(n, config, rng):

    run_dt = datetime.fromisoformat(config["start_date"]) + timedelta(hours=config["interval_hours"] * (n - 1))

    generated_s = fmt_ts(run_dt)
    suite_start  = run_dt

    target_pass_rate = run_pass_rate(n, config["anomaly_runs"], config["anomaly_pass_rate"])
    total_tests = len(TESTS)
    target_failures = round(total_tests * (1 - target_pass_rate))

    # Flaky tests will fail naturally on their own — only force the remaining gap
    expected_natural_failures = round(sum(
        test[5] for test in TESTS
        if test[4] in ("flaky-mild", "flaky-moderate", "flaky-heavy")
    ))
    forced_failure_count = max(0, target_failures - expected_natural_failures)

    weights = []
    test_results = {}
    for test in TESTS:
        category = test[4]
        fail_prob = test[5]
        if category == "stable":
            weights.append(0.02)
        else:
            weights.append(fail_prob)

    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    # Sample without replacement to avoid duplicates skewing the count
    fail_indices = set()
    candidates = list(range(len(TESTS)))
    candidate_weights = list(weights)
    while len(fail_indices) < forced_failure_count and candidates:
        chosen = rng.choices(candidates, weights=candidate_weights, k=1)[0]
        fail_indices.add(chosen)
        idx = candidates.index(chosen)
        candidates.pop(idx)
        candidate_weights.pop(idx)
        total_cw = sum(candidate_weights)
        if total_cw > 0:
            candidate_weights = [w / total_cw for w in candidate_weights]

    setup_end = suite_start + timedelta(milliseconds=110)
    suite_xml = (
        f'  <suite id="s1" name="{config["suite_name"]}" '
        f'source="/opt/ci/tests/alpha/{config["suite_name"]}.robot">\n'
        f'    <kw name="Suite Setup" type="setup">\n'
        f'      <msg timestamp="{generated_s}" level="INFO">'
        f'Suite {config["suite_name"]} initialized — team {config["team_name"]}</msg>\n'
        f'      <status status="PASS" starttime="{generated_s}" endtime="{fmt_ts(setup_end)}"/>\n'
        f'    </kw>\n'
    )

    cursor = setup_end + timedelta(milliseconds=200)
    passed = 0
    failed = 0
    tests_xml = ""

    test_results = {}

    for i, test in enumerate(TESTS):
        name = test[1]
        category = test[4]
        base_fail_prob = test[5]

        # Start with base probability
        fail_prob = base_fail_prob

        # 🔥 Apply dependency weights
        dep_info = DEPENDENCIES.get(name)
        if dep_info:
            deps = dep_info["deps"]
            weight = dep_info["weight"]

            failed_count = sum(
                test_results.get(d) == "FAIL" for d in deps
            )

            # multiplicative risk model (more realistic)
            if failed_count > 0:
                fail_prob = 1 - (1 - fail_prob) * (1 - weight * failed_count)

        # 🔥 Clamp probability
        fail_prob = min(fail_prob, 0.95)

        # Decide outcome probabilistically
        if category == "stable":
            status = "PASS"
        else:
            status = "PASS" if rng.random() > fail_prob else "FAIL"

        # 🔥 STILL allow forced failures (for pass-rate control)
        if i in fail_indices:
            status = "FAIL"

        t_xml, _, cursor = build_test_xml(
            test, n, cursor, rng, force_fail=(status == "FAIL")
        )
        tests_xml += t_xml

        test_results[name] = status

        cursor += timedelta(milliseconds=rng.randint(100, 300))

        if status == "PASS":
            passed += 1
        else:
            failed += 1

    suite_status = "FAIL" if failed > 0 else "PASS"
    suite_end_s  = fmt_ts(cursor)

    suite_xml += tests_xml
    suite_xml += (
        f'    <status status="{suite_status}" starttime="{generated_s}" '
        f'endtime="{suite_end_s}" passed="{passed}" failed="{failed}"/>\n'
        f'  </suite>\n'
    )

    stats_xml = (
        f'  <statistics>\n'
        f'    <total>\n'
        f'      <stat pass="{passed}" fail="{failed}">All Tests</stat>\n'
        f'    </total>\n'
        f'    <tag>\n'
        f'      <stat pass="{passed}" fail="{failed}">alpha_regression</stat>\n'
        f'    </tag>\n'
        f'  </statistics>\n'
        f'  <errors/>\n'
    )

    full_xml = (
        f'<robot generator="Robot 6.1.1 (Python 3.10.12)" '
        f'generated="{generated_s}" rpa="FALSE" schemaversion="4">\n'
        f'{suite_xml}'
        f'{stats_xml}'
        f'</robot>\n'
    )

    total = passed + failed
    meta = {
        "team":          config["team_name"],
        "suite":         config["suite_name"],
        "build_no":      n,
        "timestamp":     run_dt.isoformat(),
        "total":         total,
        "passed":        passed,
        "failed":        failed,
        "pass_rate_pct": round(passed / total * 100, 1),
        "environment":   "staging",
        "executor":      f"jenkins-agent-{rng.randint(1,9):02d}",
    }

    return full_xml, meta


def generate(config):
    rng = random.Random(config["seed"])
    out = config["output_dir"]
    num = config["num_runs"]

    os.makedirs(out, exist_ok=True)

    for n in range(1, num + 1):
        folder = os.path.join(out, f"{config['team_name']}_build_{n:03d}")
        os.makedirs(folder, exist_ok=True)

        xml, meta = build_run(n, config, rng)

        with open(os.path.join(folder, "output.xml"), "w", encoding="utf-8") as f:
            f.write(xml)

        with open(os.path.join(folder, "ci_metadata.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        if n % 10 == 0:
            print(f"  Generated run {n:3d}/{num}  pass={meta['passed']:2d}  fail={meta['failed']:2d}  "
                  f"pass_rate={meta['pass_rate_pct']}%")

    print(f"\nDone — {num} runs written to {out}/")


def parse_args():
    p = argparse.ArgumentParser(description="Phase 1 Synthetic Log Generator")
    p.add_argument("--output-dir",  default=DEFAULT_CONFIG["output_dir"])
    p.add_argument("--num-runs",    type=int,   default=DEFAULT_CONFIG["num_runs"])
    p.add_argument("--start-date",  default=DEFAULT_CONFIG["start_date"])
    p.add_argument("--interval",    type=int,   default=DEFAULT_CONFIG["interval_hours"],
                   dest="interval_hours")
    p.add_argument("--seed",        type=int,   default=DEFAULT_CONFIG["seed"])
    p.add_argument("--team",        default=DEFAULT_CONFIG["team_name"])
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "output_dir":     args.output_dir,
        "num_runs":       args.num_runs,
        "start_date":     args.start_date,
        "interval_hours": args.interval_hours,
        "seed":           args.seed,
        "team_name":      args.team,
    })
    print(f"Generating {cfg['num_runs']} runs → {cfg['output_dir']}/")
    generate(cfg)