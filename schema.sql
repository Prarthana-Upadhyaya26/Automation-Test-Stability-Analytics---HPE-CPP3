-- ============================================================
-- analytics.db — SQLite Schema
-- Automation Test Stability Analytics — Phase 2 + Defect Mapping
--
-- Run once to create your database:
--   python -c "import sqlite3; conn=sqlite3.connect('analytics.db'); conn.executescript(open('schema.sql').read()); conn.close()"
--
-- Tables:
--   runs              — one row per CI run  (from ci_metadata.json)
--   test_results      — one row per test per run  (from output.xml)
--   ingestion_log     — tracks which runs have already been ingested
--   jira_defects      — one row per Jira issue (from Jira REST API / JSON input)
--   defect_test_mappings — links defects to matching test run failures
-- ============================================================

-- ── TABLE 1: runs ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    team            TEXT NOT NULL,
    suite_name      TEXT NOT NULL,
    job_name        TEXT,
    build_no        INTEGER,
    timestamp       DATETIME NOT NULL,
    duration_s      REAL,
    total           INTEGER NOT NULL,
    passed          INTEGER NOT NULL,
    failed          INTEGER NOT NULL,
    pass_rate_pct   REAL,
    environment     TEXT,
    executor        TEXT
);


-- ── TABLE 2: test_results ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS test_results (
    result_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    suite_name      TEXT NOT NULL,
    test_name       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
    duration_s      REAL,
    failure_msg     TEXT,
    failure_kw      TEXT,
    tags            TEXT
);


-- ── TABLE 3: ingestion_log ────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingestion_log (
    run_id          TEXT PRIMARY KEY,
    ingested_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    status          TEXT NOT NULL CHECK(status IN ('success', 'error')),
    error_msg       TEXT
);


-- ── TABLE 4: jira_defects ─────────────────────────────────────
-- One row per Jira issue ingested from the defect feed.
-- Populated by pipeline.py ingest_jira_defects().
--
-- Mapping pre-conditions (applied before semantic matching):
--   (a) reporter_email must equal the tester's login / email captured in CI metadata
--   (b) ABS(JULIANDAY(created) - JULIANDAY(run.timestamp)) <= 7

CREATE TABLE IF NOT EXISTS jira_defects (
    defect_id       TEXT PRIMARY KEY,
    -- Jira issue key, e.g. "CSSOSE-0002"

    project         TEXT NOT NULL,
    -- Jira project key: CSSOSE | CSSE | MCIO

    summary         TEXT NOT NULL,
    -- Short one-line summary from Jira

    description     TEXT,
    -- Full description body from Jira (may be NULL for minimal payloads)

    reporter_name   TEXT,
    -- Human-readable reporter name from Jira "reporter_name" field
    -- Used only for display; matching uses reporter_email

    reporter_email  TEXT,
    -- Normalised lowercase email of the defect reporter
    -- Pre-condition (a): must match tester_email on the CI run

    status          TEXT,
    -- Jira workflow status: Triage | In Progress | Triage | Lab Review |
    --   Closed - No Change | Closed - Fixed | Development | Duplicate | Testing

    priority        TEXT,
    -- Undecided | Medium  (or any future value)

    issue_type      TEXT,
    -- Bug | Story | Task …

    labels          TEXT,
    -- JSON array of label strings, e.g. '["automation","flaky","timeout"]'

    components      TEXT,
    -- JSON array of component strings, e.g. '["OS : OS - Linux"]'

    created         DATETIME NOT NULL,
    -- ISO-8601 timestamp from Jira "created" field
    -- Pre-condition (b): must be within 7 days of the matched run

    raw_json        TEXT,
    -- Full original JSON payload stored verbatim for audit / re-processing

    ingested_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);


-- ── TABLE 5: embeddings ──────────────────────────────────────
-- Persistent cache for BAAI/bge-small-en-v1.5 (or any swap-in model) vectors.
--
-- One row per (entity_id, model_name) pair.  entity_id is either a
-- result_id (from test_results) or a defect_id (from jira_defects).
-- entity_type distinguishes the two so both namespaces can share the table.
--
-- vector is a little-endian float32 BLOB produced by numpy:
--   numpy.asarray(model.encode(text), dtype=numpy.float32).tobytes()
-- Reload with:
--   numpy.frombuffer(blob, dtype=numpy.float32)
--
-- source_text stores the exact text that was embedded so we can detect
-- stale vectors when summary / failure_msg fields change after re-ingestion.

