import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "runs_dir":      "./runs",
    "database_path": "./analytics.db",
    "schema_path":   "./schema.sql",
    "batch_size":    50,
    "force":         False,
}


SCHEMA_MAP = {
    "schema_v2": {
        "run_id":         "run_id",
        "team":           "team",
        "suite_name":     "suite_name",
        "build_no":       "build_no",
        "timestamp":      "timestamp",
        "total":          "total",
        "passed":         "passed",
        "failed":         "failed",
        "pass_rate_pct":  "pass_rate_pct",
        "environment":    "environment",
        "executor":       "executor",
        "duration_s":     "duration_s",
        # test_results
        "test_name":      "test_name",
        "status":         "status",
        "test_duration_s": "duration_s",
        "failure_msg":    "failure_msg",
        "failure_kw":     "failure_kw",
        "tags":           "tags",
    },
    "schema_v1": {
        "run_id":         "run_id",
        "team":           None,
        "suite_name":     None,
        "build_no":       "build_number",
        "timestamp":      "timestamp",
        "total":          "total_tests",
        "passed":         "passed",
        "failed":         "failed",
        "pass_rate_pct":  "pass_rate",
        "environment":    "environment",
        "executor":       "executor",
        "duration_s":     None,
        "test_name":      "test_name",
        "status":         "status",
        "test_duration_s": "duration",
        "failure_msg":    "message",
        "failure_kw":     "keyword_name",
        "tags":           None,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  JIRA DEFECT INGESTION & DEFECT-TO-TEST-RUN MAPPING
#  ─────────────────────────────────────────────────────────────────────────────
#
#  Architecture: hybrid two-stage scoring pipeline
#
#  Stage 1 — Gate checks (mandatory, cheap, applied before any scoring)
#    (a) reporter_email == tester_email for the CI run
#    (b) |defect.created − run.timestamp| ≤ DEFECT_WINDOW_DAYS (default 7)
#    Pairs failing both gates are skipped entirely (no DB row written).
#
#  Stage 2 — Hybrid confidence score  [0.0 – 1.0]
#
#    Component A: Rule-based lexical signals     weight α = 0.50
#    ─────────────────────────────────────────────────────────────
#    +0.60  exact TC_* test name found verbatim in defect summary or description
#    +0.25  test-name stem words (e.g. "bulkimport", "ssredirect") in defect text
#    +0.15  ≥1 shared diagnostic token between failure_msg and defect text
#    (sub-score capped at 1.0 before applying α)
#
#    Component B: Semantic cosine similarity     weight β = 0.50
#    ─────────────────────────────────────────────────────────────
#    Model:  BAAI/bge-small-en-v1.5  (FlagEmbedding, 33 M params, 512-dim)
#    Input A: failure_msg from test_results
#    Input B: defect summary + "\n\n" + first 512 chars of description
#    Similarity: cosine similarity of L2-normalised embeddings
#    Embeddings are computed once and cached in the `embeddings` table as
#    little-endian float32 BLOBs (via numpy tobytes/frombuffer).
#    On import failure of FlagEmbedding, semantic_score = 0 and α is raised
#    to 1.0 automatically (full rule-based fallback, no exceptions raised).
#
#    final_score = α × rule_score + β × semantic_score   (capped at 1.0)
#
#  Thresholds
#    confirmed = 1  iff  email_match AND date_within_window AND score ≥ 0.50
#    rows with score < DEFECT_MIN_CONFIDENCE (0.25) are discarded silently.
#
#  Explainability
#    match_reason is a human-readable semicolon-delimited string of every
#    signal that fired, e.g.:
#      "exact test name 'TC_User_BulkImport' in defect text;
#       failure keyword overlap {'csv', 'rows', 'processed'};
#       semantic similarity 0.912"
# ─────────────────────────────────────────────────────────────────────────────

import pickle
from typing import TYPE_CHECKING

DEFECT_WINDOW_DAYS:    int   = 7
DEFECT_MIN_CONFIDENCE: float = 0.25

# Hybrid score weights — must sum to 1.0
_ALPHA_RULE:     float = 0.50   # lexical rule-based component
_BETA_SEMANTIC:  float = 0.50   # semantic embedding component

# Default embedding model — swap via --embed-model CLI flag
DEFAULT_EMBED_MODEL: str = "BAAI/bge-small-en-v1.5"

# Cosine similarity threshold below which the semantic signal is not credited
# in match_reason (it still contributes to score, but isn't labelled as a
# "match" in the human-readable explanation).
_SEMANTIC_REASON_THRESHOLD: float = 0.70

# Stopwords for lexical tokenisation — intentionally minimal; only tokens
# that are *always* diagnostic noise in CI failure / Jira text.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "do", "for", "from", "has", "have", "in", "is", "it", "its",
    "not", "of", "on", "or", "that", "the", "this", "to", "was",
    "were", "which", "with", "you", "your",
})

# Module-level embedding model cache — loaded once per process.
# Type is FlagModel | None; we avoid a hard import at module level so the
# whole pipeline remains usable without FlagEmbedding installed.
_EMBED_MODEL_CACHE: dict[str, object] = {}


def _get_embed_model(model_name: str = DEFAULT_EMBED_MODEL) -> Optional[object]:
    """
    Lazily load and cache a BAAI FlagModel.

    Returns the model object on success, or None if FlagEmbedding is not
    installed or the model cannot be loaded (graceful degradation).
    """
    if model_name in _EMBED_MODEL_CACHE:
        return _EMBED_MODEL_CACHE[model_name]

    try:
        from FlagEmbedding import FlagModel  # type: ignore[import]
        import numpy as np  # noqa: F401  (confirm numpy is also present)

        print(f"  ↳ Loading embedding model '{model_name}' …", flush=True)
        model = FlagModel(
            model_name,
            query_instruction_for_retrieval=(
                "Represent this automation failure log for matching Jira defects:"
            ),
            use_fp16=True,
        )
        _EMBED_MODEL_CACHE[model_name] = model
        print(f"  ✓ Embedding model loaded: {model_name}")
        return model

    except ImportError:
        _EMBED_MODEL_CACHE[model_name] = None
        print(
            "  ⚠  FlagEmbedding not installed — running in rule-only mode.\n"
            "     Install with:  pip install FlagEmbedding",
            file=sys.stderr,
        )
        return None

    except Exception as exc:
        _EMBED_MODEL_CACHE[model_name] = None
        print(f"  ⚠  Could not load '{model_name}': {exc} — rule-only mode.", file=sys.stderr)
        return None


def _vec_to_blob(vec) -> bytes:
    """Serialise a 1-D numpy float32 array to a compact BLOB."""
    import numpy as np
    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes):
    """Deserialise a BLOB back to a 1-D numpy float32 array."""
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)


def _cosine_similarity(a, b) -> float:
    """Cosine similarity between two 1-D numpy arrays (already L2-normalised)."""
    import numpy as np
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── Jira JSON normalisation ───────────────────────────────────────────────────

def _normalise_email(raw: str) -> str:
    """
    Lowercase-strip an email address.

    Handles markdown-formatted strings like "[user@hpe.com](mailto:user@hpe.com)"
    that appear in Jira API responses when description fields are rendered as
    markdown.  Returns '' for None / non-email input.
    """
    if not raw:
        return ""
    raw = str(raw).strip()
    # Extract the actual address from markdown link wrappers
    match = re.search(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", raw)
    return match.group(0).lower() if match else raw.lower()


def _parse_jira_timestamp(ts: str) -> Optional[datetime]:
    """
    Parse an ISO-8601 Jira timestamp to a UTC-naive datetime.

    Handles the common Jira API format "2026-06-05T08:15:00.000+0000"
    as well as RFC-3339 variants with colon separators in the offset.
    Always returns a UTC-naive datetime for uniform SQLite comparison.
    """
    if not ts:
        return None
    ts = ts.strip()
    # Jira emits "+0000" not "+00:00"; fromisoformat requires the colon form.
    ts = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", ts)
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None) - dt.utcoffset()
        return dt
    except (ValueError, TypeError):
        return None


def parse_jira_defect(raw: dict) -> Optional[dict]:
    """
    Normalise a single Jira defect payload into a flat dict ready for
    insertion into the jira_defects table.

    Accepts payloads in either the synthetic template format used in this
    project (flat dict with "key", "summary", "description", …) or the
    Jira REST API v3 format (nested "fields" sub-dict).

    Returns None when the defect_id ("key") is absent or the created
    timestamp is missing/unparseable — both are mandatory for the mapping
    pipeline to function correctly.
    """
    if not raw or not raw.get("key"):
        return None

    # Support Jira REST API v3 nested fields format
    fields = raw.get("fields", raw)

    created_dt  = _parse_jira_timestamp(raw.get("created") or fields.get("created", ""))
    created_iso = created_dt.isoformat() if created_dt else None
    if not created_iso:
        return None

    # Reporter fields (supports both REST payloads and already-normalised payloads)
    reporter_field = fields.get("reporter")

    if isinstance(reporter_field, dict):
        reporter_name = reporter_field.get("displayName", "")
        reporter_email = reporter_field.get("emailAddress", "")
    else:
        reporter_name = (
            raw.get("reporter_name")
            or raw.get("reporter")
            or ""
        )
        reporter_email = (
            raw.get("reporter_email")
            or raw.get("reporter_name")
            or ""
        )

    reporter_email = _normalise_email(reporter_email)

    summary     = str(raw.get("summary") or fields.get("summary", "")).strip()
    description = str(
        raw.get("description") or fields.get("description") or ""
    ).strip() or None

    labels     = raw.get("labels") or fields.get("labels", [])
    components_raw = raw.get("components") or fields.get("components", [])
    # REST API returns [{"name": "..."}] dicts; flatten to strings
    if components_raw and isinstance(components_raw[0], dict):
        components = [c.get("name", "") for c in components_raw]
    else:
        components = list(components_raw)

    return {
        "defect_id":      raw["key"].strip(),
        "project":        str(raw.get("project") or (fields.get("project") or {}).get("key", "")).strip(),
        "summary":        summary,
        "description":    description,
        "reporter_name":  str(reporter_name).strip() or None,
        "reporter_email": reporter_email,
        "status":         str(raw.get("status") or (fields.get("status") or {}).get("name", "") or "").strip() or None,
        "priority":       str(raw.get("priority") or (fields.get("priority") or {}).get("name", "") or "").strip() or None,
        "issue_type":     str(raw.get("issuetype") or (fields.get("issuetype") or {}).get("name", "") or "").strip() or None,
        "labels":         json.dumps(labels if isinstance(labels, list) else []),
        "components":     json.dumps(components),
        "created":        created_iso,
        "raw_json":       json.dumps(raw),
    }


