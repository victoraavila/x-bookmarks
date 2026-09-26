"""MCP server exposing the bookmark archive to OpenCode over stdio."""

from __future__ import annotations

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as _MCPServer

from . import db
from . import query as q
from . import sync as sync_mod

INSTRUCTIONS = """\
This server exposes a local, cumulative archive of the user's X (Twitter)
bookmarks.

How to use it well:
- Start with `archive_status` to see how many bookmarks exist and when they were
  last synced. If the archive is empty, tell the user to run `xbm login` and
  `xbm sync --mode full` before searching.
- Prefer `search_bookmarks` for topic questions ("what did I save about vector
  databases") and `recent_bookmarks` for "what's new" questions.
- Always include the `url` of each bookmark you mention so the user can open the
  original post. Do not truncate `full_text` when quoting a bookmark.
- If the user asks for something saved very recently and it is missing, call
  `refresh_bookmarks` (mode="quick") and search again.
"""

mcp = _MCPServer("x-bookmarks", instructions=INSTRUCTIONS)


@mcp.tool()
def archive_status() -> dict:
    """Counts, coverage, sync freshness and top authors for the archive."""
    stats = q.stats()
    if not stats["total"]:
        stats["hint"] = (
            "Archive is empty. Run `xbm login` then `xbm sync --mode full` in a "
            "terminal inside the x-bookmarks project to backfill it."
        )
    return stats


@mcp.tool()
def search_bookmarks(
    query: str,
    limit: int = 25,
    author: str | None = None,
    folder: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Full-text search over saved bookmarks.

    Args:
        query: Free-text terms; all terms must match (prefix matching).
        limit: Maximum results (1-200).
        author: Restrict to an author handle, with or without a leading @.
        folder: Restrict to a bookmark folder name (X Premium folders).
        since: Inclusive ISO timestamp lower bound on the tweet's created_at.
        until: Inclusive ISO timestamp upper bound on the tweet's created_at.
    """
    return q.search_bookmarks(query, limit, author, folder, since, until)


@mcp.tool()
def recent_bookmarks(
    limit: int = 25,
    days: int | None = None,
    author: str | None = None,
    folder: str | None = None,
) -> list[dict]:
    """Most recently saved bookmarks, newest first.

    Args:
        limit: Maximum results (1-200).
        days: Only include bookmarks posted within the last N days.
        author: Restrict to an author handle, with or without a leading @.
        folder: Restrict to a bookmark folder name.
    """
    return q.recent_bookmarks(limit, days, author, folder)


@mcp.tool()
def get_bookmark(tweet_id_or_url: str) -> dict:
    """Fetch one bookmark by tweet id or full X/Twitter URL, including media and links."""
    found = q.get_bookmark(tweet_id_or_url)
    if found is None:
        return {"error": "not found in archive", "query": tweet_id_or_url}
    return found


@mcp.tool()
def list_folders() -> list[dict]:
    """List bookmark folders with their bookmark counts."""
    return q.list_folders()


@mcp.tool()
def top_authors(limit: int = 25, min_bookmarks: int = 2) -> list[dict]:
    """Authors you save the most, ranked by bookmark count."""
    return q.authors(limit, min_bookmarks)


@mcp.tool()
def sql_query(sql: str, limit: int = 200) -> list[dict]:
    """Run a read-only SELECT against the archive for custom analysis.

    Tables: bookmarks(tweet_id, url, full_text, created_at, status, lang,
    author_name, author_handle, author_followers, likes, retweets, replies,
    quotes, bookmarks, views, quoted_full_text, media_json, links_json, origin,
    first_seen_at, last_seen_at, content_updated_at, ...), folders(id, name),
    bookmark_folders(tweet_id, folder_id), meta(key, value).
    """
    return q.run_sql(sql, limit)


@mcp.tool()
def refresh_bookmarks(mode: str = "quick", limit: int | None = None) -> dict:
    """Pull new bookmarks from X into the archive.

    Args:
        mode: "quick" stops at the first run of already-archived bookmarks;
            "full" walks the entire bookmark timeline.
        limit: Optional cap on how many bookmarks to scan.
    """
    try:
        result = sync_mod.sync(mode=mode, limit=limit)
    except sync_mod.SyncError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": not result.errors,
        "mode": result.mode,
        "scanned": result.seen,
        "added": result.added,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "stopped": result.stopped_reason,
        "account": result.account,
        "errors": result.errors,
        "archive_total": db.counts(db.init())["total"],
    }


def main() -> None:
    db.init().close()
    mcp.run()


if __name__ == "__main__":
    main()
