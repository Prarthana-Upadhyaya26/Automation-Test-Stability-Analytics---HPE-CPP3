-- ============================================================
-- analytics.db — SQLite Schema
-- Automation Test Stability Analytics — Phase 2
--
-- Run once to create your database:
--   python -c "import sqlite3; conn=sqlite3.connect('analytics.db'); conn.executescript(open('schema.sql').read()); conn.close()"
--
-- Two tables:
--   runs         — one row per CI run  (from ci_metadata.json)
--   test_results — one row per test per run  (from output.xml)
--
-- ingestion_log  — tracks which runs have already been ingested
-- ============================================================

-- ── TABLE 1: runs ─────────────────────────────────────────────
-- One row per CI execution.
-- Populated from ci_metadata.json in each run folder.

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    -- e.g. "TeamAlpha_build_001"
    -- Tip: use the folder name as run_id

    team            TEXT NOT NULL,
    -- from ci_metadata.json → "team"

    suite_name      TEXT NOT NULL,
    -- from ci_metadata.json → "suite"

    job_name        TEXT,
    -- from ci_metadata.json → "job_name"

    build_no        INTEGER,
    -- from ci_metadata.json → "build_no"

    timestamp       DATETIME NOT NULL,
    -- from ci_metadata.json → "timestamp"
    -- store as ISO format: "2024-10-01T02:00:00"

    duration_s      REAL,
    -- from ci_metadata.json → "duration_s"

    total           INTEGER NOT NULL,
    -- from ci_metadata.json → "total"

    passed          INTEGER NOT NULL,
    -- from ci_metadata.json → "passed"

    failed          INTEGER NOT NULL,
    -- from ci_metadata.json → "failed"

    pass_rate_pct   REAL,
    -- from ci_metadata.json → "pass_rate_pct"
    -- or compute as: ROUND(passed * 100.0 / total, 2)

    environment     TEXT,
    -- from ci_metadata.json → "environment"

    executor        TEXT
    -- from ci_metadata.json → "executor"
);


-- ── TABLE 2: test_results ─────────────────────────────────────
-- One row per test case per run.
-- Populated by parsing output.xml in each run folder.

CREATE TABLE IF NOT EXISTS test_results (
    result_id       TEXT PRIMARY KEY,
    -- construct as: run_id + "_" + test_name
    -- e.g. "TeamAlpha_build_001_TC_Login_ValidCredentials"

    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    -- foreign key back to runs table

    suite_name      TEXT NOT NULL,
    -- from output.xml → <suite name="...">

    test_name       TEXT NOT NULL,
    -- from output.xml → <test name="...">

    status          TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
    -- from output.xml → <status status="PASS|FAIL">

    duration_s      REAL,
    -- compute from output.xml:
    --   parse starttime and endtime on <status> element
    --   subtract to get seconds
    -- timestamp format in XML: "YYYYMMDD HH:MM:SS.mmm"
    -- Python hint:
    --   from datetime import datetime
    --   fmt = "%Y%m%d %H:%M:%S.%f"
    --   duration = (datetime.strptime(endtime, fmt) -
    --               datetime.strptime(starttime, fmt)).total_seconds()

    failure_msg     TEXT,
    -- from output.xml → text content of <status> when FAIL
    -- NULL when PASS

    failure_kw      TEXT,
    -- from output.xml → name attribute of inner <kw> when FAIL
    -- this is the exact keyword that threw the error
    -- NULL when PASS

    tags            TEXT
    -- from output.xml → all <tag> elements inside <test>
    -- store as JSON array string: '["alpha_regression","feature_login"]'
    -- Python hint: import json; json.dumps(tag_list)
);


-- ── TABLE 3: ingestion_log ────────────────────────────────────
-- Tracks which run folders have already been ingested.
-- Check this before processing each folder — skip if already done.
-- This makes the pipeline incremental (only processes new runs).

CREATE TABLE IF NOT EXISTS ingestion_log (
    run_id          TEXT PRIMARY KEY,
    ingested_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    status          TEXT NOT NULL CHECK(status IN ('success', 'error')),
    error_msg       TEXT
    -- NULL on success, error description on failure
);


-- ── INDEXES ───────────────────────────────────────────────────
-- Speed up common queries used by the metrics engine and dashboard.

CREATE INDEX IF NOT EXISTS idx_runs_team
    ON runs(team);
-- used by: WHERE team = 'TeamAlpha'

CREATE INDEX IF NOT EXISTS idx_runs_timestamp
    ON runs(timestamp);
-- used by: ORDER BY timestamp, date range filters

CREATE INDEX IF NOT EXISTS idx_test_results_run_id
    ON test_results(run_id);
-- used by: JOIN runs ON test_results.run_id = runs.run_id

CREATE INDEX IF NOT EXISTS idx_test_results_test_name
    ON test_results(test_name);
-- used by: GROUP BY test_name for flakiness calculation

CREATE INDEX IF NOT EXISTS idx_test_results_status
    ON test_results(status);
-- used by: WHERE status = 'FAIL' for failure analysis


-- ── TABLE 4: jira_defects ─────────────────────────────────────
-- Raw JIRA defect import. Populated by jira_ingest.py, not pipeline.py.