def load_jira_defects_from_file(json_path: str) -> list[dict]:
    """
    Load and parse Jira defect records from a JSON file.

    Accepts four payload shapes:
      1. Single flat defect object     { "key": "CSSOSE-0001", ... }
      2. List of defect objects        [ { "key": ... }, ... ]
      3. Jira API search result        { "issues": [ ... ] }
      4. Named dict of defects         { "cssose_0001": { "key": ... }, ... }

    Records failing validation (missing key / unparseable timestamp) are
    logged to stderr and skipped; the rest are returned for DB insertion.
    """
    with open(json_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if isinstance(raw, dict):
        if "issues" in raw:
            records = raw["issues"]
        elif "key" in raw:
            records = [raw]
        else:
            records = [v for v in raw.values() if isinstance(v, dict)]
    elif isinstance(raw, list):
        records = raw
    else:
        print(f"  ✗ Unrecognised Jira JSON structure in {json_path}", file=sys.stderr)
        return []

    parsed, skipped = [], 0
    for item in records:
        result = parse_jira_defect(item)
        if result:
            parsed.append(result)
        else:
            key = item.get("key", "<unknown>") if isinstance(item, dict) else "<unknown>"
            print(f"  ⚠  Skipping '{key}' — missing key or unparseable timestamp",
                  file=sys.stderr)
            skipped += 1

    if skipped:
        print(f"  ⚠  {skipped} record(s) skipped in {json_path}")
    return parsed


def load_jira_defects_from_list(raw_list: list[dict]) -> list[dict]:
    """Parse a list of raw Jira defect dicts (already decoded from JSON)."""
    parsed = []
    for item in raw_list:
        result = parse_jira_defect(item)
        if result:
            parsed.append(result)
    return parsed


# ── DB insertion ─────────────────────────────────────────────────────────────

def ingest_jira_defects(
    conn:      sqlite3.Connection,
    defects:   list[dict],
    *,
    overwrite: bool = False,
) -> dict:
    """
    Upsert parsed defect dicts into the jira_defects table.

    Parameters
    ----------
    conn      : open sqlite3 connection (row_factory = sqlite3.Row)
    defects   : list of dicts from parse_jira_defect()
    overwrite : INSERT OR REPLACE when True; INSERT OR IGNORE when False

    Returns
    -------
    dict — keys: inserted, skipped, errors
    """
    stats  = {"inserted": 0, "skipped": 0, "errors": 0}
    verb   = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
    sql    = f"""
        {verb} INTO jira_defects
            (defect_id, project, summary, description,
             reporter_name, reporter_email, status, priority, issue_type,
             labels, components, created, raw_json)
        VALUES
            (:defect_id, :project, :summary, :description,
             :reporter_name, :reporter_email, :status, :priority, :issue_type,
             :labels, :components, :created, :raw_json)
    """
    cursor = conn.cursor()
    for d in defects:
        try:
            cursor.execute(sql, d)
            if cursor.rowcount > 0:
                stats["inserted"] += 1
            else:
                stats["skipped"] += 1
        except sqlite3.Error as exc:
            print(f"  ✗ DB error inserting {d.get('defect_id', '?')}: {exc}",
                  file=sys.stderr)
            stats["errors"] += 1
    conn.commit()
    return stats


# ── Embedding cache — compute once, store in DB ───────────────────────────────

def _build_defect_embed_text(summary: str, description: Optional[str]) -> str:
    """
    Construct the text to embed for a Jira defect.

    We use summary + first 512 chars of description.  The description is
    truncated rather than fully included because:
    (a) BGE-small has a 512-token context window;
    (b) the failure signature / reproduction steps at the top of the
        description are the most semantically relevant section.
    """
    desc_prefix = (description or "")[:512].strip()
    return f"{summary}\n\n{desc_prefix}".strip() if desc_prefix else summary


def _build_result_embed_text(failure_msg: Optional[str], failure_kw: Optional[str], test_name: str) -> str:
    """
    Construct the text to embed for a test result failure.

    Concatenates the failure message with the failing keyword and test name
    so the embedding captures the full failure context.
    """
    parts = [test_name]
    if failure_kw:
        parts.append(f"keyword: {failure_kw}")
    if failure_msg:
        parts.append(failure_msg)
    return " | ".join(parts)


def compute_and_cache_embeddings(
    conn:       sqlite3.Connection,
    model_name: str = DEFAULT_EMBED_MODEL,
    *,
    batch_size: int  = 64,
    force:      bool = False,
) -> dict:
    """
    Compute embeddings for all un-embedded defects and FAIL test results
    and store them in the `embeddings` table.

    Embeddings are stored as little-endian float32 BLOBs alongside the
    source text so we can detect if the text changed and re-embed.

    Parameters
    ----------
    conn       : open sqlite3 connection
    model_name : HuggingFace model ID (default: BAAI/bge-small-en-v1.5)
    batch_size : number of texts to embed per model.encode() call
    force      : recompute even if an embedding already exists

    Returns
    -------
    dict — keys: defects_embedded, results_embedded, skipped, errors, model_available
    """
    stats = {
        "defects_embedded":  0,
        "results_embedded":  0,
        "skipped":           0,
        "errors":            0,
        "model_available":   False,
    }

    model = _get_embed_model(model_name)
    if model is None:
        return stats  # graceful degradation — caller checks model_available
    stats["model_available"] = True

    import numpy as np

    def _embed_batch(texts: list[str], entity_ids: list[str], entity_type: str) -> int:
        """Embed a list of texts and write them to the embeddings table."""
        if not texts:
            return 0
        try:
            vecs = model.encode(texts)  # shape (N, dim), already float32
        except Exception as exc:
            print(f"  ✗ Embedding encode error [{entity_type}]: {exc}", file=sys.stderr)
            stats["errors"] += len(texts)
            return 0

        rows_written = 0
        cursor = conn.cursor()
        for eid, text, vec in zip(entity_ids, texts, vecs):
            blob = _vec_to_blob(vec)
            verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
            cursor.execute(
                f"""
                {verb} INTO embeddings
                    (entity_id, entity_type, model_name, vector, source_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (eid, entity_type, model_name, blob, text),
            )
            if cursor.rowcount > 0:
                rows_written += 1
            else:
                stats["skipped"] += 1
        conn.commit()
        return rows_written

    # ── 1. Embed Jira defects ─────────────────────────────────────────────────
    defect_rows = conn.execute(
        "SELECT defect_id, summary, description FROM jira_defects"
    ).fetchall()

    existing_defect_ids: set[str] = set()
    if not force:
        existing_defect_ids = {
            r[0] for r in conn.execute(
                "SELECT entity_id FROM embeddings WHERE entity_type = 'jira_defect' AND model_name = ?",
                (model_name,),
            ).fetchall()
        }

    defect_texts, defect_ids = [], []
    for row in defect_rows:
        if row["defect_id"] in existing_defect_ids:
            stats["skipped"] += 1
            continue
        text = _build_defect_embed_text(row["summary"], row["description"])
        defect_texts.append(text)
        defect_ids.append(row["defect_id"])

    for i in range(0, len(defect_texts), batch_size):
        n = _embed_batch(
            defect_texts[i:i+batch_size],
            defect_ids[i:i+batch_size],
            "jira_defect",
        )
        stats["defects_embedded"] += n

    # ── 2. Embed FAIL test results ────────────────────────────────────────────
    result_rows = conn.execute(
        "SELECT result_id, test_name, failure_msg, failure_kw FROM test_results WHERE status = 'FAIL'"
    ).fetchall()

    existing_result_ids: set[str] = set()
    if not force:
        existing_result_ids = {
            r[0] for r in conn.execute(
                "SELECT entity_id FROM embeddings WHERE entity_type = 'test_result' AND model_name = ?",
                (model_name,),
            ).fetchall()
        }

    result_texts, result_ids = [], []
    for row in result_rows:
        if row["result_id"] in existing_result_ids:
            stats["skipped"] += 1
            continue
        text = _build_result_embed_text(row["failure_msg"], row["failure_kw"], row["test_name"])
        result_texts.append(text)
        result_ids.append(row["result_id"])

    for i in range(0, len(result_texts), batch_size):
        n = _embed_batch(
            result_texts[i:i+batch_size],
            result_ids[i:i+batch_size],
            "test_result",
        )
        stats["results_embedded"] += n

    return stats


def _iter_embedding_vectors(
    conn:        sqlite3.Connection,
    entity_type: str,
    model_name:  str,
    chunk_size:  int = 512,
):
    """
    Iterate over cached embedding vectors in chunks, never loading the
    full corpus into memory at once.

    Each chunk is a list of (entity_id, ndarray) tuples.  The caller
    processes one chunk, discards it, then requests the next.

    Memory profile
    ──────────────
    chunk_size = 512, embedding dim = 384 (bge-small-en-v1.5):
        512 × 384 × 4 bytes = 786 KB per chunk in RAM regardless of
        total corpus size.  Scales to hundreds of thousands of entities.

    Yields
    ──────
    list[tuple[str, numpy.ndarray]]  — one chunk at a time
    """
    import numpy as np

    # SQLite cursor — iterate rather than fetchall() to avoid materialising
    # the entire result set in Python memory.
    cursor = conn.execute(
        """
        SELECT entity_id, vector
        FROM   embeddings
        WHERE  entity_type = ?
          AND  model_name  = ?
        ORDER  BY entity_id
        """,
        (entity_type, model_name),
    )

    chunk: list[tuple[str, object]] = []
    for row in cursor:
        chunk.append((row["entity_id"], np.frombuffer(row["vector"], dtype=np.float32)))
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _load_embedding_vectors(
    conn:        sqlite3.Connection,
    entity_type: str,
    model_name:  str,
) -> dict[str, object]:
    """
    Load cached embedding vectors into a dict for random-access lookup.

    Use this only when the corpus is small enough to fit comfortably in RAM
    (rule of thumb: < 5,000 entities).  For larger corpora use
    _iter_embedding_vectors() and compute cosine similarities in chunks.

    Returns
    -------
    dict mapping entity_id → 1-D numpy float32 ndarray
    """
    result: dict[str, object] = {}
    for chunk in _iter_embedding_vectors(conn, entity_type, model_name):
        for eid, vec in chunk:
            result[eid] = vec
    return result


def _cosine_matrix(query_vecs, key_ids: list[str], key_vecs) -> dict[str, float]:
    """
    Compute cosine similarity between a matrix of query vectors and a
    matrix of key vectors using batched numpy operations.

    Both inputs must be 2-D numpy float32 arrays with matching embedding
    dimension.  Returns a flat dict mapping key_id → max similarity across
    all query vectors (for the many-queries-to-one-key case).

    Parameters
    ----------
    query_vecs : numpy.ndarray, shape (Q, dim)  — query embeddings (L2-normed)
    key_ids    : list[str], length K             — IDs for the key rows
    key_vecs   : numpy.ndarray, shape (K, dim)  — key embeddings (L2-normed)

    Returns
    -------
    dict[key_id, float]  — cosine similarity in [−1, 1], clamped to ≥ 0
    """
    import numpy as np

    # L2-normalise in place — bge-small outputs are already unit vectors but
    # normalise defensively in case of numerical drift after BLOB round-trip.
    q_norms = np.linalg.norm(query_vecs, axis=1, keepdims=True)
    k_norms = np.linalg.norm(key_vecs,   axis=1, keepdims=True)

    q_safe = np.where(q_norms > 0, query_vecs / q_norms, 0.0)
    k_safe = np.where(k_norms > 0, key_vecs   / k_norms, 0.0)

    # Dot product: shape (Q, K)
    sims = q_safe @ k_safe.T

    # For each key column, take the max similarity across all queries.
    # This naturally handles the multi-query (multiple test failures) case.
    max_sims = sims.max(axis=0) if sims.ndim == 2 else sims

    return {
        kid: max(0.0, float(sim))
        for kid, sim in zip(key_ids, max_sims)
    }


# ── Lexical (rule-based) scoring sub-component ───────────────────────────────

def _tokenise(text: str) -> set[str]:
    """
    Lowercase, split on non-alphanumeric boundaries, remove stopwords.
    Returns a set of tokens with ≥ 2 characters.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS}


def _rule_score(
    test_name:   str,
    failure_msg: Optional[str],
    failure_kw:  Optional[str],
    summary:     str,
    description: Optional[str],
) -> tuple[float, list[str]]:
    """
    Compute the lexical rule-based sub-score for a (defect, test_result) pair.

    Scoring rubric
    ──────────────
    +0.60  exact TC_* test name found verbatim in defect summary or description
    +0.25  test-name stem words found in defect text (fires only when exact
           name is NOT found, avoiding double-crediting the same evidence)
    +0.15  ≥1 shared diagnostic token between failure_msg/kw and defect text

    Returns
    -------
    (sub_score, reasons)  — sub_score in [0.0, 1.0], reasons is a list of
    human-readable strings that fired.
    """
    score   = 0.0
    reasons: list[str] = []

    combined       = f"{summary} {description or ''}".strip()
    combined_lower = combined.lower()

    # Signal 1 — verbatim test name
    if test_name.lower() in combined_lower:
        score += 0.60
        reasons.append(f"exact test name '{test_name}' in defect text")

    # Signal 2 — stem word match (only when exact name did not fire)
    if score < 0.60:
        stem_parts  = [p for p in re.split(r"_+", test_name.lower()) if p not in ("tc",)]
        stem_tokens = _tokenise(" ".join(stem_parts))
        defect_toks = _tokenise(combined)
        overlap     = stem_tokens & defect_toks
        if overlap:
            score += 0.25
            reasons.append(f"stem word overlap {sorted(overlap)}")

    # Signal 3 — failure text keyword overlap
    if failure_msg or failure_kw:
        raw_fail  = f"{failure_msg or ''} {failure_kw or ''}".strip()
        fail_toks = _tokenise(raw_fail)
        defect_toks = _tokenise(combined)
        kw_overlap  = fail_toks & defect_toks
        if kw_overlap:
            score += 0.15
            reasons.append(f"failure keyword overlap {sorted(kw_overlap)}")

    return min(round(score, 4), 1.0), reasons


# ── Hybrid scoring — rule + semantic ─────────────────────────────────────────

def _mapping_is_confirmed(
    *,
    score: float,
    reporter_email: Optional[str],
    tester_email: Optional[str],
    email_match: int,
    date_within_window: int,
) -> bool:
    """Return True when a mapping should be treated as confirmed.

    Missing reporter emails are treated as neutral rather than blocking the
    mapping, which is helpful for imported demo defects that do not carry a
    reliable reporter identity.
    """
    email_gate_passed = (
        email_match == 1
        or not reporter_email
        or not tester_email
    )
    return bool(score >= 0.5 and email_gate_passed and date_within_window)


def _hybrid_score(
    test_name:       str,
    failure_msg:     Optional[str],
    failure_kw:      Optional[str],
    summary:         str,
    description:     Optional[str],
    result_vec,               # numpy float32 array or None
    defect_vec,               # numpy float32 array or None
) -> tuple[float, str]:
    """
    Compute the final hybrid confidence score and a human-readable explanation.

    Parameters
    ----------
    test_name, failure_msg, failure_kw, summary, description:
        Fields from test_results / jira_defects tables.
    result_vec, defect_vec:
        Pre-computed embedding vectors, or None if not available.

    Returns
    -------
    (confidence_score, match_reason)
    """
    # ── Lexical component ─────────────────────────────────────────────────────
    rule_sub, rule_reasons = _rule_score(
        test_name, failure_msg, failure_kw, summary, description
    )

    # ── Semantic component ────────────────────────────────────────────────────
    semantic_sub  = 0.0
    semantic_avail = result_vec is not None and defect_vec is not None

    if semantic_avail:
        semantic_sub = max(0.0, _cosine_similarity(result_vec, defect_vec))

    # ── Blend — adjust weights when semantic is unavailable ──────────────────
    if semantic_avail:
        alpha, beta = _ALPHA_RULE, _BETA_SEMANTIC
    else:
        alpha, beta = 1.0, 0.0   # full rule-based fallback

    final_score = min(round(alpha * rule_sub + beta * semantic_sub, 4), 1.0)

    # ── Build explainability string ───────────────────────────────────────────
    reasons = list(rule_reasons)  # copy so we don't mutate
    if semantic_avail:
        sim_str = f"{semantic_sub:.3f}"
        if semantic_sub >= _SEMANTIC_REASON_THRESHOLD:
            reasons.append(f"semantic similarity {sim_str} ≥ {_SEMANTIC_REASON_THRESHOLD}")
        else:
            reasons.append(f"semantic similarity {sim_str}")
    else:
        reasons.append("semantic: model unavailable (rule-only)")

    match_reason = "; ".join(reasons) if reasons else "no significant match signals"
    return final_score, match_reason


# ── Main mapping function ─────────────────────────────────────────────────────

def map_defects_to_test_results(
    conn:            sqlite3.Connection,
    *,
    tester_email:    Optional[str] = None,
    window_days:     Optional[int] = DEFECT_WINDOW_DAYS,
    min_confidence:  float         = DEFECT_MIN_CONFIDENCE,
    overwrite:       bool          = False,
    model_name:      str           = DEFAULT_EMBED_MODEL,
    embed_batch_size: int          = 64,
    force_reembed:   bool          = False,
) -> dict:
    """
    Match every defect in jira_defects against every FAIL test result and
    write qualifying candidate pairs to defect_test_mappings.

    This function orchestrates the full hybrid pipeline:
      1. Compute / load embedding vectors for all defects and FAIL results.
      2. For each (defect, result) pair:
         a. Apply the two mandatory gate checks (email + date window).
         b. Compute the hybrid confidence score.
         c. Write rows above min_confidence to defect_test_mappings.

    Parameters
    ----------
    conn              : open sqlite3 connection (row_factory = sqlite3.Row)
    tester_email      : email to enforce reporter-match gate (optional —
                        if None, email_match is 0 but pairs are still evaluated)
    window_days       : max |days| between defect.created and run.timestamp
    min_confidence    : discard pairs below this score (default 0.25)
    overwrite         : DELETE all existing mappings before re-computing
    model_name        : HuggingFace model ID for semantic embeddings
    embed_batch_size  : batch size passed to compute_and_cache_embeddings()
    force_reembed     : recompute embeddings even if cached

    Returns
    -------
    dict — keys: candidates_evaluated, mappings_written, confirmed, errors,
                 semantic_enabled, defects_embedded, results_embedded
    """
    stats: dict = {
        "candidates_evaluated": 0,
        "mappings_written":     0,
        "confirmed":            0,
        "errors":               0,
        "semantic_enabled":     False,
        "defects_embedded":     0,
        "results_embedded":     0,
    }

    # ── Step 1: ensure embedding vectors are computed and cached ──────────────
    print("  Computing / loading embeddings …", flush=True)
    embed_stats = compute_and_cache_embeddings(
        conn,
        model_name=model_name,
        batch_size=embed_batch_size,
        force=force_reembed,
    )
    stats["semantic_enabled"]  = embed_stats["model_available"]
    stats["defects_embedded"]  = embed_stats["defects_embedded"]
    stats["results_embedded"]  = embed_stats["results_embedded"]

    if stats["semantic_enabled"]:
        print(
            f"  ✓  Embeddings ready: {embed_stats['defects_embedded']} new defect vectors, "
            f"{embed_stats['results_embedded']} new result vectors "
            f"({embed_stats['skipped']} cached)."
        )
    else:
        print("  ⚠  Semantic scoring disabled — running in rule-only mode.")

    # ── Step 2: SQL pre-filter — only load candidate pairs ───────────────────
    #
    # The date-window check is pushed entirely into SQL so Python never sees
    # pairs that cannot possibly be confirmed.  This collapses the O(D × R)
    # Cartesian product to O(candidates) where candidates ≪ D × R.
    #
    # Email-match is a Python-side check because reporter_email is on the
    # defect row, not the result row — the SQL JOIN would require a subquery
    # that scans defects twice.  We handle it cheaply in the scoring loop.
    #
    # JQL equivalent of the WHERE clause:
    #   ABS(JULIANDAY(d.created) - JULIANDAY(r.timestamp)) <= window_days
    #   AND tr.status = 'FAIL'
    #
    # Result is ordered by defect_id so the outer chunking loop processes
    # one defect at a time without repeated seeks.

    if window_days and window_days > 0:
        logger.info(
            "map_defects_to_test_results: SQL pre-filter (window=%d days, "
            "min_confidence=%.2f)", window_days, min_confidence
        )
        date_filter_sql = "AND ABS(JULIANDAY(d.created) - JULIANDAY(r.timestamp)) <= :window_days"
        params = {"window_days": window_days}
    else:
        logger.info(
            "map_defects_to_test_results: SQL pre-filter (date window disabled, "
            "min_confidence=%.2f)", min_confidence
        )
        date_filter_sql = ""
        params = {}

    candidates_sql = f"""
        SELECT
            d.defect_id,
            d.project          AS defect_project,
            d.summary          AS defect_summary,
            d.description      AS defect_description,
            d.reporter_email,
            d.status           AS defect_status,
            d.priority         AS defect_priority,
            d.created          AS defect_created,
            tr.result_id,
            tr.run_id,
            tr.test_name,
            tr.failure_msg,
            tr.failure_kw,
            r.timestamp        AS run_timestamp,
            ABS(
                JULIANDAY(d.created) - JULIANDAY(r.timestamp)
            )                  AS date_diff_days
        FROM   jira_defects d
        CROSS  JOIN test_results tr
        JOIN   runs r ON tr.run_id = r.run_id
        WHERE  tr.status = 'FAIL'
        {date_filter_sql}
        ORDER  BY d.defect_id, tr.result_id
    """

    # Use a dedicated cursor so we can stream rows without materialising the
    # full result set (important when candidate count is large).
    cand_cursor = conn.execute(candidates_sql, params)

    if not cand_cursor:
        logger.warning("map_defects_to_test_results: no candidate pairs after SQL pre-filter")
        return stats

    # ── Step 3: load embedding vectors (chunked, memory-safe) ─────────────────
    #
    # Strategy depends on corpus size:
    #   ≤ SMALL_CORPUS_THRESHOLD entities → load all into a dict (fast random access)
    #   >  threshold                      → build an in-memory numpy matrix and
    #                                        use batched matrix cosine for O(1) per pair
    #
    # We always load defect vectors (typically small, < 200) into a dict.
    # Result vectors are loaded into a dict when small; for large corpora
    # _cosine_matrix() is used with the defect matrix as the query side.

    SMALL_CORPUS_THRESHOLD = 5_000

    defect_vecs:  dict[str, object] = {}
    result_vecs:  dict[str, object] = {}
    use_matrix_cosine = False

    if stats["semantic_enabled"]:
        logger.info("Loading embedding vectors …")

        # Defect vectors — always dict (corpora are small)
        defect_vecs = _load_embedding_vectors(conn, "jira_defect", model_name)

        # Result vectors — dict or matrix depending on size
        n_results = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE entity_type='test_result' AND model_name=?",
            (model_name,),
        ).fetchone()[0]

        if n_results <= SMALL_CORPUS_THRESHOLD:
            result_vecs = _load_embedding_vectors(conn, "test_result", model_name)
            logger.info(
                "Loaded %d defect vectors, %d result vectors (dict mode)",
                len(defect_vecs), len(result_vecs),
            )
        else:
            # Build (entity_id→row_index) + numpy matrix for batched cosine
            import numpy as np
            result_id_index: dict[str, int] = {}
            matrix_rows: list = []
            for chunk in _iter_embedding_vectors(conn, "test_result", model_name):
                for eid, vec in chunk:
                    result_id_index[eid] = len(matrix_rows)
                    matrix_rows.append(vec)
            result_matrix = np.stack(matrix_rows).astype(np.float32)
            result_id_list = list(result_id_index.keys())  # index → entity_id
            use_matrix_cosine = True
            logger.info(
                "Loaded %d defect vectors, %d result vectors (matrix mode, shape %s)",
                len(defect_vecs), len(result_id_index), result_matrix.shape,
            )

    # ── Step 4: optionally wipe existing mappings ─────────────────────────────
    if overwrite:
        conn.execute("DELETE FROM defect_test_mappings")
        conn.commit()
        logger.info("Existing mappings cleared (overwrite mode).")
        print("  ✓  Existing mappings cleared (overwrite mode).")

    # ── Step 5: normalise tester_email once ───────────────────────────────────
    norm_tester_email: str = _normalise_email(tester_email) if tester_email else ""

    # ── Step 6: score every SQL-filtered candidate pair ───────────────────────
    insert_sql = """
        INSERT OR IGNORE INTO defect_test_mappings (
            mapping_id, defect_id, result_id, run_id, test_name,
            defect_project, defect_summary, defect_status, defect_priority,
            reporter_email, run_timestamp, defect_created, date_diff_days,
            confidence_score, match_reason, email_match, date_within_window, confirmed
        ) VALUES (
            :mapping_id, :defect_id, :result_id, :run_id, :test_name,
            :defect_project, :defect_summary, :defect_status, :defect_priority,
            :reporter_email, :run_timestamp, :defect_created, :date_diff_days,
            :confidence_score, :match_reason, :email_match, :date_within_window, :confirmed
        )
    """

    write_cursor = conn.cursor()
    BATCH_SIZE   = 500
    batch: list[dict] = []

    for row in cand_cursor:
        stats["candidates_evaluated"] += 1

        # ── Gate (a): email match ─────────────────────────────────────────────
        reporter_email = row["reporter_email"] or ""
        if norm_tester_email and reporter_email:
            email_match = 1 if reporter_email == norm_tester_email else 0
        else:
            email_match = 1 if not norm_tester_email else 0

        # date_diff_days already computed in SQL — no Python datetime parsing
        date_diff          = float(row["date_diff_days"])
        date_within_window = 1 if (not window_days or window_days <= 0) else 1

        # ── Resolve embedding vectors ─────────────────────────────────────────
        d_vec = defect_vecs.get(row["defect_id"]) if stats["semantic_enabled"] else None

        if stats["semantic_enabled"]:
            if use_matrix_cosine:
                # result_matrix available — look up by index
                idx   = result_id_index.get(row["result_id"])
                r_vec = result_matrix[idx] if idx is not None else None
            else:
                r_vec = result_vecs.get(row["result_id"])
        else:
            r_vec = None

        # ── Hybrid scoring ────────────────────────────────────────────────────
        score, reason = _hybrid_score(
            test_name   = row["test_name"],
            failure_msg = row["failure_msg"],
            failure_kw  = row["failure_kw"],
            summary     = row["defect_summary"],
            description = row["defect_description"],
            result_vec  = r_vec,
            defect_vec  = d_vec,
        )

        if score < min_confidence:
            continue

        confirmed = 1 if _mapping_is_confirmed(
            score=score,
            reporter_email=reporter_email,
            tester_email=tester_email,
            email_match=email_match,
            date_within_window=date_within_window,
        ) else 0
        mapping_id = f"{row['defect_id']}__{row['result_id']}"

        batch.append({
            "mapping_id":         mapping_id,
            "defect_id":          row["defect_id"],
            "result_id":          row["result_id"],
            "run_id":             row["run_id"],
            "test_name":          row["test_name"],
            "defect_project":     row["defect_project"],
            "defect_summary":     row["defect_summary"],
            "defect_status":      row["defect_status"],
            "defect_priority":    row["defect_priority"],
            "reporter_email":     row["reporter_email"],
            "run_timestamp":      row["run_timestamp"],
            "defect_created":     row["defect_created"],
            "date_diff_days":     round(date_diff, 4),
            "confidence_score":   score,
            "match_reason":       reason,
            "email_match":        email_match,
            "date_within_window": date_within_window,
            "confirmed":          confirmed,
        })

        if len(batch) >= BATCH_SIZE:
            try:
                write_cursor.executemany(insert_sql, batch)
                conn.commit()
                stats["mappings_written"] += write_cursor.rowcount
                stats["confirmed"]        += sum(r["confirmed"] for r in batch)
            except sqlite3.Error as exc:
                logger.error("Batch insert error: %s", exc)
                print(f"  ✗ Batch insert error: {exc}", file=sys.stderr)
                stats["errors"] += 1
            finally:
                batch.clear()

    # ── Flush final batch ─────────────────────────────────────────────────────
    if batch:
        try:
            write_cursor.executemany(insert_sql, batch)
            conn.commit()
            stats["mappings_written"] += write_cursor.rowcount
            stats["confirmed"]        += sum(r["confirmed"] for r in batch)
        except sqlite3.Error as exc:
            logger.error("Final batch insert error: %s", exc)
            print(f"  ✗ Final batch insert error: {exc}", file=sys.stderr)
            stats["errors"] += 1

    return stats


