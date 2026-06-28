# Jira Sync & Write-Back — Setup and Reference Guide

**Automation Test Stability Analytics · `jira_client.py` + `pipeline.py`**

---

## Table of Contents

1. [What This Feature Does](#1-what-this-feature-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Prerequisites](#3-prerequisites)
4. [Credentials Setup](#4-credentials-setup)
5. [Per-Project Configuration (Optional)](#5-per-project-configuration-optional)
6. [Step-by-Step Setup](#6-step-by-step-setup)
7. [Sample Jira Issues for Integration Testing](#7-sample-jira-issues-for-integration-testing)
8. [Running the Pipeline](#8-running-the-pipeline)
9. [Feature Reference: What Each Section Does and Why It Matters](#9-feature-reference-what-each-section-does-and-why-it-matters)
   - [9.1 Defect Fetch (`fetch-defects`)](#91-defect-fetch-fetch-defects)
   - [9.2 Defect-to-Test Mapping (`map-defects`)](#92-defect-to-test-mapping-map-defects)
   - [9.3 Write-Back to Jira (`writeback`)](#93-write-back-to-jira-writeback)
   - [9.4 Duplicate Defect Detection (`detect-duplicates`)](#94-duplicate-defect-detection-detect-duplicates)
   - [9.5 Sync Log and Dashboard Health](#95-sync-log-and-dashboard-health)
10. [Idempotency and Safety](#10-idempotency-and-safety)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What This Feature Does

The Jira sync and write-back feature closes the loop between your CI test runs and your Jira defect tracker. It does four things:

- **Pulls** Bug issues from your Jira projects (CSSOSE, CSSE, MCIO) into the local SQLite database.
- **Maps** each Jira defect to the specific test failure it most likely describes, using a hybrid rule-based + semantic scoring pipeline.
- **Writes back** a structured comment (and optionally a custom field value) onto the matched Jira issue, linking it to the exact CI run ID where the failure was observed.
- **Detects duplicates** — surfaces pairs of Jira defects that almost certainly describe the same underlying failure so your team can merge or link them before reporting inflates.

The end result is that every confirmed test failure in your analytics database has a traceable Jira ticket attached, and every Jira ticket has a direct link back to the CI run that triggered it. Both the dashboard and the defect tracker stay in sync automatically.

---

## 2. Architecture Overview

```
CI runs (output.xml + ci_metadata.json)
        │
        ▼
pipeline.py --ingest          ──► runs + test_results tables
        │
        ├──► pipeline.py --fetch-defects ──► Jira REST API v3
        │                                        │
        │                                        ▼
        │                                  jira_defects table
        │                                  embeddings table (BAAI/bge-small-en-v1.5)
        │
        ├──► pipeline.py --map-defects ───► defect_test_mappings table
        │          (hybrid rule + semantic scoring, confirmed flag set)
        │
        ├──► pipeline.py --writeback ─────► Jira REST API v3
        │          (posts comment + updates customfield_10042)
        │          (jira_sync_log table updated for auditing)
        │
        └──► pipeline.py --detect-duplicates
                   (cosine similarity on embeddings, Jaccard fallback)
```

All writes to Jira are **idempotent**: a sentinel tag `[automation-analytics]` is embedded in every comment, and the system checks for its presence before posting. Re-running the pipeline never produces duplicate comments.

---

## 3. Prerequisites

### Python dependencies

```bash
pip install requests python-dotenv FlagEmbedding numpy
```

`FlagEmbedding` is required for semantic scoring. If it is unavailable the pipeline falls back automatically to rule-only scoring (α = 1.0); no exception is raised.

### Jira access requirements

| Requirement | Details |
|---|---|
| Jira Cloud | REST API v3 (`/rest/api/3`). Self-hosted Jira Server/Data Center uses v2 — the base URL stays the same but the ADF description parser will not apply. |
| Account type | Any account that can browse the target projects and post comments. A dedicated service account is recommended for production. |
| API Token | Generate at: `https://id.atlassian.com/manage-profile/security/api-tokens` |
| Custom field | `customfield_10042` must exist and be editable by your account if you want the run ID written into a field (not just a comment). This ID is configurable — see §5. |

---

## 4. Credentials Setup

**Never pass credentials on the command line.** Set these as environment variables or place them in a `.env` file in the same directory as `jira_client.py`. The file is loaded automatically via `python-dotenv`.

Create `.env`:

```ini
# Required
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_EMAIL=tester@hpe.com
JIRA_API_TOKEN=your_personal_access_token_here

# Optional — defaults to CSSOSE,CSSE,MCIO
JIRA_PROJECTS=CSSOSE,CSSE,MCIO
```

Verify the connection before running any pipeline steps:

```python
from jira_client import load_credentials, test_connection

creds  = load_credentials()
result = test_connection(creds)
print(result)
# {"ok": True, "display_name": "Sample Reporter", "email": "tester@hpe.com"}
```

---

## 5. Per-Project Configuration (Optional)

You can override the confirmation score threshold, the custom field ID, and the date window on a per-project basis. The naming convention is `{PROJECT}_{KEY}`.

```ini
# Raise the confirmation bar for CSSOSE (stricter matching)
CSSOSE_CONFIRM_THRESHOLD=0.60

# Lower bar for MCIO (noisier defect descriptions)
MCIO_CONFIRM_THRESHOLD=0.45

# Different custom field IDs per project
CSSOSE_AUTOMATION_FIELD=customfield_10042
CSSE_AUTOMATION_FIELD=customfield_10055
MCIO_AUTOMATION_FIELD=customfield_10042

# Wider date window for CSSE (defects often filed late)
CSSE_WINDOW_DAYS=14
```

If no project-level override is set, the global defaults apply:

| Parameter | Default | Description |
|---|---|---|
| `CONFIRM_THRESHOLD` | `0.50` | Minimum hybrid score for `confirmed = 1` |
| `AUTOMATION_FIELD` | `customfield_10042` | Jira field written with the CI run ID |
| `DEFECT_WINDOW_DAYS` | `7` | Max days between defect creation and run timestamp |

---

## 6. Step-by-Step Setup

### Step 1 — Create the database

```bash
python -c "
import sqlite3
conn = sqlite3.connect('analytics.db')
conn.executescript(open('schema.sql').read())
conn.close()
print('Database created.')
"
```

### Step 2 — Generate and ingest CI runs

```bash
python generate.py                          # produces ./runs/ (100 runs)
python pipeline.py --ingest                 # loads runs + test_results into analytics.db
```

### Step 3 — Load defects from the JSON file (offline / testing)

For testing without a live Jira connection, the existing `defects.json` file can be loaded directly:

```bash
python pipeline.py --load-defects defects.json
```

### Step 4 — Or fetch defects live from Jira

```bash
python pipeline.py --fetch-defects --since-days 90 --tester-email tester@hpe.com
```

### Step 5 — Run defect-to-test mapping

```bash
python pipeline.py --map-defects --tester-email tester@hpe.com --window-days 7
```

### Step 6 — Dry-run write-back (no API calls made)

```bash
python pipeline.py --writeback --dry-run
```

### Step 7 — Live write-back to Jira

```bash
python pipeline.py --writeback --dashboard-url https://your-dashboard-url.internal
```

### Step 8 — Detect duplicate defects

```bash
python pipeline.py --detect-duplicates --dup-threshold 0.90
```

### Running all steps in one command

```bash
python pipeline.py \
  --ingest \
  --fetch-defects --since-days 90 \
  --map-defects \
  --writeback \
  --detect-duplicates \
  --tester-email tester@hpe.com \
  --dashboard-url https://your-dashboard.internal
```

---

## 7. Sample Jira Issues for Integration Testing

The four issues below are designed specifically to test different aspects of the sync and write-back pipeline. Each exercises a different matching signal and a different Jira workflow status. Create these as Bug issues in your test Jira projects before running the integration test.

> **Important:** Set `reporter` to the email you will use for `JIRA_EMAIL` / `--tester-email`, and set `created` to within 7 days of a run in your dataset. The mapping pipeline gates on both.

---

### Issue 1 — Exact Test Name Match (tests rule-based scoring, `+0.60` signal)

**Project:** CSSOSE  
**Issue type:** Bug  
**Priority:** High  
**Labels:** `automation`, `login`, `timeout`, `flaky`  
**Component:** Authentication

**Summary:**
```
TC_Login_MFAVerification intermittent timeout during MFA overlay resolution
```

**Description:**
```
Automated test TC_Login_MFAVerification fails intermittently in CI regression.

Failure signature:
- Keyword: Wait Until Element Is Visible
- Message: MFA overlay still visible after 45s; expected resolution within 10s
- Duration: 45.2s

Steps to reproduce:
1. Execute full regression suite with MFA enabled in staging.
2. Run at least 5 consecutive executions to observe flakiness.
3. On failure, capture MFA service response time.

Expected: MFA overlay resolves within 10 seconds.
Actual: Overlay persists beyond timeout threshold intermittently.

Impact: Flaky MFA validation reduces confidence in login module pass-rate.

Requested action:
- Add retry logic with exponential backoff for MFA response polling.
- Investigate MFA service latency under concurrent session load.
```

**Why this issue is useful:** The exact test name `TC_Login_MFAVerification` appears verbatim in the summary. This fires the strongest rule-based signal (`+0.60`). The keyword `timeout` also matches failure messages from `s1-t5` runs. Expect `confidence_score ≥ 0.85` and `confirmed = 1`.

---

### Issue 2 — Stem Match + Keyword Overlap (tests mid-tier scoring, `+0.25` + `+0.15` signals)

**Project:** CSSE  
**Issue type:** Bug  
**Priority:** Medium  
**Labels:** `automation`, `usermanagement`, `data`, `assertion`  
**Component:** User management

**Summary:**
```
BulkImport data assertion failure — record count mismatch in staging regression
```

**Description:**
```
Automation regression is observing repeated failures in the user bulk import
test path. The import operation completes without error but the post-import
assertion on record count fails.

Failure signature:
- Keyword: Validate Imported Row Count
- Message: Expected >= 500 records after bulk import; found 12. Possible
  partial write or commit rollback.
- Duration: 16.4s

Steps to reproduce:
1. Prepare a CSV with 500+ user records.
2. Trigger TC_User_BulkImport via the UI import wizard.
3. Run Validate Imported Row Count assertion.
4. Observe mismatch between expected and actual row counts.

Expected: All records committed and queryable after import completes.
Actual: Partial record set persisted; remaining records silently dropped.

Impact: User provisioning workflows dependent on bulk import are unreliable.

Requested action:
- Check database transaction commit logic for bulk operations.
- Add row-level import validation in the import service.
```

**Why this issue is useful:** The word `BulkImport` (stem of `TC_User_BulkImport`) appears in the summary without the full `TC_` prefix. This exercises the stem-match signal (`+0.25`). The tokens `assertion`, `data`, and `record` overlap with failure messages from `s1-t17` runs, adding the keyword-overlap signal (`+0.15`). Expect `confidence_score` in the `0.55–0.70` range.

---

### Issue 3 — Semantic Match Only (tests embedding fallback when no name match exists)

**Project:** MCIO  
**Issue type:** Bug  
**Priority:** Medium  
**Labels:** `automation`, `dashboard`, `element`, `performance`  
**Component:** Tools Misc

**Summary:**
```
Widget rendering failure after concurrent data refresh in staging dashboard
```

**Description:**
```
The dashboard stability test suite is observing intermittent failures during
widget load sequences. Failures occur specifically when a data refresh is
triggered during an ongoing widget render cycle.

Failure signature:
- Keyword: Wait Until Widget Is Loaded
- Message: Element '.dashboard-widget[data-id="main-chart"]' not found in
  DOM after 30s; page appears partially rendered.
- Duration: 31.0s

Steps to reproduce:
1. Load the analytics dashboard.
2. Trigger a data refresh while the main chart widget is still loading.
3. Observe widget not found error.

Expected: Widget loads successfully within 10 seconds even under concurrent refresh.
Actual: Widget element missing from DOM; race condition between render and refresh.

Impact: Dashboard reliability tests are flaky, masking true UI regression signals.

Requested action:
- Implement render lock during data refresh operations.
- Add explicit widget-ready signal before executing DOM assertions.
```

**Why this issue is useful:** The summary does not contain `TC_Dashboard_LoadWidget` or `TC_Dashboard_RefreshData` verbatim or by stem. This issue can only be matched through the semantic embedding component (BAAI/bge-small-en-v1.5 cosine similarity between the failure message and the defect text). It tests whether semantic scoring compensates for weak rule-based signals. Expect `rule_score ≈ 0.15`, `semantic_score ≈ 0.75`, `confidence_score ≈ 0.45` — borderline confirmed, useful for threshold tuning.

---

### Issue 4 — Duplicate Pair Seed (tests `detect_duplicate_defects`)

Create **both** of these issues within 3 days of each other. They describe the same root cause from slightly different angles, so the duplicate detector should flag them as a pair.

**Issue 4a:**

**Project:** CSSOSE  
**Issue type:** Bug  
**Priority:** High  
**Labels:** `automation`, `login`, `sso`, `element`, `redirect`  
**Component:** Authentication

**Summary:**
```
TC_Login_SSORedirect failing — consent button element not found post-redirect
```

**Description:**
```
TC_Login_SSORedirect fails consistently after the SSO provider redirect.
The consent button expected on the post-redirect page is not present in
the DOM when the test script attempts to interact with it.

Keyword: Click Consent Button
Message: '#sso-consent-button' not found after 10s implicit wait
Duration: 22.3s

Root cause suspected: SSO provider changed the consent page layout in a
recent deployment; locator no longer matches rendered element.
```

**Issue 4b** (file 2–3 days later):

**Project:** CSSOSE  
**Issue type:** Bug  
**Priority:** High  
**Labels:** `automation`, `login`, `sso`, `locator`, `element`  
**Component:** Authentication

**Summary:**
```
TC_Login_SSORedirect consent page locator broken after provider UI update
```

**Description:**
```
Re-filing issue for TC_Login_SSORedirect. Previous report may be stale.
Fresh failure observed after latest SSO provider deployment.

Keyword: Verify Post-Redirect Page
Message: Locator '#sso-consent-button' does not match any element; DOM
snapshot shows '#consent-submit-btn' instead.
Duration: 19.8s

This is likely the same issue as the earlier report — the locator needs
updating to reflect the new provider UI.
```

**Why this pair is useful:** Both issues reference `TC_Login_SSORedirect` verbatim, were filed within 3 days of each other (inside the `window_days` gate), and their descriptions are semantically nearly identical (same keywords, same failure pattern). The `detect_duplicate_defects` function should surface this pair with `similarity ≥ 0.90` and `shared_test_names = ['tc_login_ssoredirect']`. Without duplicate detection, both issues would receive separate write-back comments and be counted as two distinct defects on the same failure — inflating defect metrics and confusing triagers.

---

## 8. Running the Pipeline

### Full pipeline flags reference

```
python pipeline.py [OPTIONS]

Ingestion
  --ingest                    Parse ./runs/ and load into analytics.db
  --runs-dir PATH             Override default ./runs/
  --database-path PATH        Override default ./analytics.db
  --force                     Re-ingest already-seen runs

Defect fetch
  --fetch-defects             Pull Bug issues from Jira REST API
  --since-days N              Fetch issues created in the last N days (default: 30)
  --tester-email EMAIL        Filter defects to a specific reporter email
  --extra-jql JQL             Append raw JQL to the base query
  --overwrite-defects         Replace existing defect rows instead of skipping

Defect mapping
  --map-defects               Run hybrid defect-to-test scoring
  --window-days N             Date proximity gate in days (default: 7)
  --min-confidence FLOAT      Discard mappings below this score (default: 0.25)
  --embed-model MODEL         HuggingFace model ID (default: BAAI/bge-small-en-v1.5)
  --no-semantic               Disable embedding scoring; rule-only mode
  --overwrite-mappings        Recompute existing mappings
  --force-reembed             Re-embed even if cached vectors exist

Write-back
  --writeback                 Post comments + update fields on confirmed Jira mappings
  --dry-run                   Simulate write-back without making any API calls
  --no-field-update           Post comments only; skip custom field update
  --dashboard-url URL         Embed this URL in every written comment

Duplicate detection
  --detect-duplicates         Run pairwise duplicate defect detection
  --dup-threshold FLOAT       Cosine similarity floor for duplicate pairs (default: 0.90)

Load from file (testing / offline)
  --load-defects PATH         Ingest defects from a local JSON file (bypasses live API)
```

---

## 9. Feature Reference: What Each Section Does and Why It Matters

### 9.1 Defect Fetch (`fetch-defects`)

**What it does:**  
Calls Jira REST API v3 `/search/jql` with a JQL query scoped to your configured projects and `issuetype = Bug AND labels in ("automation")`. Pages through results 100 at a time with automatic retry and exponential back-off on 429 / 5xx responses. Each issue is normalised from Jira's Atlassian Document Format (ADF) description into plain text and inserted into the `jira_defects` table.

**Why it matters:**  
Without this step, the mapping pipeline has no defects to match against. Fetching live from Jira ensures the local database is always a fresh mirror of what your team has actually filed, including status updates (e.g., a defect moving from `Triage` to `Closed - Fixed` is reflected on the next fetch). This also provides the raw material for all downstream features: mapping, write-back, and duplicate detection all read from `jira_defects`.

**When to use it:**  
Run once a day in CI (e.g., after the nightly regression suite) to keep the database current. Use `--since-days 30` for the rolling window and `--overwrite-defects` to refresh any defects whose status has changed.

---

### 9.2 Defect-to-Test Mapping (`map-defects`)

**What it does:**  
For every failing test result in `test_results`, the pipeline evaluates it against every defect in `jira_defects` using two gating pre-conditions and a hybrid two-component score.

**Gate checks (mandatory, applied first):**
- `reporter_email` on the Jira defect must match the `tester_email` argument (ensures only defects from your team are matched).
- The defect's `created` timestamp must fall within `window_days` of the run's `timestamp` (ensures temporal relevance).

Pairs failing either gate are skipped entirely — no row is written.

**Hybrid confidence score (0.0 – 1.0):**

| Signal | Component | Weight | Score added |
|---|---|---|---|
| Exact `TC_*` test name in defect summary or description | Rule (α) | 0.50 | +0.60 |
| Test name stem (e.g., "BulkImport") in defect text | Rule (α) | 0.50 | +0.25 |
| ≥1 shared diagnostic token between failure_msg and defect text | Rule (α) | 0.50 | +0.15 |
| Cosine similarity of BAAI/bge-small-en-v1.5 embeddings | Semantic (β) | 0.50 | 0.0 – 1.0 |

`final_score = 0.50 × rule_score + 0.50 × semantic_score`

A mapping is **confirmed** (`confirmed = 1`) when:
- `email_match = 1`
- `date_within_window = 1`
- `confidence_score ≥ 0.50`

Mappings with `score < 0.25` are discarded. Mappings between 0.25 and 0.50 are stored but marked unconfirmed — visible in the dashboard for review, but not acted on by write-back.

**Why it matters:**  
This is the core intelligence of the platform. Without it, a test failure and its Jira defect exist in completely separate systems with no connection between them. The hybrid approach gives you the best of both worlds: deterministic, explainable rule signals for common cases (test name appears in the defect) and semantic recovery for cases where the defect describes the failure in natural language without naming the test directly. The `match_reason` column stores a human-readable explanation of every signal that fired, so triagers can understand and audit why a particular mapping was made.

---

### 9.3 Write-Back to Jira (`writeback`)

**What it does:**  
For every row in `defect_test_mappings` where `confirmed = 1` and no prior successful sync exists in `jira_sync_log`, the pipeline posts a structured comment onto the Jira issue and optionally writes the CI run ID into a configurable custom field (`customfield_10042` by default).

A typical posted comment looks like this:

```
[automation-analytics] CI Run Linked

This defect was matched to an automated test failure by the Automation Test 
Stability Analytics pipeline.

Test:       TC_Login_MFAVerification
Run ID:     TeamAlpha_build_023
Timestamp:  2024-10-23T08:00:00
Score:      0.87
Reason:     exact test name 'TC_Login_MFAVerification' in defect text; 
            keyword overlap {'timeout', 'mfa'}; semantic similarity 0.831

Dashboard:  https://your-dashboard.internal
```

The `[automation-analytics]` sentinel is checked before posting. If it is already present in any existing comment on the issue, the write-back is **skipped** (`status = 'skipped'` in `jira_sync_log`). This makes the operation fully idempotent — safe to re-run on the same dataset indefinitely.

Every attempt (success, error, skipped) is recorded in `jira_sync_log`.

**Why it matters:**  
Write-back eliminates the manual step of a tester finding a failing test, looking up the Jira project, and manually linking the failure to a ticket. The triager opens the Jira issue and immediately sees which CI run triggered it, when, and why the system believed this was a match. This is especially valuable for `consistently_failing` tests like `TC_User_RoleAssignment` and `TC_Login_OAuthCallback`, where the same Jira defect accumulates comments from multiple runs over time, giving the assignee a complete failure history directly inside Jira without ever opening the dashboard.

**Use `--dry-run` first** to review what would be posted before making live API calls. The dry-run log is written to `jira_sync_log` with `dry_run = 1`.

---

### 9.4 Duplicate Defect Detection (`detect-duplicates`)

**What it does:**  
Performs an O(n²) pairwise comparison across all defects in `jira_defects`. A pair is flagged as a duplicate candidate when all three criteria hold simultaneously:

- Both defects reference at least one shared `TC_*` test name (extracted by regex from their summary + description).
- The two defects were created within `window_days` of each other (default 7 days).
- Their cosine similarity — computed from cached BAAI/bge-small-en-v1.5 embeddings — is ≥ `sim_threshold` (default 0.90). Falls back to Jaccard token overlap if embeddings are not cached.

Results are printed to stdout and sorted by similarity descending. At n < 500 defects the pairwise scan is fast; above that, consider running it on a filtered subset.

**Why it matters:**  
Duplicate defects are a common and expensive problem in test automation teams. When a flaky test fails on back-to-back CI runs, different team members often file separate Jira issues for what is the same underlying failure. Without detection:

- **Defect metrics are inflated.** A test that has 1 real root cause appears to have 3 open defects, making triage prioritisation unreliable.
- **Write-back posts duplicate comments** on both issues, creating noise for the assignee.
- **Fix tracking diverges.** One ticket gets marked `Closed - Fixed` while the duplicate stays open in `Triage`, creating a false signal that the issue is unresolved.

The threshold is intentionally high (0.90) to avoid false positives. At this threshold, flagged pairs almost certainly describe the same failure from different angles (as shown by Issue 4a and 4b in §7). The output gives the team actionable information: which two issues to merge or link in Jira, and exactly why they were flagged.

**Practical workflow:**  
After running `--detect-duplicates`, review the output and manually mark the lower-priority duplicate as `Won't Fix` or link it to the canonical issue in Jira. The next `--fetch-defects` run will pick up the status update.

---

### 9.5 Sync Log and Dashboard Health

**What it does:**  
Every write-back attempt is recorded in `jira_sync_log` with a status of `success`, `error`, or `skipped`. The `get_sync_stats()` helper in `jira_client.py` aggregates this into a summary dict consumed by the Streamlit dashboard to show a "Jira Sync Health" widget:

```python
{
    "total_synced": 42,
    "successes":    39,
    "errors":        2,
    "dry_runs":      1,
    "last_sync_at": "2024-10-30T08:15:00"
}
```

**Why it matters:**  
Without a sync log, you have no way to know whether write-back is actually working. The sync log gives you:

- **Idempotency enforcement** — the pipeline reads the log before posting to skip already-written mappings.
- **Error auditing** — the `error_msg` column stores the full exception text, so API failures (rate limits, auth errors, field permission errors) are diagnosable without digging through process logs.
- **Dashboard visibility** — the health widget shows at a glance if write-back is healthy or silently failing. A high `errors` count next to a low `successes` count is an immediate signal that Jira credentials or field permissions need attention.
- **Dry-run tracking** — `dry_run = 1` rows let you audit what would have been posted before committing to live write-back.

---

## 10. Idempotency and Safety

The integration is designed to be safe to run repeatedly without side effects:

| Concern | Protection mechanism |
|---|---|
| Posting duplicate Jira comments | `[automation-analytics]` sentinel checked before every POST; skipped if already present |
| Re-inserting already-seen defects | `jira_defects` uses `INSERT OR IGNORE` on `defect_id` |
| Re-running mapping on unchanged data | `defect_test_mappings` uses `INSERT OR IGNORE` on `mapping_id` unless `--overwrite-mappings` is passed |
| Write-back on already-synced mappings | `jira_sync_log` checked; `status = 'skipped'` if already written |
| Making live API calls during testing | `--dry-run` flag simulates all write-back operations and logs to `jira_sync_log` with `dry_run = 1` |
| Credentials leaking into shell history | All credentials read from `.env` file or environment variables, never accepted as CLI arguments |

---

## 11. Troubleshooting

**`EnvironmentError: Missing required Jira credentials`**  
Set `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` in your `.env` file. Run `python -c "from jira_client import load_credentials, test_connection; print(test_connection(load_credentials()))"` to verify.

**`401 Unauthorized` from Jira API**  
Your API token may have expired or been revoked. Regenerate it at `https://id.atlassian.com/manage-profile/security/api-tokens` and update `.env`.

**`400 Bad Request` on field update**  
`customfield_10042` may not exist in your Jira project, or your account may not have permission to edit it. Use `--no-field-update` to post comments only, and ask a Jira admin to verify the custom field ID and permissions.

**No mappings confirmed after `--map-defects`**  
Check that `--tester-email` matches the `reporter_email` field on your Jira defects exactly (normalised to lowercase). Also verify that defect `created` timestamps fall within `--window-days` of run timestamps in your dataset. Use `--window-days 90` temporarily to widen the window and confirm the pipeline is otherwise working.

**`FlagEmbedding not installed — running in rule-only mode`**  
Install with `pip install FlagEmbedding`. The pipeline continues in rule-only mode but semantic scoring is disabled, reducing match quality for defects that don't contain test names verbatim.

**Duplicate detection returns no results**  
The default `--dup-threshold 0.90` is intentionally high. Lower to `0.80` to surface near-duplicate pairs. If embeddings are not cached yet (no prior `--map-defects` run), the system falls back to Jaccard token overlap, which is less sensitive — run `--map-defects` first to populate the `embeddings` table.