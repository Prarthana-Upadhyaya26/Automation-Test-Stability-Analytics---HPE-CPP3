"""
jira_ingest.py — JIRA defect import and defect-to-test-run mapping.

Usage:
  python jira_ingest.py defects.json          # import + link
  python jira_ingest.py defects.json --db ./analytics.db
  python jira_ingest.py defects.csv           # CSV supported too
  python jira_ingest.py --link-only           # re-run matching without re-importing

This script is intentionally separate from pipeline.py.
pipeline.py is never modified; jira_ingest.py only appends to new tables.

Matching approaches (layered):
  A  — test name extracted from JIRA summary + ±7-day date window
  B  — additionally filters by reporter team via reporter_team_map table
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as _cos_sim

DEFAULT_DB = "./analytics.db"
AUTO_CONFIRM_THRESHOLD = 70
DATE_WINDOW_DAYS = 7
TFIDF_THRESHOLD = 0.15       # fallback when sentence-transformers not installed
EMBEDDING_THRESHOLD = 0.30   # cosine threshold for sentence-transformer embeddings
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
# How far back to look when a test was already failing before the defect was filed.
# Only activates when at least one FAIL run exists within DATE_WINDOW_DAYS before creation.
LATE_FILING_LOOKBACK_DAYS = 30

# Maps component name (from TC_<Component>_<Action>) to natural-language keywords.
# Used by the keyword_area strategy: if any keyword appears in the JIRA text, all
# tests in that component group become candidates.
COMPONENT_KEYWORDS: dict[str, list[str]] = {
    "Login":     ["login", "auth", "credential", "password", "oauth", "sso", "mfa",
                  "authentication", "signin", "sign-in", "session", "lockout"],
    "Dashboard": ["dashboard", "filter", "widget", "chart", "search", "pagination",
                  "refresh", "load", "display", "date range", "date-range"],
    "API":       ["api", "endpoint", "response", "schema", "rest", "request",
                  "http", "status", "profile", "json", "accountstatus"],
    "Report":    ["report", "generate", "monthly", "reporting"],
    "Export":    ["export", "csv", "audit", "file", "download", "logs", "audit log"],
    "User":      ["user", "account", "role", "batch", "bulk", "import",
                  "delete", "create", "edit", "profile", "permission"],
}

# All 23 canonical test names from config.py.
# Used for closed-vocabulary substring matching against JIRA summaries and descriptions.
CANONICAL_TESTS = [
    "TC_Login_ValidCredentials",
    "TC_Login_InvalidPassword",
    "TC_Login_SessionTimeout",
    "TC_Login_AccountLockout",
    "TC_Login_MFAVerification",
    "TC_Dashboard_FilterByDate",
    "TC_Dashboard_Pagination",
    "TC_Dashboard_ExportChart",
    "TC_Dashboard_SearchBar",
    "TC_User_CreateAccount",
    "TC_User_EditProfile",
    "TC_User_DeleteAccount",
    "TC_User_PasswordReset",
    "TC_Login_SSORedirect",
    "TC_Dashboard_LoadWidget",
    "TC_Dashboard_RefreshData",
    "TC_User_BulkImport",
    "TC_User_RoleAssignment",
    "TC_User_BatchExport",
    "TC_Login_OAuthCallback",
    "TC_API_UserProfile_Get",
    "TC_Report_GenerateMonthly",
    "TC_Export_AuditLogs",
]

# Known-flaky tests (fail_prob >= 0.30 in config.py).
# Used for a small confidence bonus when the JIRA label also says "flaky".
KNOWN_FLAKY = {
    "TC_Login_MFAVerification",
    "TC_Login_SSORedirect",
    "TC_Dashboard_LoadWidget",
    "TC_Dashboard_RefreshData",
    "TC_User_BulkImport",
    "TC_User_RoleAssignment",
    "TC_User_BatchExport",
    "TC_Login_OAuthCallback",
    "TC_API_UserProfile_Get",
    "TC_Report_GenerateMonthly",
    "TC_Export_AuditLogs",
}

# Maps JIRA labels to canonical test names.
# Add entries here as new tests or label conventions emerge.
LABEL_TO_TEST: dict[str, str] = {
    "bulk-import":      "TC_User_BulkImport",
    "batch-export":     "TC_User_BatchExport",
    "role-assignment":  "TC_User_RoleAssignment",
    "oauth":            "TC_Login_OAuthCallback",
    "sso":              "TC_Login_SSORedirect",
    "mfa":              "TC_Login_MFAVerification",
    "load-widget":      "TC_Dashboard_LoadWidget",
    "refresh-data":     "TC_Dashboard_RefreshData",
    "export-chart":     "TC_Dashboard_ExportChart",
    "filter-date":      "TC_Dashboard_FilterByDate",
    "pagination":       "TC_Dashboard_Pagination",
    "search-bar":       "TC_Dashboard_SearchBar",
    "valid-credentials":"TC_Login_ValidCredentials",
    "invalid-password": "TC_Login_InvalidPassword",
    "session-timeout":  "TC_Login_SessionTimeout",
    "account-lockout":  "TC_Login_AccountLockout",
    "create-account":   "TC_User_CreateAccount",
    "edit-profile":     "TC_User_EditProfile",
    "delete-account":   "TC_User_DeleteAccount",
    "password-reset":   "TC_User_PasswordReset",
    "schema-mismatch":  "TC_API_UserProfile_Get",
    "concurrency":      "TC_Report_GenerateMonthly",
    "audit":            "TC_Export_AuditLogs",
    "data-validation":  "TC_Export_AuditLogs",
}

# Maps feature keyword fragments (lowercase) to candidate test names.
# Used only when neither summary nor labels match.
KEYWORD_TO_TESTS: dict[str, list[str]] = {
    "bulkimport":   ["TC_User_BulkImport"],
    "batchexport":  ["TC_User_BatchExport"],
    "roleassign":   ["TC_User_RoleAssignment"],
    "oauthcallback":["TC_Login_OAuthCallback"],
    "ssored":       ["TC_Login_SSORedirect"],
    "mfaverif":     ["TC_Login_MFAVerification"],
    "loadwidget":       ["TC_Dashboard_LoadWidget"],
    "refreshdata":      ["TC_Dashboard_RefreshData"],
    "apiuserprofile":   ["TC_API_UserProfile_Get"],
    "generatemonthly":  ["TC_Report_GenerateMonthly"],
    "auditlogs":        ["TC_Export_AuditLogs"],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse ISO-8601 timestamps tolerantly, returning UTC-aware datetime or None."""
    if not ts:
        return None
    ts = ts.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _days_between(a: str, b: str) -> Optional[int]:
    da, db_ = _parse_iso(a), _parse_iso(b)
    if da is None or db_ is None:
        return None
    return abs((da.date() - db_.date()).days)


