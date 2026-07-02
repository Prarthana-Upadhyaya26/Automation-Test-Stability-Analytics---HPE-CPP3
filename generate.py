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
    targets = ["loading-spinner", "overlay-modal", "progress-bar", "auth-redirect", "session-token"]
    timeouts = [15, 20, 30, 45]
    templates = [
        "Timeout failure — waiting for '{target}' exceeded {timeout}s",
        "Timeout failure — service request stalled beyond {timeout}s",
        "Timeout failure — '{target}' blocked completion for {timeout}s",
    ]
    return rng.choice(templates).format(target=rng.choice(targets), timeout=rng.choice(timeouts))

def gen_element_msg(rng):
    locators = ["id=widget-container", "id=submit-btn", "css=.data-grid", "id=modal-confirm", "css=.nav-item"]
    retries = [3, 5, 7]
    templates = [
        "Element not found — locator '{locator}' missing from DOM after {retries} retries",
        "Element not found — UI locator '{locator}' could not be resolved after {retries} retries",
        "Element not found — page DOM changed and '{locator}' was absent after {retries} retries",
    ]
    return rng.choice(templates).format(locator=rng.choice(locators), retries=rng.choice(retries))

def gen_assertion_msg(rng):
    pairs = [
        ("200", "500", "Internal Server Error"),
        ("200", "404", "Not Found"),
        ("201", "400", "Bad Request"),
        ("200", "503", "Service Unavailable"),
    ]
    exp, got, desc = rng.choice(pairs)
    templates = [
        "HTTP assertion failed — expected status '{exp}' but got '{got}' ({desc})",
        "HTTP assertion failure — request returned '{got}' instead of expected '{exp}' ({desc})",
        "HTTP assertion failure — API contract mismatch, got '{got}' but expected '{exp}' ({desc})",
    ]
    return rng.choice(templates).format(exp=exp, got=got, desc=desc)

def gen_data_msg(rng):
    rows = [0, 1, 2]
    mins = [50, 100, 200]
    ranges = ["Oct 2024", "last 30 days", "Q4 2024", "last 7 days"]
    templates = [
        "Data validation failure — CSV export contained {rows} rows but expected at least {mins} records for {range}",
        "Data validation failure — fixture data mismatch, found {rows} rows instead of at least {mins} for {range}",
        "Data validation failure — export/import data did not match expected {mins} records for {range}",
    ]
    return rng.choice(templates).format(rows=rng.choice(rows), mins=rng.choice(mins), range=rng.choice(ranges))

def gen_environment_msg(rng):
    messages = [
        "Environment/setup failure — test environment unreachable",
        "Environment/setup failure — staging health check failed",
        "Environment/setup failure — unable to provision CI agent",
        "Environment/setup failure — infrastructure error prevented browser launch",
    ]
    return rng.choice(messages)

FAIL_GEN = {
    "timeout":   gen_timeout_msg,
    "element":   gen_element_msg,
    "assertion": gen_assertion_msg,
    "data":      gen_data_msg,
    "environment": gen_environment_msg,
}

# keyword names used in inner <kw> for each failure type
FAIL_KW = {
    "timeout":   "Wait Until Element Is Visible",
    "element":   "Click Element",
    "assertion": "Should Be Equal As Integers",
    "data":      "Should Not Be Empty",
    "environment": "Environment_Setup",
}

# Temporal failure waves — cluster categories around build ranges for ML-3.
# strength controls how often the wave overrides the test's natural primary/secondary mix;
# the remainder stays scattered so the distribution does not look artificial.
FAILURE_WAVES = [
    {
        "start": 34, "end": 37,
        "dominant": "environment",
        "strength": 0.82,
        "types": {"environment", "timeout"},
    },
    {
        "start": 22, "end": 33,
        "dominant": "element",
        "strength": 0.74,
        "types": {"element", "timeout"},
    },
    {
        "start": 31, "end": 44,
        "dominant": "assertion",
        "strength": 0.68,
        "types": {"assertion", "data"},
    },
    {
        "start": 33, "end": 46,
        "dominant": "timeout",
        "strength": 0.70,
        "types": {"timeout", "environment"},
    },
    {
        "start": 50, "end": 63,
        "dominant": "data",
        "strength": 0.60,
        "types": {"data", "assertion"},
    },
]


def pick_failure_type(category, primary, secondary, prim_prob, n, is_anomaly, rng):
    """Choose a failure family, with optional temporal clustering by build number."""
    if category == "stable":
        return "environment"

    if prim_prob is None:
        base = primary or "timeout"
    else:
        base = primary if rng.random() < prim_prob else secondary

    return apply_temporal_failure_bias(n, base, primary, secondary, is_anomaly, rng)


def apply_temporal_failure_bias(n, base_ftype, primary, secondary, is_anomaly, rng):
    """Nudge failure type toward active temporal waves; leave a natural scatter tail."""
    candidates = {t for t in (primary, secondary, base_ftype) if t}

    if is_anomaly and rng.random() < 0.30:
        return rng.choice(["environment", "timeout"])

    for wave in FAILURE_WAVES:
        if not (wave["start"] <= n <= wave["end"]):
            continue
        applicable = candidates & wave["types"]
        if wave["dominant"] not in candidates and not applicable:
            continue
        if rng.random() >= wave["strength"]:
            continue
        if wave["dominant"] in candidates:
            return wave["dominant"]
        if applicable:
            return rng.choice(list(applicable))

    return base_ftype


# PASS RATE CURVE  (applies to stable + consistently_failing; flaky uses own prob)
def run_pass_rate(n, anomaly_runs, anomaly_pass_rate):
    """Return the suite-level pass-rate target for run n (1-indexed)."""
    if n in anomaly_runs:
        return anomaly_pass_rate
    if   1  <= n <= 25: return random.uniform(0.70, 0.80)
    elif 26 <= n <= 35: return random.uniform(0.65, 0.72)
    elif 38 <= n <= 45: return random.uniform(0.60, 0.65)
    elif 46 <= n <= 75: return random.uniform(0.65, 0.80)
    else:               return random.uniform(0.82, 0.95)


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
        ftype = pick_failure_type(category, primary, secondary, prim_prob, n, is_anomaly, rng)

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

    target_pass_rate = run_pass_rate(n, config["anomaly_runs"], config["anomaly_pass_rate"])
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
        "team":          config["team_name"],
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