# ── Dashboard data helpers (called from dashboard.py) ────────────────────────

def load_defect_mappings(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Return the full defect_test_mappings table joined with jira_defects as a
    DataFrame.  Used by the dashboard Defect Mapping tab.
    """
    return pd.read_sql_query(
        """
        SELECT
            m.mapping_id,
            m.defect_id,
            m.defect_project,
            m.defect_summary,
            m.defect_status,
            m.defect_priority,
            m.reporter_email,
            m.test_name,
            m.run_id,
            m.run_timestamp,
            m.defect_created,
            m.date_diff_days,
            m.confidence_score,
            m.match_reason,
            m.email_match,
            m.date_within_window,
            m.confirmed,
            d.labels         AS defect_labels,
            d.components     AS defect_components,
            d.description    AS defect_description
        FROM  defect_test_mappings m
        JOIN  jira_defects d ON m.defect_id = d.defect_id
        ORDER BY m.confidence_score DESC, m.date_diff_days ASC
        """,
        conn,
    )


def load_jira_defects_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """Return the jira_defects table as a DataFrame for the dashboard."""
    return pd.read_sql_query(
        """
        SELECT defect_id, project, summary, reporter_email,
               status, priority, issue_type, labels, components,
               created, ingested_at
        FROM   jira_defects
        ORDER  BY created DESC
        """,
        conn,
    )


def get_defect_coverage_stats(conn: sqlite3.Connection) -> dict:
    """
    Compute high-level defect coverage metrics for the dashboard KPI cards.

    Returns
    -------
    dict with keys:
        total_defects, confirmed_mappings, unique_tests_covered,
        unique_runs_covered, avg_confidence, coverage_pct
    """
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM jira_defects)                    AS total_defects,
            COUNT(*)                                               AS confirmed_mappings,
            COUNT(DISTINCT test_name)                              AS unique_tests_covered,
            COUNT(DISTINCT run_id)                                 AS unique_runs_covered,
            ROUND(AVG(confidence_score), 3)                        AS avg_confidence
        FROM defect_test_mappings
        WHERE confirmed = 1
        """
    ).fetchone()

    total_fails = conn.execute(
        "SELECT COUNT(*) FROM test_results WHERE status='FAIL'"
    ).fetchone()[0]

    confirmed = row["confirmed_mappings"] or 0
    coverage  = round(confirmed * 100.0 / total_fails, 1) if total_fails else 0.0

    return {
        "total_defects":         row["total_defects"] or 0,
        "confirmed_mappings":    confirmed,
        "unique_tests_covered":  row["unique_tests_covered"] or 0,
        "unique_runs_covered":   row["unique_runs_covered"] or 0,
        "avg_confidence":        row["avg_confidence"] or 0.0,
        "coverage_pct":          coverage,
    }