def _signed_days(defect_created: str, run_ts: str) -> Optional[int]:
    """Days from defect creation to run. Negative = run happened before defect (late-filing)."""
    da, db_ = _parse_iso(defect_created), _parse_iso(run_ts)
    if da is None or db_ is None:
        return None
    return (db_.date() - da.date()).days


def _test_component(test_name: str) -> str:
    """Extract component from TC_Component_Action format, e.g. 'TC_Login_Valid' → 'Login'."""
    parts = test_name.replace("TC_", "").split("_")
    return parts[0] if parts else ""


def _load_json_labels(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x).lower() for x in v] if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ── Semantic index (sentence-transformers with TF-IDF fallback) ───────────────

def build_semantic_index(conn: sqlite3.Connection):
    """
    Build a per-test semantic index from failure messages + test name words.

    Tries sentence-transformers (all-MiniLM-L6-v2) first. Falls back to
    TF-IDF when the package is not installed.

    Returns (model_or_vectorizer, embeddings_or_matrix, test_names, mode)
      mode: "embedding" | "tfidf" | None
    """
    rows = conn.execute(
        "SELECT test_name, failure_msg FROM test_results "
        "WHERE status='FAIL' AND failure_msg IS NOT NULL"
    ).fetchall()

    if not rows:
        return None, None, [], None

    docs: dict[str, list[str]] = defaultdict(list)
    for test_name, msg in rows:
        docs[test_name].append(msg)

    test_names = list(docs.keys())
    # Enrich each document with test-name words so "login" / "dashboard" etc.
    # appear even when failure_msg is generic boilerplate like "element not found".
    corpus = [
        tn.replace("TC_", "").replace("_", " ").lower() + " " + " ".join(msgs)
        for tn, msgs in zip(test_names, docs.values())
    ]

    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL)
        embeddings = model.encode(corpus, show_progress_bar=False)
        return model, embeddings, test_names, "embedding"
    except (ImportError, OSError):
        pass

    # Fallback: TF-IDF
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2), min_df=1, sublinear_tf=True, stop_words="english"
    )
    matrix = vectorizer.fit_transform(corpus)
    return vectorizer, matrix, test_names, "tfidf"


