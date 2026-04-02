-- =============================================================================
-- Phase 2 Database Schema — Automation Test Stability Analytics
-- =============================================================================
--
-- Purpose: Store parsed Robot Framework test data for Dashboard and ML analysis
--
-- Tables:
--   - runs           (100 rows)   : CI build metadata
--   - tests          (2000 rows)  : Test execution records (20 × 100 runs)
--   - test_results   (2000 rows)  : Test metadata (1-to-1 with tests)
--   - failures       (~480 rows)  : Failure messages for clustering
--   - tags           (~6000 rows) : Test tags, ~3 per test-run row
--   - ingestion_log  (100 rows)   : Tracks processed runs for idempotency
--
-- =============================================================================


-- =============================================================================
-- TABLE: runs
-- =============================================================================
-- Purpose: Store CI build/run metadata (one row per run)
-- Row count: 100 (one per generated run)
-- Primary key: run_id (1 to 100)
-- =============================================================================

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY,  -- 1 to 100
    build_number    INTEGER NOT NULL,     -- Same as run_id for this project
    timestamp       TEXT    NOT NULL,     -- ISO format: 2024-10-01T00:00:00
    total_tests     INTEGER NOT NULL,     -- Always 20 in our design
    passed          INTEGER NOT NULL,     -- 0 to 20
    failed          INTEGER NOT NULL,     -- 0 to 20
    pass_rate       REAL    NOT NULL,     -- 0.0 to 100.0 (percentage)
    environment     TEXT    NOT NULL,     -- e.g., "staging"
    executor        TEXT    NOT NULL,     -- e.g., "jenkins-agent-01"

    -- Constraints
    CHECK (total_tests = passed + failed),
    CHECK (pass_rate >= 0 AND pass_rate <= 100),
    CHECK (passed  >= 0 AND passed  <= 20),
    CHECK (failed  >= 0 AND failed  <= 20)
);

-- Index for time-based queries (pass rate over time, Q2 trend chart)
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);

-- Index for pass rate filtering (anomaly detection)
CREATE INDEX IF NOT EXISTS idx_runs_pass_rate ON runs(pass_rate);


-- =============================================================================
-- TABLE: tests
-- =============================================================================
-- Purpose: Store individual test executions (one row per test per run)
-- Row count: 2000 (20 tests × 100 runs)
-- Primary key: test_id (auto-increment)
-- Foreign key: run_id → runs(run_id)
-- =============================================================================

CREATE TABLE IF NOT EXISTS tests (
    test_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    test_name  TEXT    NOT NULL,   -- e.g., "TC_Login_ValidCredentials"
    status     TEXT    NOT NULL,   -- "PASS" or "FAIL"
    duration   REAL    NOT NULL,   -- Execution time in seconds
    start_time TEXT    NOT NULL,   -- RF timestamp when test started
    end_time   TEXT    NOT NULL,   -- RF timestamp when test ended

    -- Foreign key
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,

    -- Constraints
    CHECK (status IN ('PASS', 'FAIL')),
    CHECK (duration >= 0),

    -- DDL-level uniqueness: one row per (run, test).
    -- The trg_prevent_duplicate_tests trigger also enforces this at runtime,
    -- but the UNIQUE constraint is the authoritative definition and is visible
    -- to external tools (DB Browser, SQLAlchemy, etc.).
    UNIQUE (run_id, test_name)
);

-- Index for test name queries — CRITICAL for Phase 4 ML4 duration drift
CREATE INDEX IF NOT EXISTS idx_tests_name    ON tests(test_name);

-- Index for run-based queries (get all tests in a run)
CREATE INDEX IF NOT EXISTS idx_tests_run_id  ON tests(run_id);

-- Index for status filtering (get all failures)
CREATE INDEX IF NOT EXISTS idx_tests_status  ON tests(status);

-- Composite index: test name + run — optimised for ML sliding-window queries
CREATE INDEX IF NOT EXISTS idx_tests_name_run ON tests(test_name, run_id);


-- =============================================================================
-- TABLE: test_results
-- =============================================================================
-- Purpose: Store test design metadata (feature, priority, category, etc.)
-- Row count: 2000 (one-to-one with tests)
-- Primary key: result_id (auto-increment)
-- Foreign key: test_id → tests(test_id)
-- =============================================================================