CREATE TABLE IF NOT EXISTS jira_defects (
    jira_key       TEXT PRIMARY KEY,
    -- e.g. "CSSOSE-0001"

    summary        TEXT NOT NULL,
    description    TEXT,
    reporter_email TEXT,
    -- e.g. "sample.reporter@hpe.com"

    status         TEXT,
    -- Triage | In Progress | Testing | Development | Lab Review |
    -- Closed - Fixed | Closed - No Change | Duplicate

    priority       TEXT,
    -- Medium | Undecided

    issuetype      TEXT,
    -- Bug

    project        TEXT,
    -- CSSE | CSSOSE | MCIO

    labels         TEXT,
    -- JSON array e.g. '["automation","flaky","bulk-import"]'

    components     TEXT,
    -- JSON array e.g. '["OS : OS - Linux"]'

    created        DATETIME NOT NULL,
    -- ISO format: "2026-06-03T10:30:00.000+0000"

    imported_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jira_defects_created
    ON jira_defects(created);
CREATE INDEX IF NOT EXISTS idx_jira_defects_reporter
    ON jira_defects(reporter_email);
CREATE INDEX IF NOT EXISTS idx_jira_defects_project
    ON jira_defects(project);


-- ── TABLE 5: reporter_team_map ────────────────────────────────
-- Maps a tester's email to their team. More stable than executor-level
-- mapping (agents rotate per build; team membership is months-long).
-- Seed once per team member; update only on team changes.

CREATE TABLE IF NOT EXISTS reporter_team_map (
    email TEXT PRIMARY KEY,
    -- e.g. "sample.reporter@hpe.com"

    team  TEXT NOT NULL,
    -- e.g. "TeamAlpha"

    notes TEXT
    -- optional: role, active dates
);


-- ── TABLE 6: defect_test_links ────────────────────────────────
-- Candidate and confirmed mappings from a JIRA defect to a specific
-- failing test run. Append-only; never modified by pipeline.py.
--
-- Workflow:
--   jira_ingest.py inserts rows with confirmed=0 (pending).
--   High-confidence matches (score >= 70) are auto-set to confirmed=1.
--   Low-confidence matches appear in the Streamlit JIRA Review queue.
--   Humans set confirmed=1 (accept) or confirmed=-1 (reject).

CREATE TABLE IF NOT EXISTS defect_test_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,

    jira_key        TEXT NOT NULL REFERENCES jira_defects(jira_key),
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    test_name       TEXT NOT NULL,
    -- denormalized from test_results.test_name for fast filtering

    match_strategy  TEXT NOT NULL,
    -- "exact_name"  : TC_ identifier found verbatim in JIRA summary or description
    -- "label_dict"  : matched via label-to-test dictionary
    -- "keyword"     : matched via feature area keyword
    -- "semantic"    : TF-IDF cosine similarity fallback (no TC_ name found)

    confidence      INTEGER NOT NULL,
    -- 0-100, computed by jira_ingest.py scoring model

    date_delta_days INTEGER,
    -- ABS(date(defect.created) - date(run.timestamp)) in days

    cosine_sim_score REAL,
    -- cosine similarity score when match_strategy = 'semantic'; NULL otherwise

    confirmed       INTEGER NOT NULL DEFAULT 0,
    -- 0 = pending review
    -- 1 = confirmed (auto or manual)
    -- -1 = rejected

    confirmed_by    TEXT,
    confirmed_at    DATETIME,
    notes           TEXT,
    linked_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dtl_jira_key
    ON defect_test_links(jira_key);
CREATE INDEX IF NOT EXISTS idx_dtl_run_id
    ON defect_test_links(run_id);
CREATE INDEX IF NOT EXISTS idx_dtl_test_name
    ON defect_test_links(test_name);
CREATE INDEX IF NOT EXISTS idx_dtl_confirmed
    ON defect_test_links(confirmed);


-- ── SAMPLE QUERIES (for reference, not executed) ──────────────
/*

-- Q1: Pass rate for each run (TeamAlpha only, ordered by time)
SELECT run_id, timestamp, pass_rate_pct
FROM   runs
WHERE  team = 'TeamAlpha'
ORDER  BY timestamp ASC;


-- Q2: Top 5 failing tests across all runs
SELECT   test_name,
         COUNT(*)                            AS total_runs,
         SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail_count,
         ROUND(SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_rate_pct
FROM     test_results
JOIN     runs USING (run_id)
WHERE    runs.team = 'TeamAlpha'
GROUP BY test_name
ORDER BY fail_count DESC
LIMIT    5;


-- Q3: Tests that failed in the MOST RECENT run
SELECT test_name, failure_msg, duration_s
FROM   test_results
WHERE  run_id = (SELECT run_id FROM runs WHERE team='TeamAlpha'
                 ORDER BY timestamp DESC LIMIT 1)
AND    status = 'FAIL';


-- Q4: Flakiness — tests that changed status most often
-- (students implement this in Python, not pure SQL)
SELECT   test_name, COUNT(DISTINCT status) AS distinct_statuses
FROM     test_results
JOIN     runs USING (run_id)
WHERE    runs.team = 'TeamAlpha'
GROUP BY test_name
HAVING   COUNT(DISTINCT status) > 1
ORDER BY distinct_statuses DESC;


-- Q5: Week-on-week pass rate comparison
SELECT
    strftime('%Y-%W', timestamp) AS week,
    ROUND(AVG(pass_rate_pct), 1) AS avg_pass_rate
FROM  runs
WHERE team = 'TeamAlpha'
GROUP BY week
ORDER BY week DESC
LIMIT 4;

*/
