"""
Usage:
    python generate.py
    python generate.py --output-dir ./runs --num-runs 100
    python generate.py --seed 42  # for reproducible output
"""

import os
import json
import random
import argparse
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET


# all runtime parameters in one place, no magic numbers in logic below
CONFIG = {
    "team_name":         "TeamAlpha",
    "suite_name":        "Suite_Regression_TeamAlpha",
    "num_runs":          100,
    "anomaly_runs":      [36, 37],   # these runs get a forced low pass rate
    "anomaly_pass_rate": 0.27,
    "start_date":        "2024-10-01",
    "interval_hours":    24,
    "output_dir":        "./runs",
    "seed":              None,       # set an int for reproducible output
}


# iteration order here determines test IDs (t1..t20), so don't reorder
TEST_CATEGORIES = {
    # stable tests follow the run-level pass rate
    "TC_Login_ValidCredentials":   "stable",
    "TC_Login_InvalidPassword":    "stable",
    "TC_Login_SessionTimeout":     "stable",
    "TC_Login_AccountLockout":     "stable",
    "TC_Dashboard_FilterByDate":   "stable",
    "TC_Dashboard_Pagination":     "stable",
    "TC_Dashboard_ExportChart":    "stable",
    "TC_Dashboard_SearchBar":      "stable",
    "TC_User_CreateAccount":       "stable",
    "TC_User_EditProfile":         "stable",
    "TC_User_DeleteAccount":       "stable",
    "TC_User_PasswordReset":       "stable",
    # flaky tests roll their own probability, ignoring the run pass rate
    "TC_Login_SSORedirect":        ("flaky", 0.35),   # mild
    "TC_Login_MFAVerification":    ("flaky", 0.30),   # mild
    "TC_Dashboard_LoadWidget":     ("flaky", 0.50),   # moderate
    "TC_Dashboard_RefreshData":    ("flaky", 0.55),   # moderate
    "TC_User_BulkImport":          ("flaky", 0.65),   # heavy
    # consistently failing - same mechanism as flaky but high fail rate
    "TC_User_RoleAssignment":      ("consistent", 0.80),
    "TC_User_BatchExport":         ("consistent", 0.75),
    "TC_Login_OAuthCallback":      ("consistent", 0.70),
}

TEST_TAGS = {
    "TC_Login_ValidCredentials":   ("feature_login",     "priority_high"),
    "TC_Login_InvalidPassword":    ("feature_login",     "priority_high"),
    "TC_Login_SessionTimeout":     ("feature_login",     "priority_high"),
    "TC_Login_AccountLockout":     ("feature_login",     "priority_medium"),
    "TC_Dashboard_FilterByDate":   ("feature_dashboard", "priority_medium"),
    "TC_Dashboard_Pagination":     ("feature_dashboard", "priority_medium"),
    "TC_Dashboard_ExportChart":    ("feature_dashboard", "priority_medium"),
    "TC_Dashboard_SearchBar":      ("feature_dashboard", "priority_medium"),
    "TC_User_CreateAccount":       ("feature_usermgmt",  "priority_high"),
    "TC_User_EditProfile":         ("feature_usermgmt",  "priority_medium"),
    "TC_User_DeleteAccount":       ("feature_usermgmt",  "priority_high"),
    "TC_User_PasswordReset":       ("feature_usermgmt",  "priority_medium"),
    "TC_Login_SSORedirect":        ("feature_login",     "priority_high"),
    "TC_Login_MFAVerification":    ("feature_login",     "priority_high"),
    "TC_Dashboard_LoadWidget":     ("feature_dashboard", "priority_medium"),
    "TC_Dashboard_RefreshData":    ("feature_dashboard", "priority_medium"),
    "TC_User_BulkImport":          ("feature_usermgmt",  "priority_medium"),
    "TC_User_RoleAssignment":      ("feature_usermgmt",  "priority_high"),
    "TC_User_BatchExport":         ("feature_usermgmt",  "priority_medium"),
    "TC_Login_OAuthCallback":      ("feature_login",     "priority_high"),
}