def create_database(db_path: str, schema_path: str) -> sqlite3.Connection:
    """
    Open (or create) analytics.db, apply schema.sql, and configure the
    connection for safe concurrent access between the pipeline and the
    Streamlit dashboard.

    PRAGMAs applied
    ───────────────
    journal_mode = WAL
        Write-Ahead Logging allows readers (dashboard) and one writer
        (pipeline) to operate concurrently without blocking each other.
        Without WAL, SQLite uses an exclusive write lock — the dashboard
        receives "database is locked" errors during ingestion.

    busy_timeout = 10000  (10 seconds)
        If a write lock is briefly contended, SQLite retries for up to
        10 s before raising OperationalError.  Prevents spurious failures
        during short lock windows (e.g. COMMIT on a batch insert).

    foreign_keys = ON
        Enforce referential integrity between runs/test_results/defects.

    synchronous = NORMAL
        Safe with WAL — guarantees durability on OS crash; faster than
        FULL because WAL checkpoints handle the fsync cadence.

    cache_size = -32768  (-32 MB)
        Larger page cache reduces I/O for the analytics queries that scan
        large result sets (flakiness, heatmap, defect mapping).
    """
    if not os.path.exists(schema_path):
        logger.error("Schema file not found: %s", schema_path)
        print(f"✗ Schema file not found: {schema_path}")
        sys.exit(1)

    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Apply performance / concurrency PRAGMAs before schema creation so they
    # take effect for the executescript() call itself.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout  = 10000")
    conn.execute("PRAGMA foreign_keys  = ON")
    conn.execute("PRAGMA synchronous   = NORMAL")
    conn.execute("PRAGMA cache_size    = -32768")

    try:
        conn.executescript(schema_sql)
        logger.info("Database ready: %s", db_path)
        print(f"✓ Database ready: {db_path}")
    except sqlite3.Error as exc:
        logger.error("Error applying schema: %s", exc)
        print(f"✗ Error applying schema: {exc}")
        conn.close()
        sys.exit(1)

    return conn


