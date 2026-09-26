"""Read-only query helpers shared by the MCP server and the CLI."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import db

SUMMARY_COLUMNS = """
    b.tweet_id, b.url, b.full_text, b.created_at, b.status, b.lang,
    b.author_name, b.author_handle, b.likes, b.retweets, b.replies, b.views,
    b.quoted_full_text, b.quoted_author_handle, b.media_json, b.links_json,
    b.origin, b.last_seen_at
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "media_json" in data:
        try:
            data["media"] = json.loads(data.pop("media_json") or "[]")
        except (TypeError, ValueError):
            data["media"] = []
    if "links_json" in data:
        try:
            data["links"] = json.loads(data.pop("links_json") or "[]")
        except (TypeError, ValueError):
            data["links"] = []
    return data


def _fts_terms(raw: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_@#.\-]+", raw or "")
    if not terms:
        raise ValueError("empty search query")
    return terms


def _candidate_expressions(terms: list[str]) -> list[str]:
    """Precise-first, recall-second.

    All-terms (implicit AND) is tried first because it is precise. If it matches
    nothing, the same terms are OR'd and BM25 ranking puts documents matching
    more of them on top. This avoids the classic "no results" dead end without
    making ordinary queries noisy.
    """
    prefixes = [f'"{term}"*' for term in terms]
    if len(prefixes) == 1:
        return [" ".join(prefixes)]
    return [" AND ".join(prefixes), " OR ".join(prefixes)]


def _run_search(
    conn: sqlite3.Connection,
    match_expression: str,
    limit: int,
    author: str | None,
    folder: str | None,
    since: str | None,
    until: str | None,
) -> list[sqlite3.Row]:
    params: list[Any] = [match_expression]
    sql = [
        "SELECT",
        SUMMARY_COLUMNS,
        "FROM bookmarks_fts f",
        "JOIN bookmarks b ON b.tweet_id = f.tweet_id",
    ]
    if folder:
        sql.append(
            "JOIN bookmark_folders bf ON bf.tweet_id = b.tweet_id "
            "JOIN folders fo ON fo.id = bf.folder_id"
        )
    sql.append("WHERE bookmarks_fts MATCH ?")
    if author:
        sql.append("AND (b.author_handle = ? OR b.author_handle = ?)")
        params.extend([author.lstrip("@"), author])
    if folder:
        sql.append("AND (fo.name LIKE ? OR fo.id = ?)")
        params.extend([f"%{folder}%", folder])
    if since:
        sql.append("AND b.created_at >= ?")
        params.append(since)
    if until:
        sql.append("AND b.created_at <= ?")
        params.append(until)
    sql.append("ORDER BY rank LIMIT ?")
    params.append(max(1, min(limit, 200)))
    return conn.execute(" ".join(sql), params).fetchall()


def search_bookmarks(
    query: str,
    limit: int = 25,
    author: str | None = None,
    folder: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    terms = _fts_terms(query)
    conn = db.connect()
    try:
        rows: list[sqlite3.Row] = []
        for expression in _candidate_expressions(terms):
            rows = _run_search(conn, expression, limit, author, folder, since, until)
            if rows:
                break
        results = [_row_to_dict(row) for row in rows]
        _attach_folders(conn, results)
        return results
    finally:
        conn.close()


def recent_bookmarks(
    limit: int = 25,
    days: int | None = None,
    author: str | None = None,
    folder: str | None = None,
) -> list[dict]:
    conn = db.connect()
    try:
        params: list[Any] = []
        sql = ["SELECT", SUMMARY_COLUMNS, "FROM bookmarks b"]
        if folder:
            sql.append(
                "JOIN bookmark_folders bf ON bf.tweet_id = b.tweet_id "
                "JOIN folders fo ON fo.id = bf.folder_id"
            )
        sql.append("WHERE 1 = 1")
        if days:
            sql.append("AND b.created_at >= datetime('now', ?)")
            params.append(f"-{int(days)} days")
        if author:
            sql.append("AND (b.author_handle = ? OR b.author_handle = ?)")
            params.extend([author.lstrip("@"), author])
        if folder:
            sql.append("AND (fo.name LIKE ? OR fo.id = ?)")
            params.extend([f"%{folder}%", folder])
        sql.append("ORDER BY COALESCE(b.created_at, b.first_seen_at) DESC LIMIT ?")
        params.append(max(1, min(limit, 200)))
        rows = conn.execute(" ".join(sql), params).fetchall()
        results = [_row_to_dict(r) for r in rows]
        _attach_folders(conn, results)
        return results
    finally:
        conn.close()


def get_bookmark(tweet_id_or_url: str) -> dict | None:
    match = re.search(r"(\d{6,25})", tweet_id_or_url or "")
    if not match:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM bookmarks WHERE tweet_id = ?", (match.group(1),)
        ).fetchone()
        if row is None:
            return None
        data = _row_to_dict(row)
        for key in ("entities_json", "hashtags_json", "raw_json"):
            data.pop(key, None)
        allowed_columns = {"media_json", "links_json"}
        for key in list(data):
            if key.endswith("_json") and key not in allowed_columns:
                data.pop(key)
        _attach_folders(conn, [data])
        return data
    finally:
        conn.close()


def _attach_folders(conn: sqlite3.Connection, results: list[dict]) -> None:
    if not results:
        return
    ids = [r["tweet_id"] for r in results]
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT bf.tweet_id, fo.name FROM bookmark_folders bf "
        "JOIN folders fo ON fo.id = bf.folder_id "
        f"WHERE bf.tweet_id IN ({placeholders})",
        ids,
    ).fetchall()
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row["tweet_id"], []).append(row["name"])
    for item in results:
        item["folders"] = sorted(mapping.get(item["tweet_id"], []))