def semantic_top_k_v2(
    query: str,
    model_or_vec,
    index,
    test_names: list[str],
    mode: Optional[str],
    k: int = 3,
) -> list[tuple[str, float]]:
    """
    Return up to k (test_name, similarity) pairs above threshold.
    Dispatches to sentence-transformer cosine or TF-IDF cosine based on mode.
    """
    if mode is None or not test_names or not query.strip():
        return []

    if mode == "embedding":
        q_emb = model_or_vec.encode([query])
        sims = _cos_sim(q_emb, index).flatten()
        threshold = EMBEDDING_THRESHOLD
    else:
        q_vec = model_or_vec.transform([query])
        sims = _cos_sim(q_vec, index).flatten()
        threshold = TFIDF_THRESHOLD

    sorted_indices = sims.argsort()[::-1]
    results = []
    for idx in sorted_indices[:k]:
        score = float(sims[idx])
        if score >= threshold:
            results.append((test_names[int(idx)], score))
    return results


# ── Test-name extraction ───────────────────────────────────────────────────────

def extract_all_test_names(
    summary: str,
    description: Optional[str],
    labels_raw: Optional[str],
) -> list[tuple[str, str]]:
    """
    Return all (test_name, strategy) pairs found across summary, description,
    labels, and keyword fragments. Strategies are tried in priority order and
    the first non-empty tier wins (so label matches are not mixed with exact matches).

    strategy values: "exact_name" | "label_dict" | "keyword"
    Semantic fallback is handled separately in link_defects.
    """
    found: dict[str, str] = {}  # test_name -> strategy, insertion-ordered, deduped

    # Strategy A: exact canonical name in summary or description
    combined = summary + " " + (description or "")
    for tc in CANONICAL_TESTS:
        if tc in combined and tc not in found:
            found[tc] = "exact_name"
    if found:
        return list(found.items())

    # Strategy B: label dictionary — collect all matching labels
    labels = _load_json_labels(labels_raw)
    for lbl in labels:
        if lbl in LABEL_TO_TEST:
            tc = LABEL_TO_TEST[lbl]
            if tc not in found:
                found[tc] = "label_dict"
    if found:
        return list(found.items())

    # Strategy C: component keyword area — natural language terms in JIRA text
    # map to all tests in that component group (e.g. "login" → all TC_Login_*)
    text_lower = (summary + " " + (description or "")).lower()
    for tc in CANONICAL_TESTS:
        comp = _test_component(tc)
        keywords = COMPONENT_KEYWORDS.get(comp, [comp.lower()])
        if any(kw in text_lower for kw in keywords):
            if tc not in found:
                found[tc] = "keyword_area"
    if found:
        return list(found.items())

    # Strategy D: compressed keyword fragments in summary (legacy, narrow)
    summary_compact = re.sub(r"[_\s]", "", summary).lower()
    for kw, candidates in KEYWORD_TO_TESTS.items():
        if kw in summary_compact:
            for tc in candidates:
                if tc not in found:
                    found[tc] = "keyword"

    return list(found.items())


# ── Confidence scoring ─────────────────────────────────────────────────────────

