"""
jira_client.py — Jira REST API v3 integration layer
=====================================================
Automation Test Stability Analytics — Live Jira Integration

Responsibilities
────────────────
  fetch_defects()        Pull defects from Jira via JQL, paginated, with
                         automatic retry and rate-limit back-off.

  write_mapping_comment() Post a structured comment on a Jira issue linking
                          it to a specific CI run and test failure.  Idempotent
                          — will not post a duplicate comment if the same
                          run_id is already recorded on the issue.

  update_automation_field() Write the CI run ID into a custom Jira field
                            (configurable per project in .env / config).

  detect_duplicate_defects() Surface pairs of defects in the local DB that
                             likely describe the same failure (same test name,
                             overlapping time window, high semantic similarity).

  get_project_config()   Read per-project threshold / field overrides from
                         the environment or a .env file.

Credentials — NEVER pass on the command line
────────────────────────────────────────────
Set these environment variables (or put them in a .env file next to this
script — python-dotenv will load them automatically):

    JIRA_BASE_URL   https://your-org.atlassian.net
    JIRA_EMAIL      tester@hpe.com
    JIRA_API_TOKEN  <Personal Access Token from id.atlassian.com/manage-profile/security>
    JIRA_PROJECTS   CSSOSE,CSSE,MCIO          (comma-separated, default: all three)

Optional per-project overrides (prefix with project key):
    CSSOSE_CONFIRM_THRESHOLD   0.60   (override global 0.50)
    CSSE_CONFIRM_THRESHOLD     0.55
    MCIO_CONFIRM_THRESHOLD     0.50
    CSSOSE_AUTOMATION_FIELD    customfield_10042   (Jira custom field for run ID)
    CSSE_AUTOMATION_FIELD      customfield_10042
    MCIO_AUTOMATION_FIELD      customfield_10042

Dashboard .env integration
──────────────────────────
The dashboard reads JIRA_BASE_URL from the environment to construct clickable
issue links.  Set it once and all defect keys in the UI become hyperlinks.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urljoin, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Load .env if present (safe no-op if file is missing) ─────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed — environment variables must be set manually

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_JIRA_API_V3        = "/rest/api/3"
_SEARCH_PAGE_SIZE   = 100          # Jira max per page
_RATE_LIMIT_SLEEP   = 1.0          # seconds between pages (polite default)
_RETRY_TOTAL        = 5            # total retries on transient errors
_RETRY_BACKOFF      = 1.5          # exponential back-off factor
_COMMENT_TAG        = "[automation-analytics]"  # sentinel in comments for idempotency
_DEFAULT_PROJECTS   = ("CSSOSE", "CSSE", "MCIO")
_DEFAULT_CONFIRM_THRESHOLD = 0.50
_WRITEBACK_FIELDS = {
    # Maps a human-readable label to the Jira custom field ID used for the
    # automation run link.  Override per project via env var (see module docs).
    "automation_run_id": "customfield_10042",
}

# ── Credentials helper ────────────────────────────────────────────────────────

class JiraCredentials:
    """
    Immutable value object holding validated Jira connection parameters.

    Loads from environment variables; raises EnvironmentError with a clear
    message if any required variable is missing so the user knows exactly
    what to set.
    """

    __slots__ = ("base_url", "email", "token", "projects", "_auth_header")

    def __init__(
        self,
        base_url:  Optional[str] = None,
        email:     Optional[str] = None,
        token:     Optional[str] = None,
        projects:  Optional[list[str]] = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("JIRA_BASE_URL", "")).rstrip("/")
        self.email    = email or os.environ.get("JIRA_EMAIL", "")
        self.token    = token or os.environ.get("JIRA_API_TOKEN", "")

        raw_projects  = os.environ.get("JIRA_PROJECTS", ",".join(_DEFAULT_PROJECTS))
        self.projects = projects or [p.strip() for p in raw_projects.split(",") if p.strip()]

        self._validate()

        # Pre-compute Basic auth header (email:token, base64-encoded)
        creds = f"{self.email}:{self.token}".encode()
        self._auth_header = "Basic " + base64.b64encode(creds).decode()

    def _validate(self) -> None:
        missing = []
        if not self.base_url:
            missing.append("JIRA_BASE_URL")
        if not self.email:
            missing.append("JIRA_EMAIL")
        if not self.token:
            missing.append("JIRA_API_TOKEN")
        if missing:
            raise EnvironmentError(
                f"Missing required Jira credentials: {', '.join(missing)}\n"
                f"Set them as environment variables or in a .env file:\n"
                + "\n".join(f"  {v}=<value>" for v in missing)
            )
        if not self.base_url.startswith("https://"):
            raise EnvironmentError(
                f"JIRA_BASE_URL must start with 'https://'; got: {self.base_url!r}"
            )

    @property
    def auth_header(self) -> str:
        return self._auth_header

    def issue_url(self, issue_key: str) -> str:
        """Construct the browser URL for a Jira issue key."""
        return f"{self.base_url}/browse/{issue_key}"

    def api_url(self, path: str) -> str:
        """Construct a full REST API URL from a relative path."""
        return self.base_url + _JIRA_API_V3 + path

    def __repr__(self) -> str:
        return f"JiraCredentials(base_url={self.base_url!r}, email={self.email!r}, projects={self.projects})"


def load_credentials(
    base_url: Optional[str] = None,
    email:    Optional[str] = None,
    token:    Optional[str] = None,
    projects: Optional[list[str]] = None,
) -> JiraCredentials:
    """
    Convenience factory — returns a validated JiraCredentials object.

    Parameters override environment variables when supplied.  Pass nothing to
    rely entirely on environment / .env file (recommended for production).
    """
    return JiraCredentials(base_url=base_url, email=email, token=token, projects=projects)


# ── HTTP session factory ──────────────────────────────────────────────────────

def _make_session(creds: JiraCredentials) -> requests.Session:
    """
    Build a requests.Session with:
      - Basic auth header pre-set
      - Automatic retry with exponential back-off for 429 / 5xx responses
      - 30-second connect + read timeout (set per-call, not here)
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": creds.auth_header,
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "User-Agent":    "automation-analytics/1.0 (+https://github.com/your-org/repo)",
    })

    retry = Retry(
        total            = _RETRY_TOTAL,
        backoff_factor   = _RETRY_BACKOFF,
        status_forcelist = {429, 500, 502, 503, 504},
        allowed_methods  = {"GET", "POST", "PUT"},
        raise_on_status  = False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


# ── Per-project configuration ─────────────────────────────────────────────────

def get_project_config(project: str) -> dict:
    """
    Return per-project configuration merged from environment variables.

    Keys returned
    ─────────────
    confirm_threshold   float   Minimum hybrid score for confirmed = 1
    automation_field    str     Jira custom field ID for CI run link
    window_days         int     Date window override (default: 7)

    Environment variable naming convention:
        {PROJECT}_CONFIRM_THRESHOLD     e.g. CSSOSE_CONFIRM_THRESHOLD=0.60
        {PROJECT}_AUTOMATION_FIELD      e.g. CSSOSE_AUTOMATION_FIELD=customfield_10042
        {PROJECT}_WINDOW_DAYS           e.g. CSSOSE_WINDOW_DAYS=7
    """
    pfx = project.upper().replace("-", "_")
    return {
        "confirm_threshold": float(
            os.environ.get(f"{pfx}_CONFIRM_THRESHOLD",
            os.environ.get("CONFIRM_THRESHOLD", str(_DEFAULT_CONFIRM_THRESHOLD)))
        ),
        "automation_field": os.environ.get(
            f"{pfx}_AUTOMATION_FIELD",
            os.environ.get("AUTOMATION_FIELD", _WRITEBACK_FIELDS["automation_run_id"]),
        ),
        "window_days": int(
            os.environ.get(f"{pfx}_WINDOW_DAYS",
            os.environ.get("DEFECT_WINDOW_DAYS", "7"))
        ),
    }


# ── Fetch defects from Jira ───────────────────────────────────────────────────

def _build_jql(
    projects: list[str],
    since_days: int,
    reporter_email: Optional[str],
    extra_jql: Optional[str],
) -> str:
    """
    Build the base JQL for fetching Bug issues.

    Only filters by project and issue type by default.
    Additional JQL can be supplied via extra_jql if needed.
    """

    proj_list = ", ".join(f'"{p}"' for p in projects)

    clauses = [
        f"project IN ({proj_list})",
        "issuetype = Bug",
    ]

    # Optional custom filters
    if extra_jql:
        clauses.append(f"({extra_jql})")

    return " AND ".join(clauses) + " ORDER BY created DESC"


def _fields_to_request() -> list[str]:
    """List of Jira field names to include in every search response."""
    return [
        "summary", "description", "status", "priority",
        "reporter", "created", "updated", "labels",
        "components", "issuetype", "project",
        "comment",   # needed for idempotency check in write-back
    ]


def _parse_adf_to_text(content) -> str:
    """
    Recursively extract plain text from Atlassian Document Format (ADF).

    Jira REST API v3 returns description as ADF JSON, not a plain string.
    This function walks the node tree and concatenates all text nodes.
    Falls back gracefully if the input is already a plain string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        node_type = content.get("type", "")
        if node_type == "text":
            return content.get("text", "")
        if node_type in ("hardBreak", "rule"):
            return "\n"
        parts = []
        for child in content.get("content", []):
            parts.append(_parse_adf_to_text(child))
        sep = "\n" if node_type in ("paragraph", "bulletList", "listItem",
                                     "orderedList", "heading", "blockquote",
                                     "codeBlock", "panel") else ""
        return sep.join(parts)

    if isinstance(content, list):
        return "\n".join(_parse_adf_to_text(item) for item in content)

    return str(content)


def _normalise_rest_v3_issue(raw: dict) -> dict:
    """
    Convert a Jira REST API v3 issue (with "fields" sub-dict) to the flat
    format expected by parse_jira_defect() in pipeline.py.
    """
    fields = raw.get("fields", {})

    reporter = fields.get("reporter") or {}
    status   = (fields.get("status")   or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    issuetype = (fields.get("issuetype") or {}).get("name", "Bug")
    project  = (fields.get("project")  or {}).get("key", "")

    description_raw = fields.get("description")
    description_txt = _parse_adf_to_text(description_raw)

    components_raw = fields.get("components", [])
    components = [c.get("name", "") for c in components_raw if isinstance(c, dict)]

    return {
        "key":           raw.get("key", ""),
        "summary":       fields.get("summary", ""),
        "description":   description_txt,
        "reporter":      reporter.get("displayName", ""),
        "reporter_name": reporter.get("emailAddress", ""),
        "status":        status,
        "priority":      priority,
        "issuetype":     issuetype,
        "project":       project,
        "labels":        fields.get("labels", []),
        "components":    components,
        "created":       fields.get("created", ""),
        "updated":       fields.get("updated", ""),
        # Pass through for idempotency check in write-back
        "_comments":     [
            c.get("body", "") for c in
            ((fields.get("comment") or {}).get("comments") or [])
        ],
    }


def fetch_defects(
    creds:          JiraCredentials,
    *,
    since_days:     int            = 7,
    reporter_email: Optional[str]  = None,
    extra_jql:      Optional[str]  = None,
    rate_limit_s:   float          = _RATE_LIMIT_SLEEP,
) -> Generator[dict, None, None]:
    """
    Yield normalised defect dicts pulled live from Jira REST API v3.

    Handles pagination automatically — yields every matching issue across
    all pages.  Each yielded dict is in the flat format accepted by
    pipeline.parse_jira_defect(), so it can be passed directly to
    pipeline.ingest_jira_defects().

    Parameters
    ──────────
    creds           : JiraCredentials (from load_credentials())
    since_days      : only fetch issues created in the last N days
    reporter_email  : optional JQL reporter filter (pre-condition (a))
    extra_jql       : raw JQL fragment appended to the base query
    rate_limit_s    : sleep between pages (default 1 s, respects Jira rate limits)

    Yields
    ──────
    dict — flat normalised defect ready for pipeline.parse_jira_defect()

    Raises
    ──────
    requests.HTTPError   on non-retried HTTP errors (4xx except 429)
    EnvironmentError     if credentials are missing / malformed
    """
    session = _make_session(creds)
    jql     = _build_jql(creds.projects, since_days, reporter_email, extra_jql)
    fields  = _fields_to_request()
    url     = creds.api_url("/search/jql")
    start   = 0
    total   = None

    logger.info("Jira fetch — JQL: %s", jql)

    while True:
        params = {
            "jql":        jql,
            "fields":     ",".join(fields),
            "maxResults": _SEARCH_PAGE_SIZE,
            "startAt":    start,
        }

        try:
            resp = session.get(url, params=params, timeout=(10, 30))
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            logger.error("Jira API error %s: %s", status_code, exc)
            raise

        data   = resp.json()
        issues = data.get("issues", [])

        if total is None:
            total = data.get("total", 0)
            logger.info("Jira fetch — %d matching issues", total)

        for issue in issues:
            flat = _normalise_rest_v3_issue(issue)
            yield flat

        start += len(issues)
        if start >= total or not issues:
            break

        time.sleep(rate_limit_s)

    logger.info("Jira fetch complete — %d issues retrieved", start)


def test_connection(creds: JiraCredentials) -> dict:
    """
    Verify Jira credentials are valid by calling the /myself endpoint.

    Returns
    ───────
    dict — {"ok": True, "display_name": "...", "email": "..."} on success
           {"ok": False, "error": "..."} on failure
    """
    session = _make_session(creds)
    try:
        resp = session.get(creds.api_url("/myself"), timeout=(5, 10))
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok":           True,
            "display_name": data.get("displayName", ""),
            "email":        data.get("emailAddress", ""),
            "account_id":   data.get("accountId", ""),
        }
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code == 401:
            return {"ok": False, "error": "Authentication failed — check JIRA_EMAIL and JIRA_API_TOKEN"}
        if code == 403:
            return {"ok": False, "error": "Permission denied — API token may lack required scopes"}
        return {"ok": False, "error": f"HTTP {code}: {exc}"}
    except requests.ConnectionError as exc:
        return {"ok": False, "error": f"Connection error — check JIRA_BASE_URL: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Write-back: post comment ──────────────────────────────────────────────────

def _build_comment_body(
    run_id:           str,
    test_name:        str,
    confidence_score: float,
    match_reason:     str,
    run_timestamp:    str,
    dashboard_url:    Optional[str] = None,
) -> dict:
    """
    Build an Atlassian Document Format (ADF) comment body.

    The comment is structured with a sentinel tag so the idempotency check
    can find it on subsequent runs without parsing free-form text.

    Format:
    ───────
    [automation-analytics] Linked by Automation Test Stability Analytics
    CI Run  : TeamAlpha_build_047
    Test    : TC_User_BulkImport
    Score   : 0.91 (exact test name 'TC_User_BulkImport' in defect text; ...)
    Timestamp: 2024-11-15T08:32:00
    Dashboard: <link if provided>
    """
    lines = [
        f"{_COMMENT_TAG} Linked by Automation Test Stability Analytics",
        f"CI Run     : {run_id}",
        f"Test       : {test_name}",
        f"Score      : {confidence_score:.3f}",
        f"Reason     : {match_reason}",
        f"Run time   : {run_timestamp}",
    ]
    if dashboard_url:
        lines.append(f"Dashboard  : {dashboard_url}")

    # ADF paragraph per line
    paragraphs = []
    for line in lines:
        paragraphs.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": line}],
        })

    return {
        "version": 1,
        "type":    "doc",
        "content": paragraphs,
    }


def _comment_already_posted(existing_comment_bodies: list, run_id: str) -> bool:
    """
    Return True if a comment for this run_id already exists on the issue.

    Checks for the sentinel tag + run_id string in previously fetched
    comment bodies (plain text extracted from ADF during fetch).
    This makes write-back idempotent — safe to call repeatedly.
    """
    needle = f"{_COMMENT_TAG}"
    run_needle = f"CI Run     : {run_id}"
    for body in existing_comment_bodies:
        body_text = _parse_adf_to_text(body) if isinstance(body, dict) else str(body)
        if needle in body_text and run_needle in body_text:
            return True
    return False


def write_mapping_comment(
    creds:            JiraCredentials,
    issue_key:        str,
    run_id:           str,
    test_name:        str,
    confidence_score: float,
    match_reason:     str,
    run_timestamp:    str,
    *,
    existing_comments: Optional[list] = None,
    dashboard_url:    Optional[str]   = None,
    dry_run:          bool            = False,
) -> dict:
    """
    Post a structured comment on a Jira issue linking it to a CI run failure.

    Idempotent — checks existing comments for the sentinel tag + run_id
    before posting.  Will never post more than one comment per run_id per issue.

    Parameters
    ──────────
    creds             : JiraCredentials
    issue_key         : e.g. "CSSOSE-0002"
    run_id            : e.g. "TeamAlpha_build_047"
    test_name         : e.g. "TC_User_BulkImport"
    confidence_score  : hybrid score from pipeline (0–1)
    match_reason      : human-readable reason string from _hybrid_score()
    run_timestamp     : ISO timestamp of the CI run
    existing_comments : ADF body dicts fetched during fetch_defects()
                        (pass None to skip idempotency check — not recommended)
    dashboard_url     : optional link to the Streamlit dashboard
    dry_run           : if True, build the comment but do not POST it

    Returns
    ───────
    dict — {"posted": bool, "skipped": bool, "reason": str, "comment_url": str|None}
    """
    # ── Idempotency check ─────────────────────────────────────────────────────
    if existing_comments is not None:
        if _comment_already_posted(existing_comments, run_id):
            return {
                "posted":      False,
                "skipped":     True,
                "reason":      f"Comment for run_id '{run_id}' already exists on {issue_key}",
                "comment_url": None,
            }

    body = _build_comment_body(
        run_id, test_name, confidence_score, match_reason, run_timestamp, dashboard_url
    )

    if dry_run:
        return {
            "posted":      False,
            "skipped":     False,
            "reason":      "dry_run=True — comment not posted",
            "comment_url": None,
            "body_preview": _parse_adf_to_text(body),
        }

    # ── POST comment ──────────────────────────────────────────────────────────
    session = _make_session(creds)
    url     = creds.api_url(f"/issue/{issue_key}/comment")

    try:
        resp = session.post(url, json={"body": body}, timeout=(10, 30))
        resp.raise_for_status()
        data        = resp.json()
        comment_id  = data.get("id", "")
        comment_url = f"{creds.base_url}/browse/{issue_key}?focusedCommentId={comment_id}"
        logger.info("Posted comment on %s (run: %s, score: %.3f)", issue_key, run_id, confidence_score)
        return {
            "posted":      True,
            "skipped":     False,
            "reason":      "Comment posted successfully",
            "comment_url": comment_url,
        }
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        msg  = f"HTTP {code} posting comment to {issue_key}: {exc}"
        logger.error(msg)
        return {"posted": False, "skipped": False, "reason": msg, "comment_url": None}
    except Exception as exc:
        msg = f"Unexpected error posting comment to {issue_key}: {exc}"
        logger.error(msg)
        return {"posted": False, "skipped": False, "reason": msg, "comment_url": None}


# ── Write-back: update custom field ──────────────────────────────────────────

def _build_field_update_value(field_schema: Optional[dict], value: str):
    """Return a Jira-compatible value for a field update payload.

    Jira expects different JSON shapes depending on the field type.  For
    array-backed fields such as Components or Labels, the value must be a list
    of strings.  For single-value string fields it should stay a plain string.
    """
    if not field_schema:
        return value

    field_type = (field_schema.get("type") or "").lower()
    if field_type == "array":
        return [value]
    if field_type in {"option", "select"}:
        return {"value": value}
    if field_type in {"string", "text", "date", "datetime", "number", "float", "double"}:
        return value
    return value


def update_automation_field(
    creds:        JiraCredentials,
    issue_key:    str,
    run_id:       str,
    project:      str,
    *,
    dry_run:      bool = False,
) -> dict:
    """
    Write the CI run ID into a configurable Jira custom field.

    The target field ID is resolved via get_project_config() so different
    projects can use different custom fields.  If the field ID is not
    configured (empty string), this function is a no-op.

    Parameters
    ──────────
    creds     : JiraCredentials
    issue_key : Jira issue key
    run_id    : CI run ID to write (e.g. "TeamAlpha_build_047")
    project   : Jira project key — used to look up per-project field ID
    dry_run   : if True, do not PUT, just return what would be sent

    Returns
    ───────
    dict — {"updated": bool, "skipped": bool, "reason": str}
    """
    cfg      = get_project_config(project)
    field_id = cfg["automation_field"]

    if not field_id:
        return {
            "updated": False,
            "skipped": True,
            "reason":  f"No automation_field configured for project {project}",
        }

    session = _make_session(creds)
    url     = creds.api_url(f"/issue/{issue_key}")

    field_schema = None
    try:
        field_resp = session.get(creds.api_url("/field"), timeout=(10, 30))
        field_resp.raise_for_status()
        for field in field_resp.json():
            if field.get("id") == field_id:
                field_schema = field.get("schema") or {}
                break
    except Exception as exc:
        logger.warning("Could not read Jira field schema for %s: %s", field_id, exc)

    field_value = _build_field_update_value(field_schema, run_id)
    payload = {"fields": {field_id: field_value}}

    if dry_run:
        return {
            "updated": False,
            "skipped": False,
            "reason":  f"dry_run=True — would PUT {payload} to {issue_key}",
        }

    try:
        resp = session.put(url, json=payload, timeout=(10, 30))
        resp.raise_for_status()
        logger.info("Updated field '%s' on %s → %s", field_id, issue_key, run_id)
        return {
            "updated": True,
            "skipped": False,
            "reason":  f"Field '{field_id}' set to '{run_id}'",
        }
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        # 400 often means the field doesn't exist or is read-only
        msg  = f"HTTP {code} updating field '{field_id}' on {issue_key}"
        if exc.response is not None:
            try:
                err_detail = exc.response.json()
                msg += f": {err_detail}"
            except Exception:
                pass
        logger.error(msg)
        return {"updated": False, "skipped": False, "reason": msg}
    except Exception as exc:
        msg = f"Unexpected error updating field on {issue_key}: {exc}"
        logger.error(msg)
        return {"updated": False, "skipped": False, "reason": msg}


# ── Bulk write-back from DB ───────────────────────────────────────────────────

def write_back_confirmed_mappings(
    conn:           sqlite3.Connection,
    creds:          JiraCredentials,
    *,
    dashboard_url:  Optional[str] = None,
    dry_run:        bool          = False,
    update_field:   bool          = True,
    force:          bool          = False,
) -> dict:
    """
    For every confirmed mapping not yet written back, post a comment and
    optionally update the custom field on the Jira issue.

    Tracks write-back state in jira_sync_log (written, skipped, errors).
    Re-running is always safe — already-written mappings are skipped via
    both the DB log and the idempotency check inside write_mapping_comment().

    Parameters
    ──────────
    conn          : open sqlite3 connection (row_factory = sqlite3.Row)
    creds         : JiraCredentials
    dashboard_url : optional Streamlit URL embedded in the comment
    dry_run       : do not actually call Jira — log what would happen
    update_field  : also PUT the run ID into the custom automation field

    Returns
    ───────
    dict — keys: attempted, comments_posted, fields_updated, skipped, errors
    """
    stats = {
        "attempted":        0,
        "comments_posted":  0,
        "fields_updated":   0,
        "skipped":          0,
        "errors":           0,
    }

    # Load confirmed mappings to process. By default we skip already-synced
    # mappings via jira_sync_log, but force=True reprocesses all confirmed
    # mappings so the sync log can be refreshed after a bug fix.
    if force:
        rows = conn.execute(
            """
            SELECT m.mapping_id, m.defect_id, m.run_id, m.test_name,
                   m.confidence_score, m.match_reason, m.run_timestamp,
                   m.defect_project
            FROM   defect_test_mappings m
            WHERE  m.confirmed = 1
            ORDER  BY m.confidence_score DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT m.mapping_id, m.defect_id, m.run_id, m.test_name,
                   m.confidence_score, m.match_reason, m.run_timestamp,
                   m.defect_project
            FROM   defect_test_mappings m
            LEFT JOIN jira_sync_log s ON m.mapping_id = s.mapping_id
            WHERE  m.confirmed = 1
              AND  s.mapping_id IS NULL
            ORDER  BY m.confidence_score DESC
            """
        ).fetchall()

    if not rows:
        logger.info("write_back: no new confirmed mappings to process.")
        return stats

    # Build a session and pre-fetch comment lists per issue to minimise API calls
    # (one GET /issue/{key}/comment per unique defect_id, not per mapping)
    session          = _make_session(creds)
    comment_cache: dict[str, list] = {}

    def _get_comments(issue_key: str) -> list:
        if issue_key in comment_cache:
            return comment_cache[issue_key]
        try:
            url  = creds.api_url(f"/issue/{issue_key}/comment")
            resp = session.get(url, timeout=(10, 30))
            resp.raise_for_status()
            data = resp.json()
            bodies = [c.get("body", "") for c in data.get("comments", [])]
        except Exception as exc:
            logger.warning("Could not fetch comments for %s: %s", issue_key, exc)
            bodies = []
        comment_cache[issue_key] = bodies
        return bodies

    cursor = conn.cursor()

    for row in rows:
        stats["attempted"] += 1
        mapping_id  = row["mapping_id"]
        issue_key   = row["defect_id"]
        run_id      = row["run_id"]
        log_status  = "success"
        log_error   = None

        try:
            # ── 1. Post comment ───────────────────────────────────────────────
            existing = _get_comments(issue_key)
            c_result = write_mapping_comment(
                creds,
                issue_key         = issue_key,
                run_id            = run_id,
                test_name         = row["test_name"],
                confidence_score  = row["confidence_score"],
                match_reason      = row["match_reason"],
                run_timestamp     = row["run_timestamp"],
                existing_comments = existing,
                dashboard_url     = dashboard_url,
                dry_run           = dry_run,
            )
            if c_result["posted"]:
                stats["comments_posted"] += 1
                # Invalidate cache so next mapping for same issue sees the new comment
                comment_cache.pop(issue_key, None)
            elif c_result["skipped"]:
                stats["skipped"] += 1

            # ── 2. Update automation custom field ─────────────────────────────
            if update_field:
                f_result = update_automation_field(
                    creds,
                    issue_key = issue_key,
                    run_id    = run_id,
                    project   = row["defect_project"],
                    dry_run   = dry_run,
                )
                if f_result["updated"]:
                    stats["fields_updated"] += 1

        except Exception as exc:
            log_status = "error"
            log_error  = str(exc)[:500]
            stats["errors"] += 1
            logger.error("write_back error for %s / %s: %s", issue_key, mapping_id, exc)

        # ── 3. Record in jira_sync_log ────────────────────────────────────────
        if not dry_run:
            cursor.execute(
                """
                INSERT OR REPLACE INTO jira_sync_log
                    (mapping_id, defect_id, run_id, status, error_msg, dry_run)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mapping_id, issue_key, run_id, log_status, log_error, 0),
            )

        time.sleep(0.3)   # polite pacing between issues

    if not dry_run:
        conn.commit()

    return stats


