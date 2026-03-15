
"""
Usage:
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

# keyword names used in inner <kw> for each failure type
FAIL_KW = {
    "timeout":   "Wait Until Element Is Visible",
    "element":   "Click Element",
    "assertion": "Should Be Equal As Integers",
    "data":      "Should Not Be Empty",
}

# PASS RATE CURVE  (applies to stable + consistently_failing; flaky uses own prob)
def run_pass_rate(n, anomaly_runs, anomaly_pass_rate):
    """Return the suite-level pass-rate target for run n (1-indexed)."""
    if n in anomaly_runs:
        return anomaly_pass_rate
    if   1  <= n <= 25: return random.uniform(0.60, 0.70)
    elif 26 <= n <= 35: return random.uniform(0.55, 0.65)
    elif 38 <= n <= 45: return random.uniform(0.50, 0.55)
    elif 46 <= n <= 75: return random.uniform(0.55, 0.80)
    else:               return random.uniform(0.82, 0.95)

# DURATION PATTERNS
def base_duration(test_name, n, rng):
    if test_name == "TC_Login_ValidCredentials":
        # seasonal
        if n % 2 == 0:
            return rng.uniform(2.0, 3.5)
        else:
            return rng.uniform(4.5, 6.5)

    if test_name == "TC_Dashboard_ExportChart":
        # step change at run 50
        if n <= 50:
            return rng.uniform(3.0, 5.0)
        else:
            return rng.uniform(12.0, 15.0)

    if test_name == "TC_User_BulkImport":
        # progressive drift
        if n <= 40:
            return rng.uniform(10.0, 14.0)
        elif n <= 65:
            return rng.uniform(18.0, 24.0)
        else:
            return rng.uniform(28.0, 36.0)

    return rng.uniform(1.2, 8.5)

def test_duration(test_name, n, status, rng):
    d = base_duration(test_name, n, rng)
    if status == "FAIL":
        d += rng.uniform(5.0, 15.0)
    return round(d, 3)

# TIMESTAMP HELPERS
def fmt_ts(dt):
    """Format datetime as Robot Framework timestamp string."""
    return dt.strftime("%Y%m%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

#-------------need to improve since not implementing the probablity of failure for flaky tests correctly, currently just adding a flat 30% to the fail_prob which is not ideal, should be using the run_pass_rate function to determine the overall pass rate for the run and then adjusting the fail_prob for flaky tests accordingly to meet that target pass rate while also respecting the relative failure rates of each test. This is a bit more complex but would produce more realistic data. Also Differentiate between flaky-mild, flaky-moderate, and flaky-heavy in how much they deviate from their base fail_prob, rather than using the same adjustment for all flaky categories.

# DECIDE TEST OUTCOME
def decide_outcome(category, fail_prob, is_anomaly, rng):
    """Return True if test passes."""
    if is_anomaly:
        if category == "stable":
            return rng.random() > 0.60
        if category in ("flaky-mild", "flaky-moderate", "flaky-heavy"):
            return rng.random() > min(fail_prob + 0.30, 0.95)
        if category == "consistently_failing":
            return rng.random() > min(fail_prob + 0.15, 0.99)
        return True

    if category == "stable":
        return True
    if category in ("flaky-mild", "flaky-moderate", "flaky-heavy"):
        return rng.random() > fail_prob
    if category == "consistently_failing":
        return rng.random() > fail_prob
    return True

#--------------

# XML BUILDERS
def build_test_xml(test, n, is_anomaly, current_dt, rng):
    tid, name, feature_tag, priority_tag, category, fail_prob, _, primary, secondary, prim_prob = test

    status = "PASS" if decide_outcome(category, fail_prob, is_anomaly, rng) else "FAIL"

    dur = test_duration(name, n, status, rng)
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
        # duration of faliure other and inner kw is determined randomly within a range to create more realistic variability in the logs, rather than having all failures occur at the same point in time.
        if prim_prob is None:
            ftype = primary or "timeout"
        else:
            ftype = primary if rng.random() < prim_prob else secondary
        fail_msg = FAIL_GEN[ftype](rng)
        fail_ts  = fmt_ts(end_dt - timedelta(milliseconds=rng.randint(5, 50)))
        inner_kw = FAIL_KW[ftype]
        inner_start = fmt_ts(end_dt - timedelta(milliseconds=rng.randint(50, 100)))

        kw_xml = (
            f'      <kw name="Run Test Steps" library="SeleniumLibrary">\n'
            f'        <msg timestamp="{info_ts}" level="INFO">Executing {name}</msg>\n'
            f'        <msg timestamp="{fail_ts}" level="FAIL">{fail_msg}</msg>\n'
            f'        <status status="FAIL" starttime="{start_s}" endtime="{end_s}"/>\n'
            f'      </kw>\n'
            f'      <kw name="{inner_kw}" library="BuiltIn">\n'
            f'        <msg timestamp="{fail_ts}" level="FAIL">{fail_msg}</msg>\n'
            f'        <status status="FAIL" starttime="{inner_start}" endtime="{fail_ts}"/>\n'
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

    #-----------------------------
    run_dt = datetime.fromisoformat(config["start_date"]) + timedelta(hours=config["interval_hours"] * (n - 1))
    is_anomaly = n in config["anomaly_runs"]

    generated_s = fmt_ts(run_dt)
    suite_start  = run_dt
    #------------------------------

    # Suite setup
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

    for test in TESTS:
        t_xml, status, cursor = build_test_xml(test, n, is_anomaly, cursor, rng)
        tests_xml += t_xml
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


# MAIN─
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