def score_match(
    strategy: str,
    date_delta: int,
    labels: list[str],
    test_name: str,
    reporter_team_match: bool,
    jira_project: Optional[str],
    expected_project: Optional[str],
    jira_status: Optional[str],
    semantic_similarity: float = 0.0,
) -> int:
    score = 0

    if strategy == "exact_name":
        score += 50
    elif strategy == "label_dict":
        score += 30
    elif strategy == "keyword_area":
        score += 20
    elif strategy == "keyword":
        score += 15
    elif strategy == "semantic":
        # Points scale with similarity strength
        if semantic_similarity >= 0.40:
            score += 35
        elif semantic_similarity >= 0.20:
            score += 20
        else:
            score += 10

    if date_delta <= 2:
        score += 30
    elif date_delta <= DATE_WINDOW_DAYS:
        score += 10

    if reporter_team_match:
        score += 20

    if expected_project and jira_project == expected_project:
        score += 10

    if "flaky" in labels and test_name in KNOWN_FLAKY:
        score += 10

    if jira_status in ("Triage", "In Progress"):
        score += 5

    return min(score, 100)


# ── Database helpers ───────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the jira_defects / reporter_team_map / defect_test_links tables if absent."""
    schema_sql = Path(__file__).parent / "schema.sql"
    conn.executescript(schema_sql.read_text(encoding="utf-8"))
    conn.commit()


def _get_reporter_team(conn: sqlite3.Connection, email: str) -> Optional[str]:
    row = conn.execute(
        "SELECT team FROM reporter_team_map WHERE email = ?", (email,)
    ).fetchone()
    return row[0] if row else None


def _get_team_project(team: str) -> Optional[str]:
    """Heuristic: map known team names to their expected JIRA project key."""
    mapping = {
        "TeamAlpha": "KAN",   # update with real project keys from JIRA → Project settings → Key
        "TeamBeta":  "CSSE",
        "TeamGamma": "MCIO",
    }
    return mapping.get(team)


# ── JIRA Cloud live fetch ─────────────────────────────────────────────────────