CREATE TABLE IF NOT EXISTS embeddings (
    entity_id    TEXT    NOT NULL,
    -- result_id from test_results  OR  defect_id from jira_defects

    entity_type  TEXT    NOT NULL CHECK(entity_type IN ('test_result','jira_defect')),
    -- discriminator so both namespaces share the table

    model_name   TEXT    NOT NULL,
    -- HuggingFace model ID, e.g. "BAAI/bge-small-en-v1.5"
    -- Allows multiple models to coexist for A/B evaluation

    vector       BLOB    NOT NULL,
    -- Little-endian float32 array — shape (embedding_dim,)
    -- For bge-small-en-v1.5: 384 floats = 1536 bytes

    source_text  TEXT    NOT NULL,
    -- The exact text that was embedded; used to detect staleness

    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (entity_id, model_name)
    -- One vector per entity per model; replace on re-embed
);


-- ── TABLE 6: defect_test_mappings ────────────────────────────
-- One row per (defect, test_result) candidate match.
-- Populated by pipeline.py map_defects_to_test_results().
--
-- A "confirmed" mapping satisfies ALL three criteria:
--   (a) reporter_email == tester_email for that run
--   (b) |defect.created - run.timestamp| <= 7 days  (date_diff_days <= 7)
--   (c) confidence_score >= threshold (default 0.5)
--
-- confidence_score breakdown:
--   +0.60  exact test name found verbatim in defect summary or description
--   +0.25  test name stem (e.g. "BulkImport") found in summary or description
--   +0.15  failure keyword overlap (≥1 shared non-stopword token between
--          test failure_msg and defect summary/description)
--   Score is capped at 1.0 and records with score < 0.25 are discarded.

CREATE TABLE IF NOT EXISTS defect_test_mappings (
    mapping_id          TEXT PRIMARY KEY,
    -- Constructed as:  defect_id + "__" + result_id
    -- e.g. "CSSOSE-0002__TeamAlpha_build_023_TC_Login_ValidCredentials"

    defect_id           TEXT NOT NULL REFERENCES jira_defects(defect_id),
    result_id           TEXT NOT NULL REFERENCES test_results(result_id),

    -- Denormalised for fast dashboard queries (avoids joins in hot paths)
    run_id              TEXT NOT NULL,
    test_name           TEXT NOT NULL,
    defect_project      TEXT NOT NULL,
    defect_summary      TEXT NOT NULL,
    defect_status       TEXT,
    defect_priority     TEXT,
    reporter_email      TEXT,

    -- Temporal proximity
    run_timestamp       DATETIME NOT NULL,
    defect_created      DATETIME NOT NULL,
    date_diff_days      REAL NOT NULL,
    -- ABS(JULIANDAY(defect_created) - JULIANDAY(run_timestamp))

    -- Match quality
    confidence_score    REAL NOT NULL,
    -- 0.0 – 1.0; records < 0.25 are excluded at write time

    match_reason        TEXT NOT NULL,
    -- Human-readable explanation of why this mapping was made.
    -- Example: "Exact test name match in summary; keyword overlap (timeout)"

    -- Pre-condition flags (both must be TRUE for a "confirmed" mapping)
    email_match         INTEGER NOT NULL DEFAULT 0 CHECK(email_match IN (0,1)),
    -- 1 = reporter_email matched the tester_email recorded for the run

    date_within_window  INTEGER NOT NULL DEFAULT 0 CHECK(date_within_window IN (0,1)),
    -- 1 = |date_diff_days| <= 7

    confirmed           INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
    -- 1 = email_match AND date_within_window AND confidence_score >= 0.5
    -- Convenience flag for the dashboard WHERE confirmed = 1 filter

    mapped_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);


-- ── TABLE 7: jira_sync_log ───────────────────────────────────
-- Tracks every write-back attempt to Jira so that:
--   (a) Re-runs are idempotent — already-written mappings are skipped
--   (b) Errors are auditable without digging through logs
--   (c) The dashboard can show sync health at a glance
--
-- Populated by jira_client.write_back_confirmed_mappings().
-- One row per (mapping_id) attempt; use INSERT OR REPLACE so re-runs
-- update the status rather than accumulating duplicate rows.

