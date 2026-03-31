-- =============================================================================
-- Phase 2 Database Schema — Automation Test Stability Analytics
-- =============================================================================
--
-- Purpose: Store parsed Robot Framework test data for ML analysis
--
-- Tables:
--   - runs (100 rows): CI build metadata
--   - tests (2000 rows): Test execution records
--   - test_results (2000 rows): Test metadata
--   - failures (~480 rows): Failure messages for clustering
--   - tags (40 rows): Test categorization tags
--
-- =============================================================================

-- =============================================================================
-- TABLE: runs
-- =============================================================================
-- Purpose: Store CI build/run metadata (one row per run)
-- Row count: 100 (one per generated run)
-- Primary key: run_id (1 to 100)
--
-- =============================================================================

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY,  -- 1 to 100
    build_number    INTEGER NOT NULL,     -- Same as run_id for this project
    timestamp       TEXT NOT NULL,        -- ISO format: 2024-10-01T00:00:00
    total_tests     INTEGER NOT NULL,     -- Always 20 in our design
    passed          INTEGER NOT NULL,     -- 0 to 20
    failed          INTEGER NOT NULL,     -- 0 to 20
    pass_rate       REAL NOT NULL,        -- 0.0 to 100.0 (percentage)
    environment     TEXT NOT NULL,        -- e.g., "staging"
    executor        TEXT NOT NULL,        -- e.g., "jenkins-agent-01"
    
    -- Constraints
    CHECK (total_tests = passed + failed),  -- Integrity check
    CHECK (pass_rate >= 0 AND pass_rate <= 100),
    CHECK (passed >= 0 AND passed <= 20),
    CHECK (failed >= 0 AND failed <= 20)
);

-- Index for time-based queries (pass rate over time)
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);

-- Index for pass rate filtering (find anomalies)
CREATE INDEX IF NOT EXISTS idx_runs_pass_rate ON runs(pass_rate);

-- =============================================================================
-- TABLE: tests
-- =============================================================================
-- Purpose: Store individual test executions (one row per test per run)
-- Row count: 2000 (20 tests × 100 runs)
-- Primary key: test_id (auto-increment)
-- Foreign key: run_id → runs(run_id)
--
-- =============================================================================

CREATE TABLE IF NOT EXISTS tests (
    test_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    test_name       TEXT NOT NULL,        -- e.g., "TC_Login_ValidCredentials"
    status          TEXT NOT NULL,        -- "PASS" or "FAIL"
    duration        REAL NOT NULL,        -- Execution time in seconds
    start_time      TEXT NOT NULL,        -- Timestamp when test started
    end_time        TEXT NOT NULL,        -- Timestamp when test ended
    
    -- Foreign key to runs table
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    
    -- Constraints
    CHECK (status IN ('PASS', 'FAIL')),
    CHECK (duration >= 0)
);

-- Index for test name queries (get all runs of specific test)
-- CRITICAL for Phase 4 ML4 duration drift detection
CREATE INDEX IF NOT EXISTS idx_tests_name ON tests(test_name);

-- Index for run-based queries (get all tests in a run)
CREATE INDEX IF NOT EXISTS idx_tests_run_id ON tests(run_id);

-- Index for status filtering (get all failures)
CREATE INDEX IF NOT EXISTS idx_tests_status ON tests(status);

-- Composite index for test name + run queries (optimized for ML)
CREATE INDEX IF NOT EXISTS idx_tests_name_run ON tests(test_name, run_id);

-- =============================================================================
-- TABLE: test_results
-- =============================================================================
-- Purpose: Store test metadata and design information
-- Row count: 2000 (one-to-one with tests table)
-- Primary key: result_id (auto-increment)
-- Foreign key: test_id → tests(test_id)
--
-- =============================================================================

CREATE TABLE IF NOT EXISTS test_results (
    result_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id             INTEGER NOT NULL UNIQUE,  -- One-to-one relationship
    feature             TEXT NOT NULL,            -- e.g., "feature_login"
    priority            TEXT NOT NULL,            -- e.g., "priority_high"
    category            TEXT NOT NULL,            -- e.g., "flaky-moderate"
    fail_probability    REAL,                     -- Design probability (0.0-1.0)
    
    -- Foreign key to tests table
    FOREIGN KEY (test_id) REFERENCES tests(test_id) ON DELETE CASCADE,
    
    -- Constraints
    CHECK (fail_probability IS NULL OR 
           (fail_probability >= 0 AND fail_probability <= 1))
);

-- Index for category queries (filter by stable/flaky/failing)
-- CRITICAL for Phase 4 ML1 classifier training
CREATE INDEX IF NOT EXISTS idx_results_category ON test_results(category);

-- Index for feature filtering
CREATE INDEX IF NOT EXISTS idx_results_feature ON test_results(feature);

-- =============================================================================
-- TABLE: failures
-- =============================================================================
-- Purpose: Store failure messages for clustering analysis
-- Row count: ~480 (only failed tests have entries)
-- Primary key: failure_id (auto-increment)
-- Foreign key: test_id → tests(test_id)
--
-- =============================================================================