CREATE TABLE IF NOT EXISTS test_results (
    result_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id          INTEGER NOT NULL UNIQUE,  -- One-to-one relationship
    feature          TEXT    NOT NULL,         -- e.g., "feature_login"
    priority         TEXT    NOT NULL,         -- e.g., "priority_high"
    category         TEXT    NOT NULL,         -- e.g., "flaky-moderate"
    fail_probability REAL,                     -- Design probability (0.0–1.0)

    FOREIGN KEY (test_id) REFERENCES tests(test_id) ON DELETE CASCADE,

    CHECK (fail_probability IS NULL OR
           (fail_probability >= 0 AND fail_probability <= 1))
);

-- Index for category queries — CRITICAL for Phase 4 ML1 classifier training
CREATE INDEX IF NOT EXISTS idx_results_category ON test_results(category);

-- Index for feature filtering
CREATE INDEX IF NOT EXISTS idx_results_feature  ON test_results(feature);


-- =============================================================================
-- TABLE: failures
-- =============================================================================
-- Purpose: Store failure messages for clustering analysis (ML2)
-- Row count: ~480 (only FAIL rows have entries here)
-- Primary key: failure_id (auto-increment)
-- Foreign key: test_id → tests(test_id)
-- =============================================================================

CREATE TABLE IF NOT EXISTS failures (
    failure_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      INTEGER NOT NULL,
    category     TEXT    NOT NULL,  -- timeout / element / assertion / data / environment
    message      TEXT    NOT NULL,  -- Full error message text
    keyword_name TEXT,              -- Failed keyword, e.g. "Click Element" (NULL if unknown)

    FOREIGN KEY (test_id) REFERENCES tests(test_id) ON DELETE CASCADE,

    CHECK (category IN ('timeout', 'element', 'assertion', 'data', 'environment'))
);

-- Index for category queries — CRITICAL for Phase 4 ML2 clustering validation
CREATE INDEX IF NOT EXISTS idx_failures_category ON failures(category);

-- Index for test_id lookups
CREATE INDEX IF NOT EXISTS idx_failures_test_id  ON failures(test_id);


-- =============================================================================
-- TABLE: tags
-- =============================================================================
-- Purpose: Store test tags — many-to-many relationship with tests
-- Row count: ~6000 (each test has ~3 tags: team, feature, priority)
--            That is: 20 tests × 100 runs × ~3 tags = ~6000 rows
-- Primary key: tag_id (auto-increment)
-- Foreign key: test_id → tests(test_id)
-- =============================================================================

CREATE TABLE IF NOT EXISTS tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id  INTEGER NOT NULL,
    tag_name TEXT    NOT NULL,

    FOREIGN KEY (test_id) REFERENCES tests(test_id) ON DELETE CASCADE
);

-- Index for tag name queries (get all tests with a specific tag)
CREATE INDEX IF NOT EXISTS idx_tags_name      ON tags(tag_name);

-- Index for test lookups (get all tags for a test)
CREATE INDEX IF NOT EXISTS idx_tags_test_id   ON tags(test_id);

-- Composite index for tag + test queries
CREATE INDEX IF NOT EXISTS idx_tags_name_test ON tags(tag_name, test_id);


-- =============================================================================
-- TABLE: ingestion_log
-- =============================================================================
-- Purpose: Track which run folders have been ingested so the pipeline is
--          idempotent.  pipeline.py checks this table before processing each
--          folder; if run_id is already present with status='success', the
--          folder is skipped entirely.
--
-- Row count: up to 100 (one per run, on success or error)
-- =============================================================================

CREATE TABLE IF NOT EXISTS ingestion_log (
    run_id      INTEGER  PRIMARY KEY,
    ingested_at TEXT     NOT NULL DEFAULT (datetime('now')),
    status      TEXT     NOT NULL,   -- 'success' | 'error'
    error_msg   TEXT,                -- NULL on success; description on error

    CHECK (status IN ('success', 'error'))
);


-- =============================================================================
-- VIEWS
-- =============================================================================