# (primary_type, secondary_type, prob_of_primary)
# mixed types keep all 4 failure categories roughly balanced across runs
FAILURE_CONFIG = {
    "TC_Login_SSORedirect":        ("timeout",      "element",   0.70),
    "TC_Login_MFAVerification":    ("timeout",      "assertion", 0.70),
    "TC_Dashboard_LoadWidget":     ("element",      "timeout",   0.80),
    "TC_Dashboard_RefreshData":    ("assertion",    "data",      0.60),
    "TC_User_BulkImport":          ("data",         "assertion", 0.70),
    "TC_User_RoleAssignment":      ("assertion",    "data",      0.65),
    "TC_User_BatchExport":         ("data",         "element",   0.65),
    "TC_Login_OAuthCallback":      ("timeout",      "element",   0.70),
    # stable tests only fail on anomaly runs, always environment type
    "TC_Login_ValidCredentials":   ("environment",  "environment", 1.00),
    "TC_Login_InvalidPassword":    ("environment",  "environment", 1.00),
    "TC_Login_SessionTimeout":     ("environment",  "environment", 1.00),
    "TC_Login_AccountLockout":     ("environment",  "environment", 1.00),
    "TC_Dashboard_FilterByDate":   ("environment",  "environment", 1.00),
    "TC_Dashboard_Pagination":     ("environment",  "environment", 1.00),
    "TC_Dashboard_ExportChart":    ("environment",  "environment", 1.00),
    "TC_Dashboard_SearchBar":      ("environment",  "environment", 1.00),
    "TC_User_CreateAccount":       ("environment",  "environment", 1.00),
    "TC_User_EditProfile":         ("environment",  "environment", 1.00),
    "TC_User_DeleteAccount":       ("environment",  "environment", 1.00),
    "TC_User_PasswordReset":       ("environment",  "environment", 1.00),
}

_FAILURE_KW_NAME = {
    "timeout":     "Wait Until Element Is Visible",
    "element":     "Click Element",
    "assertion":   "Should Be Equal As Integers",
    "data":        "Should Not Be Empty",
    "environment": "Environment_Setup",
}

# each lambda returns (message_string, keyword_name)
FAILURE_MESSAGES = {
    "timeout": lambda: (
        "Element '{}' still visible after {}s timeout".format(
            random.choice(["loading-spinner", "overlay-modal", "progress-bar",
                           "auth-redirect", "session-token"]),
            random.choice([15, 20, 30, 45]),
        ),
        _FAILURE_KW_NAME["timeout"],
    ),
    "element": lambda: (
        "Element with locator '{}' not found after {} retries".format(
            random.choice(["id=widget-container", "id=submit-btn", "css=.data-grid",
                           "id=modal-confirm", "css=.nav-item"]),
            random.choice([3, 5, 7]),
        ),
        _FAILURE_KW_NAME["element"],
    ),
    "assertion": lambda: (
        (lambda p: "Expected HTTP status '{}' but got '{}' \u2014 {}".format(*p))(
            random.choice([
                ("200", "500", "Internal Server Error"),
                ("200", "404", "Not Found"),
                ("201", "400", "Bad Request"),
                ("200", "503", "Service Unavailable"),
            ])
        ),
        _FAILURE_KW_NAME["assertion"],
    ),
    "data": lambda: (
        "CSV export contained {} rows \u2014 expected at least {} records for {}".format(
            random.choice([0, 1, 2]),
            random.choice([50, 100, 200]),
            random.choice(["Oct 2024", "last 30 days", "Q4 2024", "last 7 days"]),
        ),
        _FAILURE_KW_NAME["data"],
    ),
    "environment": lambda: (
        random.choice([
            "Connection refused \u2014 test environment unreachable",
            "Timeout waiting for application server to respond",
            "Suite setup failed \u2014 environment health check failed",
            "Unable to launch browser \u2014 infrastructure error",
        ]),
        _FAILURE_KW_NAME["environment"],
    ),
}