# ── Duplicate defect detection ────────────────────────────────────────────────

def detect_duplicate_defects(
    conn:            sqlite3.Connection,
    *,
    model_name:      str   = "BAAI/bge-small-en-v1.5",
    sim_threshold:   float = 0.90,
    window_days:     int   = 7,
) -> list[dict]:
    """
    Surface pairs of Jira defects in the local DB that likely describe the
    same underlying failure and should be merged or linked in Jira.

    Detection criteria (all three must hold)
    ─────────────────────────────────────────
    (a) Same test name appears in both defect summaries / descriptions
    (b) Both defects were created within `window_days` of each other
    (c) Cosine similarity between their cached embeddings ≥ sim_threshold
        (falls back to lexical overlap if embeddings are not cached)

    Parameters
    ──────────
    conn          : open sqlite3 connection
    model_name    : embedding model — must match the one used in pipeline
    sim_threshold : cosine similarity floor (default 0.90, intentionally high
                    to avoid false positives in the duplicate case)
    window_days   : max days between the two defects' created timestamps

    Returns
    ───────
    list of dicts, each with keys:
        defect_a, defect_b, similarity, date_diff_days,
        shared_test_names, reason
    Sorted by similarity descending.
    """
    defects = conn.execute(
        "SELECT defect_id, summary, description, created FROM jira_defects"
    ).fetchall()

    if len(defects) < 2:
        return []

    # Try to load embeddings
    try:
        import numpy as np
        emb_rows = conn.execute(
            "SELECT entity_id, vector FROM embeddings WHERE entity_type='jira_defect' AND model_name=?",
            (model_name,),
        ).fetchall()
        vecs = {r["entity_id"]: np.frombuffer(r["vector"], dtype=np.float32) for r in emb_rows}
        has_embeddings = bool(vecs)
    except ImportError:
        vecs = {}
        has_embeddings = False

    # Import cosine helper lazily (same implementation as pipeline.py)
    def _cos(a, b) -> float:
        import numpy as np
        a = np.asarray(a, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0

    # Test-name extractor — finds TC_* tokens in text
    _tc_re = re.compile(r"\bTC_[A-Z][A-Za-z0-9_]+", re.IGNORECASE)

    def _test_names(text: str) -> set[str]:
        return {m.lower() for m in _tc_re.findall(text or "")}

    # Pairwise comparison O(n²) — acceptable for typical defect volumes (< 500)
    from datetime import datetime as _dt

    def _parse_ts(ts: str) -> Optional[_dt]:
        try:
            return _dt.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    duplicates = []
    n = len(defects)

    for i in range(n):
        da = defects[i]
        ts_a = _parse_ts(da["created"])
        text_a = f"{da['summary']} {da['description'] or ''}"
        names_a = _test_names(text_a)
        vec_a   = vecs.get(da["defect_id"])

        for j in range(i + 1, n):
            db_ = defects[j]
            ts_b = _parse_ts(db_["created"])

            # Gate (b): date proximity
            if ts_a and ts_b:
                diff_days = abs((ts_a - ts_b).total_seconds()) / 86400.0
                if diff_days > window_days:
                    continue
            else:
                diff_days = float("nan")

            text_b  = f"{db_['summary']} {db_['description'] or ''}"
            names_b = _test_names(text_b)

            # Gate (a): shared test names
            shared = names_a & names_b
            if not shared:
                continue

            # Gate (c): similarity
            vec_b = vecs.get(db_["defect_id"])
            if has_embeddings and vec_a is not None and vec_b is not None:
                sim    = _cos(vec_a, vec_b)
                method = f"cosine ({model_name})"
            else:
                # Fallback: Jaccard of token sets
                from pipeline import _tokenise  # type: ignore[import]
                toks_a = _tokenise(text_a)
                toks_b = _tokenise(text_b)
                union  = toks_a | toks_b
                sim    = len(toks_a & toks_b) / len(union) if union else 0.0
                method = "jaccard (no embeddings)"

            if sim < sim_threshold:
                continue

            duplicates.append({
                "defect_a":         da["defect_id"],
                "defect_b":         db_["defect_id"],
                "similarity":       round(sim, 4),
                "date_diff_days":   round(diff_days, 2) if not (diff_days != diff_days) else None,
                "shared_test_names": sorted(shared),
                "reason": (
                    f"shared tests {sorted(shared)}; "
                    f"{method} similarity {sim:.3f} ≥ {sim_threshold}"
                ),
            })

    duplicates.sort(key=lambda x: x["similarity"], reverse=True)
    return duplicates


# ── Sync log helpers (for pipeline.py and dashboard.py) ──────────────────────

def load_sync_log(conn: sqlite3.Connection):
    """Return the jira_sync_log table as a list of Row objects."""
    return conn.execute(
        """
        SELECT mapping_id, defect_id, run_id, status, error_msg,
               written_at, dry_run
        FROM   jira_sync_log
        ORDER  BY written_at DESC
        """
    ).fetchall()


def get_sync_stats(conn: sqlite3.Connection) -> dict:
    """Compute summary statistics over jira_sync_log for the dashboard."""
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                            AS total_synced,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successes,
            SUM(CASE WHEN status = 'error'   THEN 1 ELSE 0 END) AS errors,
            SUM(CASE WHEN dry_run = 1        THEN 1 ELSE 0 END) AS dry_runs,
            MAX(written_at)                                     AS last_sync_at
        FROM jira_sync_log
        """
    ).fetchone()
    return {
        "total_synced": row["total_synced"] or 0,
        "successes":    row["successes"]    or 0,
        "errors":       row["errors"]       or 0,
        "dry_runs":     row["dry_runs"]     or 0,
        "last_sync_at": row["last_sync_at"] or "never",
    }