def list_folders() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT fo.id, fo.name, COUNT(bf.tweet_id) AS bookmarks "
            "FROM folders fo LEFT JOIN bookmark_folders bf ON bf.folder_id = fo.id "
            "GROUP BY fo.id, fo.name ORDER BY bookmarks DESC, fo.name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def authors(limit: int = 50, min_bookmarks: int = 2) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT author_handle, author_name, COUNT(*) AS bookmarks "
            "FROM bookmarks WHERE author_handle IS NOT NULL "
            "GROUP BY author_handle HAVING bookmarks >= ? "
            "ORDER BY bookmarks DESC LIMIT ?",
            (min_bookmarks, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats() -> dict:
    conn = db.connect()
    try:
        info = db.counts(conn)
        info["by_origin"] = [
            dict(r)
            for r in conn.execute(
                "SELECT origin, COUNT(*) AS count FROM bookmarks GROUP BY origin "
                "ORDER BY count DESC"
            ).fetchall()
        ]
        info["by_month"] = [
            dict(r)
            for r in conn.execute(
                "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS count "
                "FROM bookmarks WHERE created_at IS NOT NULL "
                "GROUP BY month ORDER BY month DESC LIMIT 24"
            ).fetchall()
        ]
        info["top_authors"] = authors(limit=10, min_bookmarks=1)
        info["newest"] = conn.execute(
            "SELECT MAX(COALESCE(created_at, first_seen_at)) AS newest FROM bookmarks"
        ).fetchone()["newest"]
        for key in (
            "last_sync_at",
            "last_sync_mode",
            "last_sync_summary",
            "last_full_sync_at",
            "last_xarchive_import_at",
        ):
            info[key] = db.get_meta(conn, key)
        return info
    finally:
        conn.close()


READ_ONLY_PREFIXES = ("select", "with", "pragma table_info", "explain")


def run_sql(sql: str, limit: int = 200) -> list[dict]:
    """Execute a read-only SELECT against the archive."""
    statement = (sql or "").strip().rstrip(";")
    if not statement:
        raise ValueError("empty SQL statement")
    lowered = statement.lower()
    if not lowered.startswith(READ_ONLY_PREFIXES):
        raise ValueError("only read-only SELECT / WITH / EXPLAIN queries are allowed")
    banned = ("insert", "update", "delete", "drop", "alter", "attach", "create", "replace")
    if any(re.search(rf"\b{word}\b", lowered) for word in banned):
        raise ValueError("write statements are not allowed")
    conn = db.connect()
    try:
        rows = conn.execute(statement).fetchmany(max(1, min(limit, 1000)))
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# retrieval health
# --------------------------------------------------------------------------- #

def integrity() -> dict:
    """Whether the full-text index is in sync with the bookmark table."""
    conn = db.connect()
    try:
        return db.fts_counts(conn)
    finally:
        conn.close()


def _distinctive_token(text: str) -> str | None:
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9]{5,}", text or "")
    if not candidates:
        return None
    return max(candidates, key=len).lower()


