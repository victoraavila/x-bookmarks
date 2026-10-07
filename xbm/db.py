"""SQLite storage for the bookmark archive.

The archive is cumulative: a newer observation may mark a bookmark unavailable
or stop returning it entirely, but it must never destroy content we already
captured. ``upsert_bookmark`` therefore fills missing fields from the incoming
record and leaves existing content alone when the incoming value is empty.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fields that carry tweet content. Empty incoming values never clobber them.
CONTENT_FIELDS = (
    "url",
    "full_text",
    "lang",
    "created_at",
    "source",
    "source_label",
    "conversation_id",
    "in_reply_to_tweet_id",
    "in_reply_to_screen_name",
    "author_id",
    "author_name",
    "author_handle",
    "author_followers",
    "author_profile_image",
    "likes",
    "retweets",
    "replies",
    "quotes",
    "bookmarks",
    "views",
    "quoted_tweet_id",
    "quoted_full_text",
    "quoted_author_handle",
    "quoted_author_name",
    "card_type",
    "card_title",
    "card_url",
    "article_title",
    "article_summary",
    "media_json",
    "entities_json",
    "hashtags_json",
    "links_json",
    "raw_json",
    "sort_index",
)

ALL_FIELDS = (
    "tweet_id",
    *CONTENT_FIELDS,
    "status",
    "unavailable_reason",
    "origin",
    "first_seen_at",
    "last_seen_at",
    "content_updated_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS bookmarks (
    tweet_id             TEXT PRIMARY KEY,
    url                  TEXT,
    full_text            TEXT,
    lang                 TEXT,
    created_at           TEXT,
    status               TEXT DEFAULT 'available',
    unavailable_reason   TEXT,
    source               TEXT,
    source_label         TEXT,
    conversation_id      TEXT,
    in_reply_to_tweet_id TEXT,
    in_reply_to_screen_name TEXT,
    author_id            TEXT,
    author_name          TEXT,
    author_handle        TEXT,
    author_followers     INTEGER,
    author_profile_image TEXT,
    likes                INTEGER,
    retweets             INTEGER,
    replies              INTEGER,
    quotes               INTEGER,
    bookmarks            INTEGER,
    views                INTEGER,
    quoted_tweet_id      TEXT,
    quoted_full_text     TEXT,
    quoted_author_handle TEXT,
    quoted_author_name   TEXT,
    card_type            TEXT,
    card_title           TEXT,
    card_url             TEXT,
    article_title        TEXT,
    article_summary      TEXT,
    media_json           TEXT,
    entities_json        TEXT,
    hashtags_json        TEXT,
    links_json           TEXT,
    raw_json             TEXT,
    origin               TEXT,
    first_seen_at        TEXT,
    last_seen_at         TEXT,
    content_updated_at   TEXT,
    sort_index           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bookmarks_created ON bookmarks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bookmarks_author  ON bookmarks(author_handle);
CREATE INDEX IF NOT EXISTS idx_bookmarks_status  ON bookmarks(status);

CREATE TABLE IF NOT EXISTS folders (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookmark_folders (
    tweet_id  TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    PRIMARY KEY (tweet_id, folder_id)
);

CREATE INDEX IF NOT EXISTS idx_bf_folder ON bookmark_folders(folder_id);

CREATE VIRTUAL TABLE IF NOT EXISTS bookmarks_fts USING fts5(
    tweet_id UNINDEXED,
    full_text,
    author_name,
    author_handle,
    links_text,
    quoted_full_text,
    article_title,
    article_summary,
    tokenize = 'porter unicode61'
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_dir() -> Path:
    raw = os.environ.get("XB_DATA_DIR")
    path = Path(raw).expanduser() if raw else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    raw = os.environ.get("XB_DB")
    return Path(raw).expanduser() if raw else data_dir() / "bookmarks.db"


def accounts_db_path() -> Path:
    raw = os.environ.get("XB_ACCOUNTS_DB")
    return Path(raw).expanduser() if raw else data_dir() / "accounts.db"


def secure_accounts_file() -> None:
    """Best-effort 0o600 on the twscrape account store.

    The file holds the ``auth_token``/``ct0`` session cookies. twscrape creates
    it with the default umask (often world-readable), so tighten it whenever we
    touch the store, including its SQLite sidecar files.
    """
    base = accounts_db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{base}{suffix}")
        if candidate.exists():
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _links_text(links_json: str | None) -> str:
    if not links_json:
        return ""
    try:
        links = json.loads(links_json)
    except (TypeError, ValueError):
        return ""
    if isinstance(links, list):
        return " ".join(str(x) for x in links if x)
    return str(links)


def _reindex_fts(conn: sqlite3.Connection, tweet_id: str) -> None:
    conn.execute("DELETE FROM bookmarks_fts WHERE tweet_id = ?", (tweet_id,))
    row = conn.execute(
        "SELECT tweet_id, full_text, author_name, author_handle, links_json, "
        "quoted_full_text, article_title, article_summary "
        "FROM bookmarks WHERE tweet_id = ?",
        (tweet_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO bookmarks_fts"
        "(tweet_id, full_text, author_name, author_handle, links_text, "
        " quoted_full_text, article_title, article_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["tweet_id"],
            row["full_text"],
            row["author_name"],
            row["author_handle"],
            _links_text(row["links_json"]),
            row["quoted_full_text"],
            row["article_title"],
            row["article_summary"],
        ),
    )


def _earliest(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return min(present) if present else None


def _latest(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return max(present) if present else None


def upsert_bookmark(conn: sqlite3.Connection, rec: dict) -> str:
    """Insert or merge a bookmark. Returns 'added', 'updated' or 'unchanged'."""
    tweet_id = rec.get("tweet_id")
    if not tweet_id:
        raise ValueError("bookmark record is missing tweet_id")

    observed = rec.get("last_seen_at") or now_iso()
    existing = conn.execute(
        "SELECT * FROM bookmarks WHERE tweet_id = ?", (tweet_id,)
    ).fetchone()

    if existing is None:
        row = {field: rec.get(field) for field in ALL_FIELDS}
        row["tweet_id"] = tweet_id
        row["status"] = rec.get("status") or "available"
        row["origin"] = rec.get("origin") or "unknown"
        row["first_seen_at"] = rec.get("first_seen_at") or observed
        row["last_seen_at"] = observed
        row["content_updated_at"] = rec.get("content_updated_at") or (
            observed if (row.get("full_text") or row.get("media_json")) else None
        )
        columns = ", ".join(ALL_FIELDS)
        placeholders = ", ".join("?" for _ in ALL_FIELDS)
        conn.execute(
            f"INSERT INTO bookmarks ({columns}) VALUES ({placeholders})",
            [row[field] for field in ALL_FIELDS],
        )
        _reindex_fts(conn, tweet_id)
        return "added"

    merged = dict(existing)
    changed = False

    # Content: fill in what we have; never let an empty value erase saved text.
    for field in CONTENT_FIELDS:
        incoming = rec.get(field)
        if incoming is None or incoming == "" or incoming == []:
            continue
        if merged.get(field) != incoming:
            merged[field] = incoming
            changed = True

    incoming_status = rec.get("status")
    if incoming_status and merged.get("status") != incoming_status:
        merged["status"] = incoming_status
        changed = True
    if incoming_status == "unavailable":
        # A tombstone may carry a reason; it must not drop the saved text.
        reason = rec.get("unavailable_reason")
        if reason and merged.get("unavailable_reason") != reason:
            merged["unavailable_reason"] = reason
            changed = True
    elif incoming_status == "available":
        merged["unavailable_reason"] = None

    origins = {merged.get("origin"), rec.get("origin")}
    combined_origin = "+".join(sorted(o for o in origins if o))
    if merged.get("origin") != combined_origin:
        merged["origin"] = combined_origin
        changed = True

    first_seen = _earliest(merged.get("first_seen_at"), rec.get("first_seen_at")) or observed
    last_seen = _latest(merged.get("last_seen_at"), observed) or observed
    content_updated = _latest(
        merged.get("content_updated_at"),
        rec.get("content_updated_at"),
    )
    if content_updated is None and (merged.get("full_text") or merged.get("media_json")):
        content_updated = observed

    for key, value in (
        ("first_seen_at", first_seen),
        ("last_seen_at", last_seen),
        ("content_updated_at", content_updated),
    ):
        if merged.get(key) != value:
            merged[key] = value
            changed = True

    if not changed:
        return "unchanged"

    assignments = ", ".join(f"{field} = ?" for field in ALL_FIELDS if field != "tweet_id")
    conn.execute(
        f"UPDATE bookmarks SET {assignments} WHERE tweet_id = ?",
        [merged[field] for field in ALL_FIELDS if field != "tweet_id"] + [tweet_id],
    )
    _reindex_fts(conn, tweet_id)
    return "updated"


def upsert_folder(conn: sqlite3.Connection, folder_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO folders(id, name) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (folder_id, name),
    )


def set_bookmark_folders(
    conn: sqlite3.Connection, tweet_id: str, folder_ids: list[str]
) -> None:
    """Replace the membership set. Use only for a *complete* folder observation."""
    conn.execute("DELETE FROM bookmark_folders WHERE tweet_id = ?", (tweet_id,))
    add_bookmark_folders(conn, tweet_id, folder_ids)


def add_bookmark_folders(
    conn: sqlite3.Connection, tweet_id: str, folder_ids: list[str]
) -> None:
    """Add memberships without removing existing ones (partial observations)."""
    for folder_id in sorted(set(folder_ids)):
        conn.execute(
            "INSERT OR IGNORE INTO bookmark_folders(tweet_id, folder_id) VALUES(?, ?)",
            (tweet_id, folder_id),
        )


def bookmark_folder_ids(conn: sqlite3.Connection, tweet_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT folder_id FROM bookmark_folders WHERE tweet_id = ?", (tweet_id,)
    ).fetchall()
    return {row["folder_id"] for row in rows}


def counts(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS c FROM bookmarks").fetchone()["c"]
    available = conn.execute(
        "SELECT COUNT(*) AS c FROM bookmarks WHERE status = 'available'"
    ).fetchone()["c"]
    folders = conn.execute("SELECT COUNT(*) AS c FROM folders").fetchone()["c"]
    return {
        "total": total,
        "available": available,
        "unavailable": total - available,
        "folders": folders,
    }


def fts_counts(conn: sqlite3.Connection) -> dict:
    """Compare the bookmark table with its full-text index.

    A drift here is the one failure mode that silently degrades search, so it is
    checked explicitly rather than assumed.
    """
    bookmarks = conn.execute("SELECT COUNT(*) AS c FROM bookmarks").fetchone()["c"]
    indexed = conn.execute("SELECT COUNT(*) AS c FROM bookmarks_fts").fetchone()["c"]
    orphaned = conn.execute(
        "SELECT COUNT(*) AS c FROM bookmarks_fts "
        "WHERE tweet_id NOT IN (SELECT tweet_id FROM bookmarks)"
    ).fetchone()["c"]
    missing = conn.execute(
        "SELECT COUNT(*) AS c FROM bookmarks "
        "WHERE tweet_id NOT IN (SELECT tweet_id FROM bookmarks_fts)"
    ).fetchone()["c"]
    return {
        "bookmarks": bookmarks,
        "indexed": indexed,
        "orphaned": orphaned,
        "missing": missing,
        "in_sync": bookmarks == indexed and orphaned == 0 and missing == 0,
    }


def reindex(conn: sqlite3.Connection) -> int:
    """Rebuild the search index from scratch. Returns the row count indexed."""
    conn.execute("DELETE FROM bookmarks_fts")
    rows = conn.execute("SELECT tweet_id FROM bookmarks").fetchall()
    for row in rows:
        _reindex_fts(conn, row["tweet_id"])
    conn.commit()
    return len(rows)