def _adf_to_text(node: object) -> str:
    """Recursively extract plain text from an Atlassian Document Format (ADF) node."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        node_type = node.get("type", "")
        # Hard breaks and paragraphs become newlines
        if node_type in ("hardBreak", "rule"):
            return "\n"
        text = node.get("text", "")
        children = node.get("content", [])
        result = text + "".join(_adf_to_text(c) for c in children)
        if node_type in ("paragraph", "heading", "listItem", "bulletList", "orderedList"):
            result += "\n"
        return result
    if isinstance(node, list):
        return "".join(_adf_to_text(c) for c in node)
    return ""


def fetch_jira_live(
    jql: str = "issuetype = Bug ORDER BY created DESC",
    max_results: int = 200,
    base_url: Optional[str] = None,
    email: Optional[str] = None,
    token: Optional[str] = None,
) -> list[dict]:
    """
    Fetch defects from JIRA Cloud REST API v3.

    Credentials are read from env vars by default:
        JIRA_URL   — e.g. https://yoursite.atlassian.net
        JIRA_EMAIL — your Atlassian account email
        JIRA_TOKEN — API token from id.atlassian.com/manage-profile/security/api-tokens

    Returns records in the same format as a JSON import file.
    """
    try:
        import requests
        from requests.auth import HTTPBasicAuth
    except ImportError:
        sys.exit("requests not installed — run: pip install requests")

    base_url = (base_url or os.environ.get("JIRA_URL", "")).rstrip("/")
    email    = email or os.environ.get("JIRA_EMAIL", "")
    token    = token or os.environ.get("JIRA_TOKEN", "")

    if not base_url or not email or not token:
        sys.exit(
            "JIRA credentials missing. Set env vars:\n"
            "  JIRA_URL   = https://yoursite.atlassian.net\n"
            "  JIRA_EMAIL = you@example.com\n"
            "  JIRA_TOKEN = your_api_token"
        )

    auth    = HTTPBasicAuth(email, token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    fields  = ["summary", "description", "reporter", "status", "priority",
               "issuetype", "project", "labels", "components", "created"]

    records = []
    next_page_token = None

    while True:
        payload: dict = {"jql": jql, "maxResults": min(100, max_results), "fields": fields}
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        resp = requests.post(
            f"{base_url}/rest/api/3/search/jql",
            auth=auth,
            headers=headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code == 401:
            sys.exit("JIRA auth failed — check JIRA_EMAIL and JIRA_TOKEN.")
        if resp.status_code == 400:
            try:
                body = resp.json()
                err = body.get("errorMessages") or body.get("errors") or resp.text
            except Exception:
                err = resp.text
            sys.exit(f"Bad JQL or payload: {err}")
        resp.raise_for_status()

        data   = resp.json()
        issues = data.get("issues", [])

        for issue in issues:
            f = issue.get("fields", {})

            # Description: JIRA Cloud returns ADF (JSON), not plain text
            raw_desc = f.get("description")
            description = _adf_to_text(raw_desc).strip() if isinstance(raw_desc, dict) else (raw_desc or "")

            reporter = f.get("reporter") or {}
            records.append({
                "key":           issue["key"],
                "summary":       f.get("summary", ""),
                "description":   description,
                "reporter_name": reporter.get("emailAddress", ""),
                "status":        (f.get("status") or {}).get("name", ""),
                "priority":      (f.get("priority") or {}).get("name", ""),
                "issuetype":     (f.get("issuetype") or {}).get("name", ""),
                "project":       (f.get("project") or {}).get("key", ""),
                "labels":        f.get("labels", []),
                "components":    [c.get("name", "") for c in (f.get("components") or [])],
                "created":       f.get("created", ""),
            })

        print(f"  Fetched {len(records)} issue(s) from JIRA so far…")
        next_page_token = data.get("nextPageToken")
        if not issues or not next_page_token or len(records) >= max_results:
            break

    print(f"  Done — {len(records)} issue(s) retrieved from {base_url}")
    return records


# ── Import ─────────────────────────────────────────────────────────────────────

def _insert_records(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Insert a list of defect dicts into jira_defects. Skips duplicates. Returns count inserted."""
    inserted = 0
    for r in records:
        jira_key = r.get("key") or r.get("jira_key", "")
        if not jira_key:
            print(f"  [SKIP] record missing 'key' field: {r}", file=sys.stderr)
            continue

        existing = conn.execute(
            "SELECT 1 FROM jira_defects WHERE jira_key = ?", (jira_key,)
        ).fetchone()
        if existing:
            print(f"  [SKIP] already imported: {jira_key}")
            continue

        reporter_email = r.get("reporter_name") or r.get("reporter_email", "")
        labels = r.get("labels")
        if isinstance(labels, list):
            labels = json.dumps(labels)
        components = r.get("components")
        if isinstance(components, list):
            components = json.dumps(components)

        conn.execute(
            """INSERT INTO jira_defects
               (jira_key, summary, description, reporter_email, status, priority,
                issuetype, project, labels, components, created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                jira_key,
                r.get("summary", ""),
                r.get("description"),
                reporter_email,
                r.get("status"),
                r.get("priority"),
                r.get("issuetype"),
                r.get("project"),
                labels,
                components,
                r.get("created"),
            ),
        )
        inserted += 1
        print(f"  [OK]   imported {jira_key}")

    conn.commit()
    return inserted


def import_defects(conn: sqlite3.Connection, path: Path) -> int:
    """Load defects from a JSON or CSV file into jira_defects. Returns count inserted."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        records = raw if isinstance(raw, list) else [raw]
    elif suffix == ".csv":
        import csv
        with path.open(newline="", encoding="utf-8") as f:
            records = list(csv.DictReader(f))
    else:
        raise ValueError(f"Unsupported file type: {suffix}. Use .json or .csv")

    return _insert_records(conn, records)


def import_defects_live(
    conn: sqlite3.Connection,
    jql: str,
    max_results: int = 200,
    base_url: Optional[str] = None,
    email: Optional[str] = None,
    token: Optional[str] = None,
) -> int:
    """Fetch defects live from JIRA Cloud and insert into jira_defects. Returns count inserted."""
    records = fetch_jira_live(jql=jql, max_results=max_results,
                               base_url=base_url, email=email, token=token)
    return _insert_records(conn, records)


# ── Matching ───────────────────────────────────────────────────────────────────

def link_defects(conn: sqlite3.Connection) -> int:
    """
    For every defect in jira_defects, find candidate failing test runs within
    the date window and insert rows into defect_test_links.

    Matching cascade (ALL hits collected within the winning tier):
      1. exact_name  — TC_ identifier found verbatim in summary or description
      2. label_dict  — JIRA label maps to a known test name
      3. keyword     — feature keyword fragment found in summary
      4. semantic    — TF-IDF cosine top-K on summary + description (fallback only)

    Already-linked (jira_key, run_id, test_name) triples are skipped so
    this function is safe to call repeatedly.
    """
    # Build semantic index once (sentence-transformers or TF-IDF fallback)
    sem_model, sem_index, sem_test_names, sem_mode = build_semantic_index(conn)
    if sem_mode == "embedding":
        print(f"  Semantic index: {len(sem_test_names)} test(s) [sentence-transformers]")
    elif sem_mode == "tfidf":
        print(f"  Semantic index: {len(sem_test_names)} test(s), {sem_index.shape[1]} terms [TF-IDF fallback]")
    else:
        print("  Semantic index unavailable (no failure messages in DB yet)")

    defects = conn.execute(
        """SELECT jira_key, summary, description, labels, reporter_email,
                  status, project, created
           FROM jira_defects"""
    ).fetchall()

    linked = 0
    for jira_key, summary, description, labels_raw, reporter_email, jira_status, jira_project, created in defects:

        summary = summary or ""

        # Strategies 1-3: exact names, labels, keywords (all matches collected)
        matched = extract_all_test_names(summary, description, labels_raw)

        # Strategy 5: semantic fallback — only when nothing found above
        semantic_sims: dict[str, float] = {}
        if not matched:
            query = summary + " " + (description or "")
            for tc, sim in semantic_top_k_v2(query, sem_model, sem_index, sem_test_names, sem_mode):
                matched.append((tc, "semantic"))
                semantic_sims[tc] = sim

        if not matched:
            print(f"  [SKIP] {jira_key}: no match (all strategies failed)")
            continue

        reporter_team = _get_reporter_team(conn, reporter_email or "")
        expected_project = _get_team_project(reporter_team) if reporter_team else None
        labels = _load_json_labels(labels_raw)

        for test_name, strategy in matched:
            semantic_similarity = semantic_sims.get(test_name, 0.0)

            # Find all FAIL runs for this test
            candidates = conn.execute(
                """SELECT tr.run_id, r.timestamp, r.team
                   FROM test_results tr
                   JOIN runs r ON tr.run_id = r.run_id
                   WHERE tr.test_name = ? AND tr.status = 'FAIL'""",
                (test_name,),
            ).fetchall()

            # Late-filing detection: if the test was already failing within DATE_WINDOW_DAYS
            # before the defect was created, extend the backward window so those earlier
            # runs are also linked (defect was filed late, not that the test just started failing).
            late_filing = any(
                -DATE_WINDOW_DAYS <= (_signed_days(created, run_ts) or 999) <= 0
                for _, run_ts, _ in candidates
            )
            backward_window = LATE_FILING_LOOKBACK_DAYS if late_filing else DATE_WINDOW_DAYS

            for run_id, run_ts, run_team in candidates:
                signed = _signed_days(created, run_ts)
                if signed is None:
                    continue
                # Forward: run after defect creation — keep tight window
                # Backward: run before defect creation — extend if late-filing detected
                if signed > DATE_WINDOW_DAYS or signed < -backward_window:
                    continue
                delta = abs(signed)

                reporter_team_match = (reporter_team is not None and reporter_team == run_team)
                if reporter_team is not None and not reporter_team_match:
                    continue

                exists = conn.execute(
                    """SELECT 1 FROM defect_test_links
                       WHERE jira_key=? AND run_id=? AND test_name=?""",
                    (jira_key, run_id, test_name),
                ).fetchone()
                if exists:
                    continue

                confidence = score_match(
                    strategy=strategy,
                    date_delta=delta,
                    labels=labels,
                    test_name=test_name,
                    reporter_team_match=reporter_team_match,
                    jira_project=jira_project,
                    expected_project=expected_project,
                    jira_status=jira_status,
                    semantic_similarity=semantic_similarity,
                )

                auto_confirm = 1 if confidence >= AUTO_CONFIRM_THRESHOLD else 0

                conn.execute(
                    """INSERT INTO defect_test_links
                       (jira_key, run_id, test_name, match_strategy,
                        confidence, date_delta_days, cosine_sim_score, confirmed)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        jira_key, run_id, test_name, strategy,
                        confidence, delta,
                        semantic_similarity if strategy == "semantic" else None,
                        auto_confirm,
                    ),
                )
                status_label = "AUTO-CONFIRMED" if auto_confirm else "PENDING"
                sim_tag = f", sim={semantic_similarity:.2f}" if strategy == "semantic" else ""
                late_tag = " [late-filing]" if late_filing and delta > DATE_WINDOW_DAYS else ""
                print(f"  [{status_label}] {jira_key} -> {run_id}/{test_name} "
                      f"(score={confidence}, delta={delta}d, via {strategy}{sim_tag}{late_tag})")
                linked += 1

    conn.commit()
    return linked


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import JIRA defects and map them to test run failures.")
    p.add_argument("input", nargs="?", help="Path to defects JSON or CSV file (omit when using --jira)")
    p.add_argument("--db", default=DEFAULT_DB, help="Path to analytics.db (default: ./analytics.db)")
    p.add_argument("--link-only", action="store_true", help="Skip import; re-run matching only")

    # Live JIRA Cloud fetch
    p.add_argument("--jira", action="store_true", help="Fetch defects live from JIRA Cloud instead of a file")
    p.add_argument("--jql", default="issuetype = Bug ORDER BY created DESC",
                   help="JQL query for --jira mode (default: all Bugs, newest first)")
    p.add_argument("--jira-url",   default=None, help="JIRA base URL (overrides JIRA_URL env var)")
    p.add_argument("--jira-email", default=None, help="Atlassian account email (overrides JIRA_EMAIL)")
    p.add_argument("--jira-token", default=None, help="JIRA API token (overrides JIRA_TOKEN)")
    p.add_argument("--max-results", type=int, default=200, help="Max issues to fetch from JIRA (default: 200)")
    p.add_argument("--date-window", type=int, default=None,
                   help=f"Override DATE_WINDOW_DAYS for matching (default: {DATE_WINDOW_DAYS}). "
                        "Use a large value (e.g. 730) to link against older runs in demo environments.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    print(f"Using database: {db_path.resolve()}")
    _ensure_schema(conn)

    if not args.link_only:
        if args.jira:
            print(f"\n-- Fetching defects live from JIRA Cloud --")
            print(f"   JQL: {args.jql}")
            n_imported = import_defects_live(
                conn,
                jql=args.jql,
                max_results=args.max_results,
                base_url=args.jira_url,
                email=args.jira_email,
                token=args.jira_token,
            )
            print(f"   Imported: {n_imported} new defect(s)")
        elif args.input:
            input_path = Path(args.input)
            if not input_path.exists():
                sys.exit(f"Input file not found: {input_path}")
            print(f"\n-- Importing defects from {input_path} --")
            n_imported = import_defects(conn, input_path)
            print(f"   Imported: {n_imported} new defect(s)")
        else:
            sys.exit("Provide a defects JSON/CSV file, use --jira to fetch live, or --link-only to re-run matching.")

    print("\n-- Running defect -> test run matching --")
    if args.date_window:
        global DATE_WINDOW_DAYS, LATE_FILING_LOOKBACK_DAYS
        DATE_WINDOW_DAYS = args.date_window
        LATE_FILING_LOOKBACK_DAYS = args.date_window * 4
        print(f"   [DEMO] date window overridden to ±{args.date_window} days")
    n_linked = link_defects(conn)
    print(f"   Candidate links created: {n_linked}")

    # Summary
    total_defects = conn.execute("SELECT COUNT(*) FROM jira_defects").fetchone()[0]
    total_links   = conn.execute("SELECT COUNT(*) FROM defect_test_links").fetchone()[0]
    confirmed     = conn.execute("SELECT COUNT(*) FROM defect_test_links WHERE confirmed=1").fetchone()[0]
    pending       = conn.execute("SELECT COUNT(*) FROM defect_test_links WHERE confirmed=0").fetchone()[0]

    print(f"\n-- Summary --")
    print(f"   Total defects in DB : {total_defects}")
    print(f"   Total links in DB   : {total_links}")
    print(f"   Auto-confirmed      : {confirmed}")
    print(f"   Pending review      : {pending}")
    conn.close()


if __name__ == "__main__":
    main()