CREATE TABLE IF NOT EXISTS failures (
    failure_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id         INTEGER NOT NULL,
    category        TEXT NOT NULL,        -- timeout/element/assertion/data/environment
    message         TEXT NOT NULL,        -- Full error message text
    keyword_name    TEXT,                 -- Failed keyword (e.g., "Click Element")
    
    -- Foreign key to tests table
    FOREIGN KEY (test_id) REFERENCES tests(test_id) ON DELETE CASCADE,
    
    -- Constraints
    CHECK (category IN ('timeout', 'element', 'assertion', 'data', 'environment'))
);

-- Index for category queries (get all timeout failures)
-- CRITICAL for Phase 4 ML2 clustering validation
CREATE INDEX IF NOT EXISTS idx_failures_category ON failures(category);

-- Index for test_id lookups (join with tests table)
CREATE INDEX IF NOT EXISTS idx_failures_test_id ON failures(test_id);

-- =============================================================================
-- TABLE: tags
-- =============================================================================
-- Purpose: Store test tags (many-to-many relationship with tests)
-- Row count: ~40 (each test has ~2-3 tags)
-- Primary key: tag_id (auto-increment)
-- Foreign key: test_id → tests(test_id)
--
-- =============================================================================

CREATE TABLE IF NOT EXISTS tags (
    tag_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id         INTEGER NOT NULL,
    tag_name        TEXT NOT NULL,
    
    -- Foreign key to tests table
    FOREIGN KEY (test_id) REFERENCES tests(test_id) ON DELETE CASCADE
);

-- Index for tag name queries (get all tests with specific tag)
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(tag_name);

-- Index for test lookups (get all tags for a test)
CREATE INDEX IF NOT EXISTS idx_tags_test_id ON tags(test_id);

-- Composite index for tag + test queries
CREATE INDEX IF NOT EXISTS idx_tags_name_test ON tags(tag_name, test_id);

-- =============================================================================
-- VIEWS (For convenience queries)
-- =============================================================================

-- View: Test summary with run information
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
    f.message AS failure_message,
    f.category AS failure_category
FROM tests t
JOIN runs r ON t.run_id = r.run_id
JOIN test_results tr ON t.test_id = tr.test_id
LEFT JOIN failures f ON t.test_id = f.test_id;

-- View: Run statistics with pass/fail breakdown
CREATE VIEW IF NOT EXISTS v_run_statistics AS
SELECT 
    r.run_id,
    r.timestamp,
    r.passed,
    r.failed,
    r.pass_rate,
    COUNT(CASE WHEN tr.category = 'stable' AND t.status = 'FAIL' THEN 1 END) AS stable_failures,
    COUNT(CASE WHEN tr.category LIKE 'flaky%' AND t.status = 'FAIL' THEN 1 END) AS flaky_failures,
    COUNT(CASE WHEN tr.category = 'consistently_failing' AND t.status = 'FAIL' THEN 1 END) AS consistent_failures
FROM runs r
JOIN tests t ON r.run_id = t.run_id
JOIN test_results tr ON t.test_id = tr.test_id
GROUP BY r.run_id;

-- View: Failure category distribution
CREATE VIEW IF NOT EXISTS v_failure_distribution AS
SELECT 
    category,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM failures), 2) AS percentage
FROM failures
GROUP BY category
ORDER BY count DESC;

-- View: Test failure rates
CREATE VIEW IF NOT EXISTS v_test_failure_rates AS
SELECT 
    tr.test_name,
    tr.category,
    COUNT(*) AS total_runs,
    SUM(CASE WHEN t.status = 'FAIL' THEN 1 ELSE 0 END) AS failures,
    SUM(CASE WHEN t.status = 'PASS' THEN 1 ELSE 0 END) AS passes,
    ROUND(SUM(CASE WHEN t.status = 'FAIL' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS failure_rate
FROM (
    SELECT DISTINCT test_name, category 
    FROM tests 
    JOIN test_results ON tests.test_id = test_results.test_id
) tr
JOIN tests t ON tr.test_name = t.test_name
GROUP BY tr.test_name, tr.category
ORDER BY failure_rate DESC;

-- =============================================================================
-- DATA INTEGRITY TRIGGERS
-- =============================================================================

-- Trigger: Ensure pass_rate matches passed/total ratio
CREATE TRIGGER IF NOT EXISTS trg_validate_pass_rate
BEFORE INSERT ON runs
FOR EACH ROW
WHEN NEW.pass_rate != ROUND(NEW.passed * 100.0 / NEW.total_tests, 1)
BEGIN
    SELECT RAISE(ABORT, 'Pass rate does not match passed/total ratio');
END;

-- Trigger: Prevent duplicate test names in same run
CREATE TRIGGER IF NOT EXISTS trg_prevent_duplicate_tests
BEFORE INSERT ON tests
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM tests 
    WHERE run_id = NEW.run_id AND test_name = NEW.test_name
)
BEGIN
    SELECT RAISE(ABORT, 'Duplicate test name in same run');
END;

-- =============================================================================
-- SCHEMA METADATA
-- =============================================================================

CREATE TABLE IF NOT EXISTS schema_info (
    version         TEXT PRIMARY KEY,
    applied_at      TEXT NOT NULL,
    description     TEXT
);

INSERT OR IGNORE INTO schema_info (version, applied_at, description) VALUES
    ('2.0', datetime('now'), 'Phase 2 initial schema with optimized indexes');