def parse_rf_timestamp(ts_str: str) -> datetime:
    """Parse a Robot Framework timestamp string."""
    try:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S.%f")
    except ValueError:
        return datetime.strptime(ts_str, "%Y%m%d %H:%M:%S")


def calculate_duration(start_str: str, end_str: str) -> float:
    """Return elapsed seconds between two RF timestamps.  0.0 on parse error."""
    try:
        return (parse_rf_timestamp(end_str) - parse_rf_timestamp(start_str)).total_seconds()
    except Exception:
        return 0.0


def parse_run(run_folder: str) -> dict:
    """Parse one run folder and return data shaped for schema.sql insertion."""
    meta_path = os.path.join(run_folder, "ci_metadata.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)

    pass_rate = meta.get("pass_rate_pct") or meta.get("pass_rate")
    if pass_rate is None:
        raise ValueError(
            f"ci_metadata.json in {run_folder} has neither 'pass_rate_pct' "
            f"nor 'pass_rate'. Keys found: {list(meta.keys())}"
        )

    run_id = os.path.basename(run_folder)

    run_duration_s = None

    run_row = {
        "run_id":       run_id,
        "team":         meta.get("team",        "TeamAlpha"),
        "suite_name":   meta.get("suite",       "Suite_Regression_TeamAlpha"),
        "job_name":     meta.get("job_name",    None),
        "build_no":     int(meta.get("build_no", 0)),
        "timestamp":    meta.get("timestamp",   ""),
        "total":        int(meta.get("total",   0)),
        "passed":       int(meta.get("passed",  0)),
        "failed":       int(meta.get("failed",  0)),
        "pass_rate_pct": float(pass_rate),
        "environment":  meta.get("environment", "staging"),
        "executor":     meta.get("executor",    "jenkins-agent-01"),
    }

    xml_path = os.path.join(run_folder, "output.xml")
    root = ET.parse(xml_path).getroot()

    suite_el = root.find("suite")
    if suite_el is not None:
        suite_status = suite_el.find("status")
        if suite_status is not None:
            run_duration_s = calculate_duration(
                suite_status.get("starttime", ""),
                suite_status.get("endtime",   ""),
            ) or None

    run_row["duration_s"] = run_duration_s

    results = []
    suite_name_xml = suite_el.get("name", run_row["suite_name"]) if suite_el is not None else run_row["suite_name"]

    for test_el in root.findall(".//test"):
        test_name = test_el.get("name", "")

        status_el = test_el.find("status")
        if status_el is None:
            continue

        status      = status_el.get("status", "FAIL")
        start_time  = status_el.get("starttime", "")
        end_time    = status_el.get("endtime",   "")
        duration_s  = calculate_duration(start_time, end_time)

        failure_msg = None
        failure_kw  = None
        if status == "FAIL":
            raw_msg = (status_el.text or "").strip()
            if not raw_msg:
                msg_el = test_el.find(".//msg[@level='FAIL']")
                raw_msg = (msg_el.text or "").strip() if msg_el is not None else ""
            failure_msg = raw_msg or None

            for kw_el in reversed(test_el.findall(".//kw")):
                kw_status = kw_el.find("status")
                if kw_status is not None and kw_status.get("status") == "FAIL":
                    failure_kw = kw_el.get("name")
                    break

        tag_list = [
            (tag_el.text or "").strip()
            for tag_el in test_el.findall("tag")
            if (tag_el.text or "").strip()
        ]
        tags_json = json.dumps(tag_list)

        result_id = f"{run_id}_{test_name}"

        results.append({
            "result_id":   result_id,
            "run_id":      run_id,
            "suite_name":  suite_name_xml,
            "test_name":   test_name,
            "status":      status,
            "duration_s":  round(duration_s, 3),
            "failure_msg": failure_msg,
            "failure_kw":  failure_kw,
            "tags":        tags_json,
        })

    return {"run": run_row, "results": results}


def load_run_data(conn: sqlite3.Connection, run_data: dict) -> dict:
    """Insert one run's data into runs + test_results inside a single transaction."""
    cursor = conn.cursor()
    try:
        run = run_data["run"]

        cursor.execute(
            """
            INSERT INTO runs
                (run_id, team, suite_name, job_name, build_no, timestamp,
                 duration_s, total, passed, failed, pass_rate_pct,
                 environment, executor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["run_id"],
                run["team"],
                run["suite_name"],
                run["job_name"],
                run["build_no"],
                run["timestamp"],
                run["duration_s"],
                run["total"],
                run["passed"],
                run["failed"],
                run["pass_rate_pct"],
                run["environment"],
                run["executor"],
            ),
        )

        for result in run_data["results"]:
            cursor.execute(
                """
                INSERT INTO test_results
                    (result_id, run_id, suite_name, test_name, status,
                     duration_s, failure_msg, failure_kw, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["result_id"],
                    result["run_id"],
                    result["suite_name"],
                    result["test_name"],
                    result["status"],
                    result["duration_s"],
                    result["failure_msg"],
                    result["failure_kw"],
                    result["tags"],
                ),
            )

        return {"results_inserted": len(run_data["results"])}

    except sqlite3.Error as exc:
        conn.rollback()
        raise RuntimeError(
            f"DB error loading run {run_data['run']['run_id']}: {exc}"
        ) from exc


def is_already_ingested(conn: sqlite3.Connection, run_id: str) -> bool:
    """Return True if run_id already has a success row in ingestion_log."""
    row = conn.execute(
        "SELECT 1 FROM ingestion_log WHERE run_id = ? AND status = 'success'",
        (run_id,),
    ).fetchone()
    return row is not None


def log_success(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingestion_log (run_id, ingested_at, status, error_msg)
        VALUES (?, datetime('now'), 'success', NULL)
        """,
        (run_id,),
    )


def log_error(conn: sqlite3.Connection, run_id: str, error_msg: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingestion_log (run_id, ingested_at, status, error_msg)
        VALUES (?, datetime('now'), 'error', ?)
        """,
        (run_id, error_msg[:2000]),
    )
    conn.commit()


def run_pipeline(config: dict) -> dict:
    """Full ingestion pipeline."""
    runs_dir    = config["runs_dir"]
    db_path     = config["database_path"]
    schema_path = config["schema_path"]
    batch_size  = config["batch_size"]
    force       = config.get("force", False)

    print("=" * 70)
    print("  Phase 2 Ingestion Pipeline  (schema.sql-aligned)")
    print("=" * 70)
    print()

    if not os.path.exists(runs_dir):
        print(f"✗ Runs directory not found: {runs_dir}")
        sys.exit(1)

    folders = sorted(
        f for f in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, f))
        and any(f.startswith(f"{prog}_build_") for prog in ["alpha", "beta", "gamma"])
    )

    if not folders:
        print(f"✗ No program_build_* folders found in {runs_dir}")
        sys.exit(1)

    print(f"  Input directory  : {runs_dir}/")
    print(f"  Run folders      : {len(folders)}")
    print(f"  Database         : {db_path}")
    print(f"  Schema           : {schema_path}")
    print(f"  Force re-ingest  : {'yes' if force else 'no'}")
    print()

    conn = create_database(db_path, schema_path)
    print()

    stats = {
        "runs_processed":    0,
        "results_inserted":  0,
        "runs_skipped":      0,
        "errors":            0,
    }

    print(f"Processing {len(folders)} folders…")
    print()

    for i, folder in enumerate(folders, 1):
        folder_path = os.path.join(runs_dir, folder)
        run_id      = folder

        if not force and is_already_ingested(conn, run_id):
            stats["runs_skipped"] += 1
            continue

        try:
            run_data = parse_run(folder_path)
        except Exception as exc:
            msg = str(exc)
            print(f"  ✗ Parse  — {folder}: {msg[:110]}")
            log_error(conn, run_id, f"parse: {msg}")
            stats["errors"] += 1
            continue

        try:
            result = load_run_data(conn, run_data)
        except Exception as exc:
            msg = str(exc)
            print(f"  ✗ Load   — {folder}: {msg[:110]}")
            log_error(conn, run_id, f"load: {msg}")
            stats["errors"] += 1
            continue

        log_success(conn, run_id)
        stats["runs_processed"]   += 1
        stats["results_inserted"] += result["results_inserted"]

        if stats["runs_processed"] % batch_size == 0 or i == len(folders):
            conn.commit()
            print(
                f"  ✓  {stats['runs_processed']:3d}/{len(folders)} runs  "
                f"| {stats['results_inserted']:5d} test_results"
            )

    conn.commit()
    print()

    print("=" * 70)
    print("INGESTION COMPLETE")
    print("=" * 70)
    print()
    print(f"  Runs processed   : {stats['runs_processed']:4d}")
    print(f"  Runs skipped     : {stats['runs_skipped']:4d}  (already ingested)")
    print(f"  Test rows stored : {stats['results_inserted']:4d}")
    if stats["errors"]:
        print(f"  ✗ Errors        : {stats['errors']:4d}  (see above)")
    print()

    print("Verifying row counts…")
    total_expected_results = stats["runs_processed"] * 20

    cursor = conn.cursor()
    for table, expected, label in [
        ("runs",         stats["runs_processed"] + stats["runs_skipped"], "runs"),
        ("test_results", total_expected_results,                          "test_results (≈20 per run)"),
        ("ingestion_log", stats["runs_processed"] + stats["runs_skipped"], "ingestion_log"),
    ]:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        actual = cursor.fetchone()[0]
        ok = actual >= expected
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {table:<16}: {actual:5d} rows  (expected ≥{expected})  [{label}]")

    print()
    conn.close()
    return stats