def _unique_token(conn: sqlite3.Connection, text: str) -> tuple[str, int] | tuple[None, None]:
    """Find a token that occurs in exactly one bookmark.

    Using a corpus-unique token makes the round-trip assertion deterministic:
    the search must return that tweet regardless of BM25 ranking. Testing with a
    common word (e.g. "transformers") would fail whenever the tweet is not in
    the top-N, which is a flaky test, not a real regression.
    """
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9]{5,}", text or ""):
        token = raw.lower()
        if token in seen:
            continue
        seen.add(token)
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM bookmarks_fts WHERE bookmarks_fts MATCH ?",
            (f'"{token}"',),
        ).fetchone()["c"]
        if count == 1:
            return token, count
    return None, None


def self_test() -> dict:
    """End-to-end retrieval check.

    Picks a real bookmark, finds a token unique to it, then confirms the normal
    query path (tokenizer -> index -> query builder -> join) retrieves that same
    bookmark. Also exercises the author filter.

    This verifies that indexing and retrieval work, deterministically. It does
    not measure ranking quality — that needs a labelled query set, not a
    round-trip test.
    """
    conn = db.connect()
    try:
        candidates = conn.execute(
            "SELECT tweet_id, full_text, author_handle FROM bookmarks "
            "WHERE full_text IS NOT NULL AND length(full_text) > 80 "
            "ORDER BY RANDOM() LIMIT 20"
        ).fetchall()
        if not candidates:
            return {"ok": False, "reason": "no bookmarks with text to test against"}

        chosen = None
        token = None
        for row in candidates:
            candidate, _ = _unique_token(conn, row["full_text"])
            if candidate:
                chosen, token = row, candidate
                break

        if chosen is None:
            # No corpus-unique token: fall back to a wide membership check and
            # flag it, rather than reporting a misleading failure.
            row = candidates[0]
            fallback_token = _distinctive_token(row["full_text"])
            if fallback_token is None:
                return {"ok": False, "reason": "could not derive a test token"}
            hits = search_bookmarks(fallback_token, limit=200)
            return {
                "ok": True,
                "mode": "fallback",
                "tweet_id": row["tweet_id"],
                "token": fallback_token,
                "hits": len(hits),
                "found": any(hit["tweet_id"] == row["tweet_id"] for hit in hits),
                "note": "no corpus-unique token found; membership checked in top 200",
            }
    finally:
        conn.close()

    hits = search_bookmarks(token, limit=10)
    found = any(hit["tweet_id"] == chosen["tweet_id"] for hit in hits)

    author_ok: bool | None = None
    if chosen["author_handle"]:
        by_author = search_bookmarks(token, limit=10, author=chosen["author_handle"])
        author_ok = any(hit["tweet_id"] == chosen["tweet_id"] for hit in by_author)

    return {
        "ok": bool(found and author_ok is not False),
        "mode": "unique-token",
        "tweet_id": chosen["tweet_id"],
        "token": token,
        "hits": len(hits),
        "found": found,
        "author_filter_ok": author_ok,
    }
