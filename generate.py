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
import xml.etree.ElementTree as ET

from config import DEFAULT_CONFIG, TESTS, DEPENDENCIES

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

def gen_environment_msg(rng):
    messages = [
        "Connection refused — test environment unreachable",
        "Timeout waiting for application server to respond",
        "Suite setup failed — environment health check failed",
        "Unable to launch browser — infrastructure error",
    ]
    return rng.choice(messages)

FAIL_GEN = {
    "timeout":   gen_timeout_msg,
    "element":   gen_element_msg,
    "assertion": gen_assertion_msg,
    "data":      gen_data_msg,
    "environment": gen_environment_msg
}

# keyword names used in inner <kw> for each failure type
FAIL_KW = {
    "timeout":   "Wait Until Element Is Visible",
    "element":   "Click Element",
    "assertion": "Should Be Equal As Integers",
    "data":      "Should Not Be Empty",
    "environment": "Environment_Setup"
}

# PASS RATE CURVE  (applies to stable + consistently_failing; flaky uses own prob)
def run_pass_rate(n, anomaly_runs, anomaly_pass_rate, rng):
    """Return the suite-level pass-rate target for run n (1-indexed)."""
    if n in anomaly_runs:
        return anomaly_pass_rate
    if   1  <= n <= 25: return rng.uniform(0.70, 0.80)
    elif 26 <= n <= 35: return rng.uniform(0.65, 0.72)
    elif 38 <= n <= 45: return rng.uniform(0.60, 0.65)
    elif 46 <= n <= 75: return rng.uniform(0.65, 0.80)
    else:               return rng.uniform(0.82, 0.95)


def get_program_name(n, total_runs):
    """Return program name for run n based on proportional alpha/beta/gamma split."""
    alpha_count = max(1, round(total_runs * 0.20))
    beta_count = max(1, round(total_runs * 0.30))
    if alpha_count + beta_count >= total_runs:
        beta_count = max(1, total_runs - alpha_count)
    alpha_threshold = alpha_count
    beta_threshold = alpha_count + beta_count

    if n <= alpha_threshold:
        return "alpha"
    elif n <= beta_threshold:
        return "beta"
    return "gamma"

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

# DECIDE TEST OUTCOME
def decide_outcome(category, fail_prob, is_anomaly, rng):
    """Return True if test passes."""
    if is_anomaly:
        if category == "stable":
            return rng.random() > 0.80
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
def _indent(elem, level=0):
    """Add pretty-print whitespace to an ET element tree in-place."""
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
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = pad


def build_test_xml(test, passed, n, is_anomaly, current_dt, rng, force_fail=False, force_pass=False):
    tid, name, feature_tag, priority_tag, category, fail_prob, _, primary, secondary, prim_prob = test

    if force_fail:
        status = "FAIL"
    elif force_pass:
        status = "PASS"
    else:
        status = "PASS" if passed else "FAIL"

    dur = test_duration(name, n, status, rng)
    start_dt = current_dt
    end_dt   = start_dt + timedelta(seconds=dur)

    start_s  = fmt_ts(start_dt)
    end_s    = fmt_ts(end_dt)
    info_ts  = fmt_ts(start_dt + timedelta(milliseconds=200))

    test_el = ET.Element("test")
    test_el.set("id", tid)
    test_el.set("name", name)

    ET.SubElement(test_el, "tag").text = "alpha_regression"
    ET.SubElement(test_el, "tag").text = feature_tag
    ET.SubElement(test_el, "tag").text = priority_tag

    kw = ET.SubElement(test_el, "kw")
    kw.set("name", "Run Test Steps")
    kw.set("library", "SeleniumLibrary")

    info_msg = ET.SubElement(kw, "msg")
    info_msg.set("timestamp", info_ts)
    info_msg.set("level", "INFO")
    info_msg.text = f"Executing {name}"

    if status == "PASS":
        kw_status = ET.SubElement(kw, "status")
        kw_status.set("status", "PASS")
        kw_status.set("starttime", start_s)
        kw_status.set("endtime", end_s)

        outer_status = ET.SubElement(test_el, "status")
        outer_status.set("status", "PASS")
        outer_status.set("starttime", start_s)
        outer_status.set("endtime", end_s)
    else:
        # duration of failure outer and inner kw is determined randomly within a range
        # to create more realistic variability in the logs
        if category == "stable":
            ftype = "environment"
        elif prim_prob is None:
            ftype = primary or "timeout"
        else:
            ftype = primary if rng.random() < prim_prob else secondary

        fail_msg       = FAIL_GEN[ftype](rng)
        fail_ts        = fmt_ts(end_dt - timedelta(milliseconds=rng.randint(5, 50)))
        inner_kw_name  = FAIL_KW[ftype]
        inner_start    = fmt_ts(end_dt - timedelta(milliseconds=rng.randint(50, 100)))

        fail_msg_el = ET.SubElement(kw, "msg")
        fail_msg_el.set("timestamp", fail_ts)
        fail_msg_el.set("level", "FAIL")
        fail_msg_el.text = fail_msg

        kw_status = ET.SubElement(kw, "status")
        kw_status.set("status", "FAIL")
        kw_status.set("starttime", start_s)
        kw_status.set("endtime", end_s)

        inner_kw = ET.SubElement(test_el, "kw")
        inner_kw.set("name", inner_kw_name)
        inner_kw.set("library", "BuiltIn")

        inner_msg = ET.SubElement(inner_kw, "msg")
        inner_msg.set("timestamp", fail_ts)
        inner_msg.set("level", "FAIL")
        inner_msg.text = fail_msg

        inner_status = ET.SubElement(inner_kw, "status")
        inner_status.set("status", "FAIL")
        inner_status.set("starttime", inner_start)
        inner_status.set("endtime", fail_ts)

        outer_status = ET.SubElement(test_el, "status")
        outer_status.set("status", "FAIL")
        outer_status.set("starttime", start_s)
        outer_status.set("endtime", end_s)
        outer_status.text = fail_msg

    return test_el, status, end_dt