#  OPTION B — MULTI-DATABASE MERGING
#  Open a separate sqlite3.connect() per database, detect schema variant,
#  query each independently, and merge into a single canonical pandas DataFrame.

def detect_schema_variant(conn: sqlite3.Connection) -> str:
    """Identify which schema variant a database uses."""
    tables = {
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "tests" in tables and "failures" in tables:
        return "schema_v1"
    return "schema_v2"


def _fetch_runs_v2(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch runs from schema_v2 (schema.sql) database."""
    return pd.read_sql_query(
        """
        SELECT
            run_id,
            team,
            suite_name,
            build_no,
            timestamp,
            COALESCE(duration_s, 0)   AS duration_s,
            total,
            passed,
            failed,
            pass_rate_pct,
            environment,
            executor
        FROM runs
        ORDER BY timestamp ASC
        """,
        conn,
    )


def _fetch_runs_v1(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch runs from schema_v1 (extended pipeline.py schema)."""
    df = pd.read_sql_query(
        """
        SELECT
            CAST(run_id AS TEXT)          AS run_id,
            'unknown'                     AS team,
            'unknown'                     AS suite_name,
            build_number                  AS build_no,
            timestamp,
            0.0                           AS duration_s,
            total_tests                   AS total,
            passed,
            failed,
            pass_rate                     AS pass_rate_pct,
            environment,
            executor
        FROM runs
        ORDER BY timestamp ASC
        """,
        conn,
    )
    return df


def _fetch_test_results_v2(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch per-test data from schema_v2 database."""
    return pd.read_sql_query(
        """
        SELECT
            tr.result_id,
            tr.run_id,
            r.team,
            r.suite_name,
            r.timestamp                   AS run_timestamp,
            r.pass_rate_pct               AS run_pass_rate,
            tr.test_name,
            tr.status,
            tr.duration_s,
            tr.failure_msg,
            tr.failure_kw,
            tr.tags
        FROM test_results tr
        JOIN runs r ON tr.run_id = r.run_id
        ORDER BY r.timestamp ASC, tr.test_name ASC
        """,
        conn,
    )


def _fetch_test_results_v1(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fetch per-test data from schema_v1 (pipeline.py extended schema)."""
    return pd.read_sql_query(
        """
        SELECT
            CAST(t.run_id AS TEXT) || '_' || t.test_name  AS result_id,
            CAST(t.run_id AS TEXT)        AS run_id,
            'unknown'                     AS team,
            'unknown'                     AS suite_name,
            r.timestamp                   AS run_timestamp,
            r.pass_rate                   AS run_pass_rate,
            t.test_name,
            t.status,
            t.duration                    AS duration_s,
            COALESCE(f.message, NULL)     AS failure_msg,
            COALESCE(f.keyword_name, NULL) AS failure_kw,
            NULL                          AS tags
        FROM tests t
        JOIN runs r         ON t.run_id  = r.run_id
        JOIN test_results tr ON t.test_id = tr.test_id
        LEFT JOIN failures f ON t.test_id = f.test_id
        ORDER BY r.timestamp ASC, t.test_name ASC
        """,
        conn,
    )


def open_connections(db_paths: list[str]) -> list[tuple[str, sqlite3.Connection, str]]:
    """Open a separate connection for each database path."""
    connections = []
    for path in db_paths:
        if not Path(path).exists():
            print(f"⚠  Database not found, skipping: {path}")
            continue
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        variant = detect_schema_variant(conn)
        print(f"  ✓  Opened {path}  [{variant}]")
        connections.append((path, conn, variant))
    return connections


def load_multi_db(db_paths: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Open each database independently, fetch data using the correct schema."""
    all_runs    = []
    all_results = []

    connections = open_connections(db_paths)

    for db_path, conn, variant in connections:
        label = Path(db_path).stem

        try:
            if variant == "schema_v2":
                df_r  = _fetch_runs_v2(conn)
                df_tr = _fetch_test_results_v2(conn)
            else:
                df_r  = _fetch_runs_v1(conn)
                df_tr = _fetch_test_results_v1(conn)

            df_r["_source_db"]  = label
            df_tr["_source_db"] = label

            all_runs.append(df_r)
            all_results.append(df_tr)
            print(f"  Loaded {label}: {len(df_r)} runs, {len(df_tr)} test results")

        except Exception as exc:
            print(f"  ✗ Error reading {db_path}: {exc}")


    if not all_runs:
        return pd.DataFrame(), pd.DataFrame()

    df_runs    = pd.concat(all_runs,    ignore_index=True)
    df_results = pd.concat(all_results, ignore_index=True)

    df_runs    = df_runs.drop_duplicates(subset=["run_id", "_source_db"])
    df_results = df_results.drop_duplicates(subset=["result_id", "_source_db"])

    df_runs["pass_rate_pct"]    = pd.to_numeric(df_runs["pass_rate_pct"],    errors="coerce")
    df_results["run_pass_rate"] = pd.to_numeric(df_results["run_pass_rate"], errors="coerce")

    return df_runs, df_results


def close_connections(connections: list[tuple[str, sqlite3.Connection, str]]) -> None:
    """Close all open connections returned by open_connections()."""
    for _, conn, _ in connections:
        try:
            conn.close()
        except Exception:
            pass


def get_runs_for_team(df_runs: pd.DataFrame, team: str) -> pd.DataFrame:
    """Filter merged runs DataFrame to a specific team."""
    return df_runs[df_runs["team"] == team].copy()


def get_flaky_scores(df_results: pd.DataFrame) -> pd.DataFrame:
    """Compute per-test flip count and failure rate from merged test results."""
    df = df_results.sort_values(["test_name", "run_timestamp"]).copy()
    df["prev_status"] = df.groupby(["test_name", "_source_db"])["status"].shift(1)

    result = (
        df.groupby(["test_name", "_source_db"])
        .agg(
            flip_count=("status",
                        lambda s: (s != s.shift(1)).sum() - 1),
            fail_count=("status",  lambda s: (s == "FAIL").sum()),
            total_runs=("status",  "count"),
        )
        .reset_index()
    )
    result["failure_rate"] = (result["fail_count"] / result["total_runs"] * 100).round(1)
    result["flip_count"]   = result["flip_count"].clip(lower=0)
    return result.sort_values("flip_count", ascending=False)


def get_heatmap_matrix(df_results: pd.DataFrame,
                       source_db: Optional[str] = None) -> pd.DataFrame:
    """Pivot test results into a matrix for the heatmap chart."""
    df = df_results.copy()
    if source_db:
        df = df[df["_source_db"] == source_db]

    df["pass_int"] = (df["status"] == "PASS").astype(int)

    run_order = (
        df[["run_id", "run_timestamp"]]
        .drop_duplicates()
        .sort_values("run_timestamp")["run_id"]
        .tolist()
    )

    pivot = df.pivot_table(
        index="test_name",
        columns="run_id",
        values="pass_int",
        aggfunc="first",
    )

    pivot = pivot.reindex(columns=[r for r in run_order if r in pivot.columns])

    pivot = pivot.sort_index()

    return pivot


def get_sankey_data(df_results: pd.DataFrame,
                    source_db: Optional[str] = None) -> dict:
    """Prepare node/link data for the Sankey failure-flow chart."""
    df = df_results.copy()
    if source_db:
        df = df[df["_source_db"] == source_db]

    failures = df[df["status"] == "FAIL"].copy()
    if failures.empty:
        return {"labels": [], "source": [], "target": [], "value": [], "colors": []}

    def classify(msg: str) -> str:
        if not msg:
            return "unknown"
        m = str(msg).lower()
        if "still visible after" in m:  return "timeout"
        if "not found after"     in m:  return "element"
        if "expected http"       in m:  return "assertion"
        if "csv export"          in m:  return "data"
        if "environment"         in m or "unreachable" in m: return "environment"
        return "unknown"

    failures["fail_cat"] = failures["failure_msg"].apply(classify)

    def run_phase(run_id: str) -> str:
        try:
            n = int(str(run_id).rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return "Phase 4"
        if n <= 25:   return "Phase 1 (1–25)"
        elif n <= 45: return "Phase 2 (26–45)"
        elif n <= 75: return "Phase 3 (46–75)"
        else:         return "Phase 4 (76–100)"

    failures["phase"] = failures["run_id"].apply(run_phase)

    cats   = sorted(failures["fail_cat"].unique())
    tests  = sorted(failures["test_name"].unique())
    phases = ["Phase 1 (1–25)", "Phase 2 (26–45)", "Phase 3 (46–75)", "Phase 4 (76–100)"]
    phases = [p for p in phases if p in failures["phase"].values]

    all_nodes = cats + tests + phases
    node_idx  = {name: i for i, name in enumerate(all_nodes)}

    ct_links = (
        failures.groupby(["fail_cat", "test_name"])
        .size()
        .reset_index(name="count")
    )
    tp_links = (
        failures.groupby(["test_name", "phase"])
        .size()
        .reset_index(name="count")
    )

    source, target, value = [], [], []

    for _, row in ct_links.iterrows():
        if row["fail_cat"] in node_idx and row["test_name"] in node_idx:
            source.append(node_idx[row["fail_cat"]])
            target.append(node_idx[row["test_name"]])
            value.append(int(row["count"]))

    for _, row in tp_links.iterrows():
        if row["test_name"] in node_idx and row["phase"] in node_idx:
            source.append(node_idx[row["test_name"]])
            target.append(node_idx[row["phase"]])
            value.append(int(row["count"]))

    CAT_COLORS  = {
        "timeout": "#D29922", "element": "#58A6FF",
        "assertion": "#BC8CFF", "data": "#FFA657",
        "environment": "#39D353", "unknown": "#8B949E",
    }
    PHASE_COLORS = {
        "Phase 1 (1–25)": "#58A6FF44", "Phase 2 (26–45)": "#D2992244",
        "Phase 3 (46–75)": "#3FB95044", "Phase 4 (76–100)": "#BC8CFF44",
    }
    TEST_COLOR  = "#1C2128"

    node_colors = []
    for name in all_nodes:
        if name in CAT_COLORS:
            node_colors.append(CAT_COLORS[name])
        elif name in PHASE_COLORS:
            node_colors.append(PHASE_COLORS[name])
        else:
            node_colors.append("#30363D")

    return {
        "labels":      all_nodes,
        "source":      source,
        "target":      target,
        "value":       value,
        "node_colors": node_colors,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Automation Test Stability Analytics — Ingestion Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "── CI run ingestion (default) ──────────────────────────────────\n"
            "  python pipeline.py\n"
            "  python pipeline.py --runs-dir ./runs2 --db ./teambravo.db\n\n"
            "── Multi-DB merge test ──────────────────────────────────────────\n"
            "  python pipeline.py --db-list ./analytics.db ./teambravo.db\n\n"
            "── File-based Jira ingestion ────────────────────────────────────\n"
            "  python pipeline.py --ingest-jira ./defects.json\n"
            "  python pipeline.py --ingest-jira ./d1.json ./d2.json --overwrite-defects\n\n"
            "── Live Jira fetch (requires .env or env vars) ──────────────────\n"
            "  python pipeline.py --fetch-jira\n"
            "  python pipeline.py --fetch-jira --since-days 14\n"
            "  python pipeline.py --fetch-jira --jira-test-connection\n\n"
            "── Defect-to-test mapping ───────────────────────────────────────\n"
            "  python pipeline.py --map-defects\n"
            "  python pipeline.py --map-defects --tester-email tester@hpe.com\n"
            "  python pipeline.py --map-defects --window-days 14 --overwrite-mappings\n\n"
            "── Write confirmed mappings back to Jira ────────────────────────\n"
            "  python pipeline.py --writeback\n"
            "  python pipeline.py --writeback --dry-run\n"
            "  python pipeline.py --writeback --dashboard-url https://your.app\n\n"
            "── Duplicate defect detection ───────────────────────────────────\n"
            "  python pipeline.py --detect-duplicates\n"
            "  python pipeline.py --detect-duplicates --dup-threshold 0.88\n\n"
            "── Full pipeline in one command ─────────────────────────────────\n"
            "  python pipeline.py --fetch-jira --map-defects --writeback\n\n"
            "── Required environment variables (or .env file) ────────────────\n"
            "  JIRA_BASE_URL    https://your-org.atlassian.net\n"
            "  JIRA_EMAIL       tester@hpe.com\n"
            "  JIRA_API_TOKEN   <Personal Access Token>\n"
            "  JIRA_PROJECTS    CSSOSE,CSSE,MCIO  (optional, default: all three)\n"
        ),
    )

    # ── CI run ingestion ──────────────────────────────────────────────────────
    p.add_argument("--runs-dir",   default=DEFAULT_CONFIG["runs_dir"],
                   help="Directory containing TeamAlpha_build_NNN folders")
    p.add_argument("--db",         default=DEFAULT_CONFIG["database_path"],
                   dest="database_path",
                   help="Path to analytics.db (created if absent)")
    p.add_argument("--schema",     default=DEFAULT_CONFIG["schema_path"],
                   dest="schema_path",
                   help="Path to schema.sql")
    p.add_argument("--batch-size", type=int, default=DEFAULT_CONFIG["batch_size"])
    p.add_argument("--force",      action="store_true", default=False,
                   help="Re-ingest already-processed run folders")
    p.add_argument("--db-list",    nargs="+", metavar="DB",
                   help="Multi-DB merge test: print merged DataFrame info and exit")

    # ── File-based Jira ingestion ─────────────────────────────────────────────
    p.add_argument(
        "--ingest-jira", nargs="+", metavar="JSON_FILE",
        help="One or more JSON files containing Jira defect records to ingest",
    )
    p.add_argument(
        "--overwrite-defects", action="store_true", default=False,
        help="INSERT OR REPLACE when ingesting Jira defects (default: INSERT OR IGNORE)",
    )

    # ── Live Jira fetch ───────────────────────────────────────────────────────
    p.add_argument(
        "--fetch-jira", action="store_true", default=False,
        help="Pull defects live from Jira REST API (requires JIRA_* env vars / .env)",
    )
    p.add_argument(
        "--since-days", type=int, default=7, metavar="N",
        help="Fetch defects created in the last N days (default: 7, used with --fetch-jira)",
    )
    p.add_argument(
        "--jira-test-connection", action="store_true", default=False,
        help="Test Jira credentials and exit (does not ingest anything)",
    )
    p.add_argument(
        "--extra-jql", default=None, metavar="JQL",
        help="Extra JQL fragment ANDed to the base query (e.g. 'priority = High')",
    )

    # ── Defect-to-test mapping ────────────────────────────────────────────────
    p.add_argument(
        "--map-defects", action="store_true", default=False,
        help="Run hybrid rule+semantic defect-to-test-result mapping",
    )
    p.add_argument(
        "--tester-email", default=None, metavar="EMAIL",
        help="Tester / reporter email for pre-condition (a) matching. "
             "Falls back to JIRA_EMAIL env var when not set.",
    )
    p.add_argument(
        "--window-days", type=int, default=DEFECT_WINDOW_DAYS,
        help=(
            f"Max calendar days between defect creation and run time "
            f"(default: {DEFECT_WINDOW_DAYS}; 0 or negative disables the date gate)"
        ),
    )
    p.add_argument(
        "--min-confidence", type=float, default=DEFECT_MIN_CONFIDENCE,
        help=f"Minimum hybrid confidence score to store a mapping (default: {DEFECT_MIN_CONFIDENCE})",
    )
    p.add_argument(
        "--overwrite-mappings", action="store_true", default=False,
        help="Delete all existing mappings before re-computing (default: incremental)",
    )

    # ── Semantic embedding controls ───────────────────────────────────────────
    p.add_argument(
        "--embed-model", default=DEFAULT_EMBED_MODEL, metavar="MODEL_ID",
        help=f"HuggingFace model ID for semantic embeddings (default: {DEFAULT_EMBED_MODEL})",
    )
    p.add_argument(
        "--embed-batch-size", type=int, default=64, metavar="N",
        help="Number of texts per model.encode() call (default: 64)",
    )
    p.add_argument(
        "--force-reembed", action="store_true", default=False,
        help="Recompute embeddings even if already cached in the DB",
    )
    p.add_argument(
        "--no-semantic", action="store_true", default=False,
        help="Disable semantic scoring — run rule-based matching only (faster)",
    )

    # ── Write-back to Jira ────────────────────────────────────────────────────
    p.add_argument(
        "--writeback", action="store_true", default=False,
        help="Post confirmed mappings as comments on Jira issues",
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Simulate write-back — log what would be posted without calling Jira",
    )
    p.add_argument(
        "--no-field-update", "--no-update-field",
        dest="no_field_update",
        action="store_true",
        default=False,
        help="Skip updating the automation custom field (comment-only write-back)",
    )
    p.add_argument(
        "--force-writeback", action="store_true", default=False,
        help="Reprocess confirmed mappings even if jira_sync_log already contains entries",
    )
    p.add_argument(
        "--dashboard-url", default=None, metavar="URL",
        help="Streamlit dashboard URL embedded in Jira comments (e.g. https://your.app)",
    )

    # ── Duplicate detection ───────────────────────────────────────────────────
    p.add_argument(
        "--detect-duplicates", action="store_true", default=False,
        help="Scan jira_defects for likely-duplicate issues and print a report",
    )
    p.add_argument(
        "--dup-threshold", type=float, default=0.90, metavar="FLOAT",
        help="Cosine similarity floor for duplicate detection (default: 0.90)",
    )

    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Configure logging for the pipeline run
    logging.basicConfig(
        level   = logging.WARNING,
        format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )

    # ── Option: connection test (exits immediately) ───────────────────────────
    if args.jira_test_connection:
        print("=" * 70)
        print("  Jira Connection Test")
        print("=" * 70)
        try:
            from jira_client import load_credentials, test_connection
            creds  = load_credentials()
            result = test_connection(creds)
            if result["ok"]:
                print(f"  ✓  Connected successfully")
                print(f"     Display name : {result['display_name']}")
                print(f"     Email        : {result['email']}")
                print(f"     Account ID   : {result['account_id']}")
                print(f"     Base URL     : {creds.base_url}")
                print(f"     Projects     : {', '.join(creds.projects)}")
            else:
                print(f"  ✗  Connection failed: {result['error']}")
                sys.exit(1)
        except EnvironmentError as exc:
            print(f"  ✗  {exc}")
            sys.exit(1)
        sys.exit(0)

    # ── Option: multi-DB merge test ───────────────────────────────────────────
    if args.db_list:
        print("=" * 70)
        print("  Multi-DB Merge Test")
        print("=" * 70)
        print()
        df_runs, df_results = load_multi_db(args.db_list)
        print()
        print(f"Merged runs    : {len(df_runs)} rows")
        print(f"Merged results : {len(df_results)} rows")
        if not df_runs.empty:
            print(f"Teams found    : {sorted(df_runs['team'].unique())}")
            print(f"DBs found      : {sorted(df_runs['_source_db'].unique())}")
            print(f"Columns        : {list(df_runs.columns)}")
        sys.exit(0)

    # ─────────────────────────────────────────────────────────────────────────
    # Determine which steps to run
    # Default (no flags): run CI ingestion only
    # ─────────────────────────────────────────────────────────────────────────
    run_ci       = not (args.ingest_jira or args.fetch_jira or args.map_defects
                        or args.writeback or args.detect_duplicates)
    total_errors = 0

    # ── Step 1: CI run ingestion ──────────────────────────────────────────────
    if run_ci or (args.runs_dir and not any([
        args.ingest_jira, args.fetch_jira, args.map_defects,
        args.writeback, args.detect_duplicates,
    ])):
        cfg = {
            "runs_dir":      args.runs_dir,
            "database_path": args.database_path,
            "schema_path":   args.schema_path,
            "batch_size":    args.batch_size,
            "force":         args.force,
        }
        result       = run_pipeline(cfg)
        total_errors += result["errors"]

    # ── Step 2a: File-based Jira defect ingestion ────────────────────────────
    if args.ingest_jira:
        print()
        print("=" * 70)
        print("  Jira Defect Ingestion  (file)")
        print("=" * 70)
        print()

        conn = create_database(args.database_path, args.schema_path)
        all_defects: list[dict] = []

        for jfile in args.ingest_jira:
            if not os.path.exists(jfile):
                print(f"  ✗ File not found: {jfile}", file=sys.stderr)
                total_errors += 1
                continue
            parsed = load_jira_defects_from_file(jfile)
            print(f"  ✓  Parsed {len(parsed)} defects from {jfile}")
            all_defects.extend(parsed)

        if all_defects:
            istats = ingest_jira_defects(conn, all_defects, overwrite=args.overwrite_defects)
            print(f"\n  Defects inserted : {istats['inserted']}")
            print(f"  Defects skipped  : {istats['skipped']}  (already present)")
            if istats["errors"]:
                print(f"  ✗ Errors         : {istats['errors']}")
                total_errors += istats["errors"]
        else:
            print("  ⚠  No valid defect records found.")

        conn.close()

    # ── Step 2b: Live Jira fetch ──────────────────────────────────────────────
    if args.fetch_jira:
        print()
        print("=" * 70)
        print("  Jira Defect Fetch  (live REST API)")
        print("=" * 70)
        print()

        try:
            from jira_client import load_credentials, fetch_defects
            creds = load_credentials()
            print(f"  Endpoint  : {creds.base_url}")
            print(f"  Reporter  : {creds.email}")
            print(f"  Projects  : {', '.join(creds.projects)}")
            print(f"  Window    : last {args.since_days} days")
            if args.extra_jql:
                print(f"  Extra JQL : {args.extra_jql}")
            print()

            conn          = create_database(args.database_path, args.schema_path)
            live_defects  = []
            fetched_count = 0

            for defect in fetch_defects(
                creds,
                since_days     = args.since_days,
                reporter_email = None,
                extra_jql      = args.extra_jql,
            ):
                fetched_count += 1
                parsed = parse_jira_defect(defect)
                if parsed:
                    live_defects.append(parsed)
                if fetched_count % 50 == 0:
                    print(f"  … fetched {fetched_count} issues so far", flush=True)

            print(f"  ✓  Fetched {fetched_count} issues from Jira REST API")

            if live_defects:
                istats = ingest_jira_defects(
                    conn, live_defects, overwrite=args.overwrite_defects
                )
                print(f"  Defects inserted : {istats['inserted']}")
                print(f"  Defects skipped  : {istats['skipped']}  (already present)")
                if istats["errors"]:
                    print(f"  ✗ Errors         : {istats['errors']}")
                    total_errors += istats["errors"]

            conn.close()

        except EnvironmentError as exc:
            print(f"  ✗  Credential error: {exc}", file=sys.stderr)
            print(
                "\n  Set the following in a .env file or as environment variables:\n"
                "    JIRA_BASE_URL=https://your-org.atlassian.net\n"
                "    JIRA_EMAIL=tester@hpe.com\n"
                "    JIRA_API_TOKEN=<token>\n",
                file=sys.stderr,
            )
            total_errors += 1

        except ImportError:
            print(
                "  ✗  jira_client.py not found — ensure it is in the same directory.\n"
                "     Install dependencies:  pip install requests python-dotenv",
                file=sys.stderr,
            )
            total_errors += 1

    # ── Step 3: Hybrid defect-to-test mapping ─────────────────────────────────
    if args.map_defects:
        print()
        print("=" * 70)
        print("  Defect → Test-Run Mapping  (Hybrid Rule + Semantic)")
        print("=" * 70)
        print()

        # Resolve tester email: CLI flag → JIRA_EMAIL env var → None
        tester_email = (
            args.tester_email
            or os.environ.get("JIRA_EMAIL")
            or None
        )
        print(f"  Tester email       : {tester_email or '(not set — email_match will be 0)'}")
        print(f"  Window (days)      : ±{args.window_days}")
        print(f"  Min confidence     : {args.min_confidence}")
        print(f"  Overwrite mode     : {'yes' if args.overwrite_mappings else 'no (incremental)'}")
        if args.no_semantic:
            print("  Semantic scoring   : DISABLED (--no-semantic)")
        else:
            print(f"  Embedding model    : {args.embed_model}")
            print(f"  Embed batch size   : {args.embed_batch_size}")
            print(f"  Force re-embed     : {'yes' if args.force_reembed else 'no'}")
        print()

        conn = create_database(args.database_path, args.schema_path)

        if args.no_semantic:
            _EMBED_MODEL_CACHE[args.embed_model] = None

        mstats = map_defects_to_test_results(
            conn,
            tester_email     = tester_email,
            window_days      = args.window_days,
            min_confidence   = args.min_confidence,
            overwrite        = args.overwrite_mappings,
            model_name       = args.embed_model,
            embed_batch_size = args.embed_batch_size,
            force_reembed    = args.force_reembed,
        )

        print()
        print(f"  Semantic enabled     : {'yes' if mstats['semantic_enabled'] else 'no (model unavailable)'}")
        print(f"  Defects embedded     : {mstats['defects_embedded']:6d}  (new vectors)")
        print(f"  Results embedded     : {mstats['results_embedded']:6d}  (new vectors)")
        print(f"  Candidates evaluated : {mstats['candidates_evaluated']:6d}")
        print(f"  Mappings written     : {mstats['mappings_written']:6d}")
        print(f"  Confirmed mappings   : {mstats['confirmed']:6d}  (email ✓ + date ✓ + score ≥ 0.5)")
        if mstats["errors"]:
            print(f"  ✗ Errors             : {mstats['errors']:6d}")
            total_errors += mstats["errors"]
        conn.close()

    # ── Step 4: Write confirmed mappings back to Jira ─────────────────────────
    if args.writeback:
        print()
        print("=" * 70)
        print("  Write-Back  → Jira" + ("  [DRY RUN]" if args.dry_run else ""))
        print("=" * 70)
        print()

        try:
            from jira_client import load_credentials, write_back_confirmed_mappings

            creds = load_credentials()
            print(f"  Endpoint      : {creds.base_url}")
            print(f"  Dry run       : {'yes — no API calls will be made' if args.dry_run else 'no'}")
            print(f"  Field update  : {'no (--no-field-update)' if args.no_field_update else 'yes'}")
            if args.dashboard_url:
                print(f"  Dashboard URL : {args.dashboard_url}")
            print()

            conn    = create_database(args.database_path, args.schema_path)
            wstats  = write_back_confirmed_mappings(
                conn,
                creds,
                dashboard_url = args.dashboard_url,
                dry_run       = args.dry_run,
                update_field  = not args.no_field_update,
                force         = args.force_writeback,
            )
            conn.close()

            print(f"  Attempted        : {wstats['attempted']:6d}")
            print(f"  Comments posted  : {wstats['comments_posted']:6d}")
            print(f"  Fields updated   : {wstats['fields_updated']:6d}")
            print(f"  Skipped          : {wstats['skipped']:6d}  (already written)")
            if wstats["errors"]:
                print(f"  ✗ Errors         : {wstats['errors']:6d}")
                total_errors += wstats["errors"]

        except EnvironmentError as exc:
            print(f"  ✗  Credential error: {exc}", file=sys.stderr)
            total_errors += 1
        except ImportError:
            print(
                "  ✗  jira_client.py not found or requests not installed.\n"
                "     pip install requests python-dotenv",
                file=sys.stderr,
            )
            total_errors += 1

    # ── Step 5: Duplicate defect detection ────────────────────────────────────
    if args.detect_duplicates:
        print()
        print("=" * 70)
        print("  Duplicate Defect Detection")
        print("=" * 70)
        print()
        print(f"  Similarity threshold : {args.dup_threshold}")
        print(f"  Embed model          : {args.embed_model}")
        print()

        try:
            from jira_client import detect_duplicate_defects

            conn       = create_database(args.database_path, args.schema_path)
            duplicates = detect_duplicate_defects(
                conn,
                model_name    = args.embed_model,
                sim_threshold = args.dup_threshold,
                window_days   = args.window_days,
            )
            conn.close()

            if not duplicates:
                print(f"  ✓  No duplicate pairs found above threshold {args.dup_threshold}.")
            else:
                print(f"  ⚠  {len(duplicates)} potential duplicate pair(s) detected:\n")
                for i, dup in enumerate(duplicates, 1):
                    print(f"  [{i}]  {dup['defect_a']}  ↔  {dup['defect_b']}")
                    print(f"       Similarity  : {dup['similarity']:.4f}")
                    print(f"       Date diff   : {dup['date_diff_days']} days")
                    print(f"       Shared tests: {', '.join(dup['shared_test_names'])}")
                    print(f"       Reason      : {dup['reason']}")
                    print()

        except ImportError:
            print("  ✗  jira_client.py not found.", file=sys.stderr)
            total_errors += 1

    sys.exit(0 if total_errors == 0 else 1)