CREATE TABLE IF NOT EXISTS jira_sync_log (
    mapping_id   TEXT PRIMARY KEY,
    -- From defect_test_mappings.mapping_id

    defect_id    TEXT NOT NULL,
    -- Jira issue key the comment was posted to

    run_id       TEXT NOT NULL,
    -- CI run ID written into the comment / custom field

    status       TEXT NOT NULL CHECK(status IN ('success', 'error', 'skipped')),
    -- 'success' — comment posted and/or field updated
    -- 'error'   — API call failed (see error_msg)
    -- 'skipped' — comment already existed (idempotency)

    error_msg    TEXT,
    -- NULL on success / skipped; error description on failure

    dry_run      INTEGER NOT NULL DEFAULT 0 CHECK(dry_run IN (0,1)),
    -- 1 = this row was written during a --dry-run invocation

    written_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sync_log_defect_id
    ON jira_sync_log(defect_id);

CREATE INDEX IF NOT EXISTS idx_sync_log_status
    ON jira_sync_log(status);

CREATE INDEX IF NOT EXISTS idx_sync_log_written_at
    ON jira_sync_log(written_at);



CREATE INDEX IF NOT EXISTS idx_runs_team
    ON runs(team);

CREATE INDEX IF NOT EXISTS idx_runs_timestamp
    ON runs(timestamp);

CREATE INDEX IF NOT EXISTS idx_test_results_run_id
    ON test_results(run_id);

CREATE INDEX IF NOT EXISTS idx_test_results_test_name
    ON test_results(test_name);

CREATE INDEX IF NOT EXISTS idx_test_results_status
    ON test_results(status);

-- ── INDEXES ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_embeddings_entity_type
    ON embeddings(entity_type, model_name);
-- used by: WHERE entity_type = 'jira_defect' AND model_name = ?

CREATE INDEX IF NOT EXISTS idx_jira_defects_project
    ON jira_defects(project);

CREATE INDEX IF NOT EXISTS idx_jira_defects_created
    ON jira_defects(created);

CREATE INDEX IF NOT EXISTS idx_jira_defects_reporter_email
    ON jira_defects(reporter_email);

CREATE INDEX IF NOT EXISTS idx_defect_mappings_defect_id
    ON defect_test_mappings(defect_id);

CREATE INDEX IF NOT EXISTS idx_defect_mappings_result_id
    ON defect_test_mappings(result_id);

CREATE INDEX IF NOT EXISTS idx_defect_mappings_confirmed
    ON defect_test_mappings(confirmed);

CREATE INDEX IF NOT EXISTS idx_defect_mappings_test_name
    ON defect_test_mappings(test_name);


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


-- Q3: All confirmed defect-to-test mappings with full detail
SELECT
    m.defect_id,
    m.defect_project,
    m.defect_summary,
    m.defect_status,
    m.test_name,
    m.run_id,
    m.date_diff_days,
    m.confidence_score,
    m.match_reason
FROM defect_test_mappings m
WHERE m.confirmed = 1
ORDER BY m.confidence_score DESC, m.date_diff_days ASC;


-- Q4: Defect coverage — how many failing test-run pairs have a linked defect?
SELECT
    COUNT(DISTINCT tr.result_id)                       AS total_failures,
    COUNT(DISTINCT m.result_id)                        AS failures_with_defect,
    ROUND(COUNT(DISTINCT m.result_id) * 100.0 /
          NULLIF(COUNT(DISTINCT tr.result_id), 0), 1) AS coverage_pct
FROM test_results tr
LEFT JOIN defect_test_mappings m
    ON tr.result_id = m.result_id AND m.confirmed = 1
WHERE tr.status = 'FAIL';


-- Q5: Per-test defect linkage rate
SELECT
    tr.test_name,
    COUNT(*)                                          AS total_failures,
    COUNT(DISTINCT m.defect_id)                       AS linked_defects,
    ROUND(AVG(m.confidence_score), 2)                 AS avg_confidence
FROM test_results tr
LEFT JOIN defect_test_mappings m
    ON tr.result_id = m.result_id AND m.confirmed = 1
WHERE tr.status = 'FAIL'
GROUP BY tr.test_name
ORDER BY linked_defects DESC;

*/