# --- core logic ---

def get_pass_rate(n, config):
    # anomaly runs get a hard override, not a curve value
    if n in config["anomaly_runs"]:
        return config["anomaly_pass_rate"]
    if n <= 25:  return random.uniform(0.60, 0.70)  # early instability
    if n <= 35:  return random.uniform(0.45, 0.65)  # declining
    if n <= 45:  return random.uniform(0.50, 0.55)  # partial recovery (38-45, 36-37 already returned)
    if n <= 75:  return random.uniform(0.55, 0.80)  # recovery
    return random.uniform(0.82, 0.95)               # stable


def test_passes(test_name, pass_rate):
    category = TEST_CATEGORIES[test_name]
    if category == "stable":
        return random.random() < pass_rate
    # flaky/consistent: outcome is independent of run health
    fail_prob = category[1]
    return random.random() >= fail_prob


def get_failure(test_name, n, config):
    category = TEST_CATEGORIES[test_name]
    if category == "stable":
        if n in config["anomaly_runs"]:
            return FAILURE_MESSAGES["environment"]()
        # stable test failed outside anomaly run, use a random app-level type
        # so environment messages stay exclusive to anomaly runs
        app_type = random.choice(["timeout", "element", "assertion", "data"])
        return FAILURE_MESSAGES[app_type]()
    primary, secondary, prob = FAILURE_CONFIG[test_name]
    failure_type = primary if random.random() < prob else secondary
    return FAILURE_MESSAGES[failure_type]()


def _base_duration(test_name, n):
    # progressive drift - gets slower over time, needed for ML4
    if test_name == "TC_User_BulkImport":
        if n <= 40:   return random.uniform(10, 14)
        elif n <= 65: return random.uniform(18, 24)
        else:         return random.uniform(32, 42)  # bumped from 28-36 for clearer ML4 signal

    # step change at run 50
    if test_name == "TC_Dashboard_ExportChart":
        return random.uniform(3, 5) if n <= 50 else random.uniform(12, 15)

    # seasonal alternation - alternates even/odd
    if test_name == "TC_Login_ValidCredentials":
        return random.uniform(2.0, 3.5) if n % 2 == 0 else random.uniform(4.5, 6.5)

    return random.uniform(1.2, 8.5)


def get_duration(test_name, n, status):
    base = _base_duration(test_name, n)
    if status == "FAIL":
        return base + random.uniform(5, 15)  # timeout/retry overhead
    return base


# --- XML builder ---

RF_FMT = "%Y%m%d %H:%M:%S.%f"


def _ts(dt):
    return dt.strftime(RF_FMT)[:-3]  # trim to milliseconds


