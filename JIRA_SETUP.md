# JIRA Integration — Setup & Run Guide

## Prerequisites

Make sure the project virtual environment is active and dependencies are installed:

```cmd
.hpe_project\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` includes `requests` (JIRA API) and `sentence-transformers` (semantic matching).  
The first run downloads the embedding model (~80MB, one-time only).

---

## Step 1 — JIRA Cloud Setup

### 1.1 Create a free Atlassian account
Go to [atlassian.com](https://atlassian.com) and sign up for a free account.

### 1.2 Create a JIRA Software project
1. In JIRA, click **Projects → Create project**
2. Choose **Software** (Scrum or Kanban) — NOT Work Management
3. Give it a name. Note the **project key** (e.g. `KAN`) from Project Settings → Details

### 1.3 Create Bug issues
1. Click **Create** in JIRA
2. Set **Issue type = Bug**
3. Write a summary and description that describes a test failure
4. Optionally add labels (e.g. `search-bar`, `flaky`) to trigger label-based matching

### 1.4 Generate an API token
1. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**
3. Give it a label (e.g. `jira-ingest`)
4. Copy the token immediately — it is only shown once

---

## Step 2 — Set Credentials

Create or edit `set_jira_env.bat` in the project root:

```bat
set JIRA_URL=https://your-site.atlassian.net
set JIRA_EMAIL=you@example.com
set JIRA_TOKEN=your_api_token_here
```

Replace the values with your actual site URL, Atlassian account email, and the token from Step 1.4.

> **Security note:** Never commit `set_jira_env.bat` to git. It is already in `.gitignore`.  
> If you accidentally expose your token, revoke it immediately at the link above and generate a new one.

Run the file in CMD before any JIRA command:

```cmd
set_jira_env.bat
```

---

## Step 3 — Import Defects from JIRA

### Full run (fetch + match)
```cmd
set_jira_env.bat
python jira_ingest.py --jira --jql "project = KAN AND issuetype = Bug" --date-window 730
```

- `--jira` — fetch live from JIRA Cloud instead of a local file
- `--jql` — JQL query to filter issues (change `KAN` to your project key)
- `--date-window 730` — match against runs up to 730 days away (use this for demo/synthetic data where JIRA issues are newer than the test runs)

### Re-run matching only (no re-fetch)
```cmd
python jira_ingest.py --link-only --date-window 730
```

Useful when you want to re-run the matching logic without hitting the JIRA API again.

### Import from a local JSON file instead
```cmd
python jira_ingest.py defects.json
```

---

## Step 4 — Understanding the Output

```
-- Fetching defects live from JIRA Cloud --
   JQL: project = KAN AND issuetype = Bug
  Fetched 5 issue(s) from JIRA so far...
  Done — 5 issue(s) retrieved from https://your-site.atlassian.net
  [OK]   imported KAN-2
  [SKIP] already imported: KAN-3        ← already in DB, skipped

-- Running defect -> test run matching --
  Semantic index: 22 test(s) [sentence-transformers]
  [AUTO-CONFIRMED] KAN-5 -> beta_build_037/TC_Login_ValidCredentials (score=80, delta=1d, via exact_name)
  [PENDING]        KAN-3 -> beta_build_040/TC_Export_AuditLogs (score=30, delta=592d, via keyword_area)
```

### Match strategies (in priority order)

| Strategy | How it works | Score base |
|----------|-------------|------------|
| `exact_name` | TC_ test case name found verbatim in JIRA summary or description | 50 |
| `label_dict` | JIRA label maps to a known test (e.g. `search-bar` → `TC_Dashboard_SearchBar`) | 30 |
| `keyword_area` | Natural language keywords matched to component group (e.g. "login" → all `TC_Login_*` tests) | 20 |
| `keyword` | Compressed keyword fragment in summary (legacy fallback) | 15 |
| `semantic` | Sentence-transformer embedding similarity — no shared words needed | 10–35 |

### Confidence score

Score = strategy base + date proximity + team match + project match + flaky signal + status signal (max 100).

| Threshold | Result |
|-----------|--------|
| ≥ 70 | **AUTO-CONFIRMED** — system accepts without human review |
| < 70 | **PENDING** — goes to the review queue in the dashboard |

### Link states in the database

| `confirmed` value | Meaning |
|-------------------|---------|
| `0` | Pending — awaiting human decision |
| `1` | Confirmed — auto or manually accepted |
| `-1` | Rejected — human marked as incorrect |

---

## Step 5 — Review in Dashboard

```cmd
streamlit run dashboard.py
```

Navigate to the **JIRA Review** section to:
- See all pending links
- Accept a link → sets `confirmed = 1`
- Reject a link → sets `confirmed = -1`

---

## Common Issues

| Error | Fix |
|-------|-----|
| `JIRA auth failed` | Check `JIRA_EMAIL` and `JIRA_TOKEN` in `set_jira_env.bat`. Re-run the bat file. |
| `[SKIP] already imported: KAN-x` | Issue already in DB. Delete from `jira_defects` if you need to reimport. |
| `Candidate links created: 0` | JIRA issue text has no matching keywords or TC_ names. Add domain keywords to the description. |
| `sentence-transformers not found` | Run `pip install sentence-transformers` |
| Model downloads on first run | Normal — `all-MiniLM-L6-v2` (~80MB) is downloaded once and cached. |

---

## Quick Reference

```cmd
# Set credentials (run this every new CMD session)
set_jira_env.bat

# Full import + match
python jira_ingest.py --jira --jql "project = KAN AND issuetype = Bug" --date-window 730

# Re-run matching only
python jira_ingest.py --link-only --date-window 730

# Import from file
python jira_ingest.py defects.json

# Clear KAN links (for demo reset)
python -c "import sqlite3; c=sqlite3.connect('analytics.db'); c.execute(\"DELETE FROM defect_test_links WHERE jira_key LIKE 'KAN-%'\"); c.commit(); print('cleared')"

# Launch dashboard
streamlit run dashboard.py
```