def build_run(n, config, rng):

    #-----------------------------
    run_dt = datetime.fromisoformat(config["start_date"]) + timedelta(hours=config["interval_hours"] * (n - 1))
    is_anomaly = n in config["anomaly_runs"]

    generated_s = fmt_ts(run_dt)
    suite_start  = run_dt
    #------------------------------

    # Root element
    root = ET.Element("robot")
    root.set("generator", "Robot 6.1.1 (Python 3.10.12)")
    root.set("generated", generated_s)
    root.set("rpa", "FALSE")
    root.set("schemaversion", "4")

    # Determine program and suite source by run number
    program = get_program_name(n, config["num_runs"])

    # Suite element
    suite = ET.SubElement(root, "suite")
    suite.set("id", "s1")
    suite.set("name", program)
    suite.set("source", f"/opt/ci/tests/{program}/{program}.robot")

    # Suite setup kw
    setup_end = suite_start + timedelta(milliseconds=110)
    kw_setup = ET.SubElement(suite, "kw")
    kw_setup.set("name", "Suite Setup")
    kw_setup.set("type", "setup")

    setup_msg = ET.SubElement(kw_setup, "msg")
    setup_msg.set("timestamp", generated_s)
    setup_msg.set("level", "INFO")
    setup_msg.text = f"Suite {program} initialized — team {config['team_name']}"

    setup_status = ET.SubElement(kw_setup, "status")
    setup_status.set("status", "PASS")
    setup_status.set("starttime", generated_s)
    setup_status.set("endtime", fmt_ts(setup_end))

    cursor = setup_end + timedelta(milliseconds=200)
    passed = 0
    failed = 0

    target_pass_rate = run_pass_rate(n, config["anomaly_runs"], config["anomaly_pass_rate"], rng)
    total_tests = len(TESTS)
    target_failures = round(total_tests * (1 - target_pass_rate))
    results = []
    natural_outcomes = {}   # name -> bool (True = pass), built as we go for dependency lookups

    for test in TESTS:
        tid, name, feature_tag, priority_tag, category, fail_prob, *_ = test

        # Apply dependency risk model before rolling the outcome.
        # If any upstream test already failed this run, raise the effective
        # fail_prob via the multiplicative model then clamp to 0.95.
        dep_info = DEPENDENCIES.get(name)
        if dep_info:
            failed_dep_count = sum(
                1 for dep in dep_info["deps"]
                if natural_outcomes.get(dep) is False
            )
            if failed_dep_count > 0:
                fail_prob = 1 - (1 - fail_prob) * (1 - dep_info["weight"] * failed_dep_count)
                fail_prob = min(fail_prob, 0.95)

        outcome = decide_outcome(category, fail_prob, is_anomaly, rng)
        natural_outcomes[name] = outcome
        results.append((test, outcome))

    natural_failures = sum(1 for _, outcome in results if not outcome)

    # --- Bidirectional correction to hit target_pass_rate ---
    extra_failures_needed = target_failures - natural_failures

    force_fail_indices = set()
    force_pass_indices = set()

    if extra_failures_needed > 0:
        # Too many passes — force some non-stable passing tests to fail
        candidates = [
            i for i, (test, outcome) in enumerate(results)
            if outcome and test[4] != "stable"
        ]
        n_force = min(extra_failures_needed, len(candidates))
        force_fail_indices = set(rng.sample(candidates, n_force))

    elif extra_failures_needed < 0:
        # Too many failures — force some failing tests to pass
        # Prefer flaky tests (more believable they recovered) over consistently_failing
        extra_passes_needed = -extra_failures_needed
        candidates_flaky = [
            i for i, (test, outcome) in enumerate(results)
            if not outcome and test[4] in ("flaky-mild", "flaky-moderate", "flaky-heavy")
        ]
        candidates_cf = [
            i for i, (test, outcome) in enumerate(results)
            if not outcome and test[4] == "consistently_failing"
        ]
        # Fill from flaky first, then consistently_failing if still needed
        chosen = []
        for pool in (candidates_flaky, candidates_cf):
            still_needed = extra_passes_needed - len(chosen)
            if still_needed <= 0:
                break
            chosen += rng.sample(pool, min(still_needed, len(pool)))
        force_pass_indices = set(chosen)

    for i, (test, outcome) in enumerate(results):
        force_fail = i in force_fail_indices
        force_pass = i in force_pass_indices
        test_el, status, cursor = build_test_xml(
            test, outcome, n, is_anomaly, cursor, rng,
            force_fail=force_fail, force_pass=force_pass
        )
        suite.append(test_el)
        cursor += timedelta(milliseconds=rng.randint(100, 300))
        if status == "PASS":
            passed += 1
        else:
            failed += 1

    suite_result = "FAIL" if failed > 0 else "PASS"
    suite_status_el = ET.SubElement(suite, "status")
    suite_status_el.set("status", suite_result)
    suite_status_el.set("starttime", generated_s)
    suite_status_el.set("endtime", fmt_ts(cursor))
    suite_status_el.set("passed", str(passed))
    suite_status_el.set("failed", str(failed))

    # Statistics
    stats_el = ET.SubElement(root, "statistics")
    total_el = ET.SubElement(stats_el, "total")
    stat_all = ET.SubElement(total_el, "stat")
    stat_all.set("pass", str(passed))
    stat_all.set("fail", str(failed))
    stat_all.text = "All Tests"

    tag_el   = ET.SubElement(stats_el, "tag")
    stat_tag = ET.SubElement(tag_el, "stat")
    stat_tag.set("pass", str(passed))
    stat_tag.set("fail", str(failed))
    stat_tag.text = "alpha_regression"

    ET.SubElement(root, "errors")

    total = passed + failed
    meta = {
        "team":          f"Team{program.capitalize()}",
        "suite":         program,
        "program":       program,
        "build_no":      n,
        "timestamp":     run_dt.isoformat(),
        "total":         total,
        "passed":        passed,
        "failed":        failed,
        "pass_rate_pct": round(passed / total * 100, 1),
        "environment":   "staging",
        "executor":      f"jenkins-agent-{rng.randint(1,9):02d}",
    }

    return root, meta


# MAIN─
def generate(config):
    rng = random.Random(config["seed"])
    out = config["output_dir"]
    num = config["num_runs"]

    os.makedirs(out, exist_ok=True)

    for n in range(1, num + 1):
        program = get_program_name(n, num)
        folder = os.path.join(out, f"{program}_build_{n:03d}")
        os.makedirs(folder, exist_ok=True)

        xml, meta = build_run(n, config, rng)

        _indent(xml)
        tree = ET.ElementTree(xml)
        with open(os.path.join(folder, "output.xml"), "w", encoding="utf-8") as f:
            tree.write(f, encoding="unicode", xml_declaration=False)

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