def _indent(elem, level=0):
    pad = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        for child in elem:
            _indent(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = pad + "  "
        if not child.tail or not child.tail.strip():
            child.tail = pad
    else:
        # don't overwrite text content (failure messages live here)
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = pad


def build_xml(run_id, run_time, tests_data, config):
    root = ET.Element("robot")
    root.set("generator", "Robot 6.1.1 (Python 3.10.12)")
    root.set("generated", _ts(run_time))
    root.set("rpa", "FALSE")
    root.set("schemaversion", "4")

    suite = ET.SubElement(root, "suite")
    suite.set("id", "s1")
    suite.set("name", config["suite_name"])
    suite.set("source", "/opt/ci/tests/alpha/{}.robot".format(config["suite_name"]))

    kw_setup = ET.SubElement(suite, "kw")
    kw_setup.set("name", "Suite Setup")
    kw_setup.set("type", "setup")

    setup_msg = ET.SubElement(kw_setup, "msg")
    setup_msg.set("timestamp", _ts(run_time))
    setup_msg.set("level", "INFO")
    setup_msg.text = "Suite {} initialized \u2014 team {}".format(
        config["suite_name"], config["team_name"]
    )

    setup_end = run_time + timedelta(milliseconds=110)
    setup_status = ET.SubElement(kw_setup, "status")
    setup_status.set("status", "PASS")
    setup_status.set("starttime", _ts(run_time))
    setup_status.set("endtime", _ts(setup_end))

    passed = 0
    failed = 0
    suite_end_time = run_time

    for idx, td in enumerate(tests_data, start=1):
        test_name = td["name"]
        status    = td["status"]
        t_start   = td["starttime"]
        t_end     = td["endtime"]
        feat_tag, prio_tag = TEST_TAGS[test_name]

        test_el = ET.SubElement(suite, "test")
        test_el.set("id", "s1-t{}".format(idx))
        test_el.set("name", test_name)

        ET.SubElement(test_el, "tag").text = "alpha_regression"
        ET.SubElement(test_el, "tag").text = feat_tag
        ET.SubElement(test_el, "tag").text = prio_tag

        kw = ET.SubElement(test_el, "kw")
        kw.set("name", "Run Test Steps")
        kw.set("library", "SeleniumLibrary")

        info_msg = ET.SubElement(kw, "msg")
        info_msg.set("timestamp", _ts(t_start))
        info_msg.set("level", "INFO")
        info_msg.text = "Executing {}".format(test_name)

        if status == "FAIL":
            fail_msg_text = td["fail_msg"]
            fail_msg_el = ET.SubElement(kw, "msg")
            fail_msg_el.set("timestamp", _ts(t_end))
            fail_msg_el.set("level", "FAIL")
            fail_msg_el.text = fail_msg_text

        kw_status = ET.SubElement(kw, "status")
        kw_status.set("status", status)
        kw_status.set("starttime", _ts(t_start))
        kw_status.set("endtime", _ts(t_end))

        if status == "FAIL":
            fail_kw = ET.SubElement(test_el, "kw")
            fail_kw.set("name", td["fail_kw"])
            fail_kw.set("library", "BuiltIn")

            inner_msg = ET.SubElement(fail_kw, "msg")
            inner_msg.set("timestamp", _ts(t_end))
            inner_msg.set("level", "FAIL")
            inner_msg.text = fail_msg_text

            inner_kw_status = ET.SubElement(fail_kw, "status")
            inner_kw_status.set("status", "FAIL")
            inner_kw_start = t_end - timedelta(milliseconds=50)
            inner_kw_status.set("starttime", _ts(inner_kw_start))
            inner_kw_status.set("endtime", _ts(t_end))

        test_status = ET.SubElement(test_el, "status")
        test_status.set("status", status)
        test_status.set("starttime", _ts(t_start))
        test_status.set("endtime", _ts(t_end))
        if status == "FAIL":
            test_status.text = fail_msg_text

        if status == "PASS":
            passed += 1
        else:
            failed += 1

        suite_end_time = t_end

    suite_end    = suite_end_time + timedelta(milliseconds=250)
    suite_result = "FAIL" if failed > 0 else "PASS"

    suite_status = ET.SubElement(suite, "status")
    suite_status.set("status", suite_result)
    suite_status.set("starttime", _ts(run_time))
    suite_status.set("endtime", _ts(suite_end))
    suite_status.set("passed", str(passed))
    suite_status.set("failed", str(failed))

    # copy counts from suite status, don't recompute
    stats_el  = ET.SubElement(root, "statistics")
    total_el  = ET.SubElement(stats_el, "total")
    stat_all  = ET.SubElement(total_el, "stat")
    stat_all.set("pass", str(passed))
    stat_all.set("fail", str(failed))
    stat_all.text = "All Tests"

    tag_el   = ET.SubElement(stats_el, "tag")
    stat_tag = ET.SubElement(tag_el, "stat")
    stat_tag.set("pass", str(passed))
    stat_tag.set("fail", str(failed))
    stat_tag.text = "alpha_regression"

    ET.SubElement(root, "errors")

    return root, passed, failed


# --- entry point ---

def compute_tests(n, pass_rate, run_time, config):
    tests_data = []
    cursor = run_time

    for test_name in TEST_CATEGORIES:
        t_start  = cursor + timedelta(seconds=0.2)
        passes   = test_passes(test_name, pass_rate)
        status   = "PASS" if passes else "FAIL"
        duration = get_duration(test_name, n, status)
        t_end    = t_start + timedelta(seconds=duration)
        cursor   = t_end

        td = {
            "name":      test_name,
            "status":    status,
            "starttime": t_start,
            "endtime":   t_end,
        }
        if status == "FAIL":
            fail_msg, fail_kw = get_failure(test_name, n, config)
            td["fail_msg"] = fail_msg
            td["fail_kw"]  = fail_kw

        tests_data.append(td)

    return tests_data


def write_xml(folder, run_id, run_time, tests_data, config):
    root, passed, failed = build_xml(run_id, run_time, tests_data, config)
    _indent(root)
    tree = ET.ElementTree(root)
    xml_path = os.path.join(folder, "output.xml")
    tree.write(xml_path, encoding="unicode", xml_declaration=False)
    return passed, failed


def write_metadata(folder, n, run_time, passed, failed, config):
    total = passed + failed
    meta = {
        "team":          config["team_name"],
        "suite":         config["suite_name"],
        "build_no":      n,
        "timestamp":     run_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total":         total,
        "passed":        passed,
        "failed":        failed,
        "pass_rate_pct": round(passed / total * 100, 1),
        "environment":   "staging",
        "executor":      "jenkins-agent-03",
    }
    meta_path = os.path.join(folder, "ci_metadata.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)


def generate_all_runs(config):
    if config.get("seed") is not None:
        random.seed(config["seed"])

    base_time = datetime.strptime(config["start_date"], "%Y-%m-%d").replace(hour=2)
    num_runs  = config["num_runs"]

    print("Generating {} runs into '{}' ...".format(num_runs, config["output_dir"]))

    for n in range(1, num_runs + 1):
        run_id   = "{}_build_{:03d}".format(config["team_name"], n)
        folder   = os.path.join(config["output_dir"], run_id)
        run_time = base_time + timedelta(hours=config["interval_hours"] * (n - 1))

        pass_rate = get_pass_rate(n, config)
        os.makedirs(folder, exist_ok=True)

        tests_data     = compute_tests(n, pass_rate, run_time, config)
        passed, failed = write_xml(folder, run_id, run_time, tests_data, config)
        write_metadata(folder, n, run_time, passed, failed, config)

        if n % 10 == 0 or n in config["anomaly_runs"]:
            tag = " *** ANOMALY ***" if n in config["anomaly_runs"] else ""
            print("  Run {:3d}/{} | target PR {:.0%} | actual {}/{}{}".format(
                n, num_runs, pass_rate, passed, passed + failed, tag))

    print("Done. {} run folders written to '{}'.".format(num_runs, config["output_dir"]))


def parse_args():
    p = argparse.ArgumentParser(description="Phase 1 Synthetic CI Log Generator")
    p.add_argument("--output-dir", default=CONFIG["output_dir"])
    p.add_argument("--num-runs",   type=int, default=CONFIG["num_runs"])
    p.add_argument("--start-date", default=CONFIG["start_date"])
    p.add_argument("--interval",   type=int, default=CONFIG["interval_hours"], dest="interval_hours")
    p.add_argument("--seed",       type=int, default=CONFIG["seed"])
    p.add_argument("--team",       default=CONFIG["team_name"])
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = dict(CONFIG)
    cfg.update({
        "output_dir":     args.output_dir,
        "num_runs":       args.num_runs,
        "start_date":     args.start_date,
        "interval_hours": args.interval_hours,
        "seed":           args.seed,
        "team_name":      args.team,
    })
    generate_all_runs(cfg)