-- v_test_summary: one row per test execution, all tables joined
CREATE VIEW IF NOT EXISTS v_test_summary AS
SELECT
    t.test_id,
    t.test_name,
    t.status,
    t.duration,
    r.run_id,
    r.timestamp,
    tr.category,
    tr.feature,
    tr.priority,
    f.message      AS failure_message,
    f.category     AS failure_category,
    f.keyword_name AS failure_keyword
FROM       tests        t
JOIN       runs         r  ON t.run_id  = r.run_id
JOIN       test_results tr ON t.test_id = tr.test_id
LEFT JOIN  failures     f  ON t.test_id = f.test_id;


-- v_run_statistics: per-run pass/fail counts broken out by test category
CREATE VIEW IF NOT EXISTS v_run_statistics AS
SELECT
    r.run_id,
    r.timestamp,
    r.passed,
    r.failed,
    r.pass_rate,
    COUNT(CASE WHEN tr.category = 'stable'               AND t.status = 'FAIL' THEN 1 END) AS stable_failures,
    COUNT(CASE WHEN tr.category LIKE 'flaky%'            AND t.status = 'FAIL' THEN 1 END) AS flaky_failures,
    COUNT(CASE WHEN tr.category = 'consistently_failing' AND t.status = 'FAIL' THEN 1 END) AS consistent_failures
FROM       runs         r
JOIN       tests        t  ON r.run_id  = t.run_id
JOIN       test_results tr ON t.test_id = tr.test_id
GROUP BY   r.run_id;


-- v_failure_distribution: overall breakdown of failure categories
CREATE VIEW IF NOT EXISTS v_failure_distribution AS
SELECT
    category,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM failures), 2) AS percentage
FROM  failures
GROUP BY category
ORDER BY count DESC;


-- v_test_failure_rates: per-test failure rate across all runs
CREATE VIEW IF NOT EXISTS v_test_failure_rates AS
SELECT
    t.test_name,
    tr.category,
    COUNT(*)                                                                   AS total_runs,
    SUM(CASE WHEN t.status = 'FAIL' THEN 1 ELSE 0 END)                        AS failures,
    SUM(CASE WHEN t.status = 'PASS' THEN 1 ELSE 0 END)                        AS passes,
    ROUND(SUM(CASE WHEN t.status = 'FAIL' THEN 1 ELSE 0 END) * 100.0
          / COUNT(*), 2)                                                       AS failure_rate
FROM       tests        t
JOIN       test_results tr ON t.test_id = tr.test_id
GROUP BY   t.test_name, tr.category
ORDER BY   failure_rate DESC;


-- v_duration_trends: per-test duration over time — feeds Phase 4 ML4
CREATE VIEW IF NOT EXISTS v_duration_trends AS
SELECT
    t.test_name,
    t.run_id,
    r.timestamp,
    t.duration,
    t.status
FROM       tests t
JOIN       runs  r ON t.run_id = r.run_id
ORDER BY   t.test_name, t.run_id;


-- =============================================================================
-- DATA INTEGRITY TRIGGERS
-- =============================================================================

-- Trigger: Validate pass_rate is consistent with passed / total_tests.
CREATE TRIGGER IF NOT EXISTS trg_validate_pass_rate
BEFORE INSERT ON runs
FOR EACH ROW
WHEN ABS(NEW.pass_rate - ROUND(NEW.passed * 100.0 / NEW.total_tests, 1)) > 0.05
BEGIN
    SELECT RAISE(ABORT, 'pass_rate does not match ROUND(passed*100/total_tests,1). Check generator or pipeline rounding.');
END;

-- Trigger: Prevent duplicate (run_id, test_name) pairs.
CREATE TRIGGER IF NOT EXISTS trg_prevent_duplicate_tests
BEFORE INSERT ON tests
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM tests
    WHERE run_id = NEW.run_id AND test_name = NEW.test_name
)
BEGIN
    SELECT RAISE(ABORT, 'Duplicate test name in same run — check for double-ingestion or malformed XML.');
END;


-- =============================================================================
-- SCHEMA METADATA
-- =============================================================================

CREATE TABLE IF NOT EXISTS schema_info (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT
);

INSERT OR IGNORE INTO schema_info (version, applied_at, description) VALUES
    ('2.1', datetime('now'),
     'Phase 2 schema — added ingestion_log, UNIQUE(run_id,test_name), '
     || 'fixed float trigger, added v_duration_trends view');