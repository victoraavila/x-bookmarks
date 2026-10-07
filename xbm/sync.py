"""Incremental sync of bookmarks from X via twscrape.

``sync(mode="full")`` walks the whole bookmark timeline (backfill).
``sync(mode="quick")`` stops once it has seen a run of bookmarks that are
already archived, which is how "always fresh" stays cheap: newest-first
pagination means the fresh items are at the front.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from . import db
from .importers import from_twscrape

DEFAULT_QUICK_BOUNDARY = 25
# `full` means full: 0 disables the cap and walks until X stops returning pages.
# twscrape's limit is page-granular and advisory, so a numeric cap can overshoot.
DEFAULT_FULL_LIMIT = 0
COMMIT_EVERY = 25


class SyncError(RuntimeError):
    """Raised when a sync cannot run (usually: no X account configured)."""


@dataclass
class SyncResult:
    mode: str
    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    stopped_reason: str = "exhausted"
    account: str | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"mode={self.mode}",
            f"scanned={self.seen}",
            f"added={self.added}",
            f"updated={self.updated}",
            f"unchanged={self.unchanged}",
            f"stopped={self.stopped_reason}",
        ]
        if self.account:
            parts.append(f"account={self.account}")
        return " ".join(parts)


async def list_accounts() -> list[dict]:
    """Return configured twscrape accounts without performing any request."""
    from twscrape import API

    api = API(str(db.accounts_db_path()))
    db.secure_accounts_file()
    accounts = await api.pool.get_all()
    return [
        {
            "username": acct.username,
            "active": bool(getattr(acct, "active", False)),
            "logged_in": bool(getattr(acct, "logged_in", False)),
            "last_used": str(getattr(acct, "last_used", "") or ""),
        }
        for acct in accounts
    ]


async def add_cookie(label: str, cookies: str) -> None:
    """Register a browser cookie string (needs ``auth_token`` and ``ct0``)."""
    from twscrape import API

    api = API(str(db.accounts_db_path()))
    db.secure_accounts_file()
    try:
        await api.pool.add_account_cookies(label, cookies)
    except ValueError as exc:
        raise SyncError(
            f"X session was rejected: {exc}. Paste the cookie values for "
            "auth_token and ct0 (a bare token is not accepted)."
        ) from exc


async def _run(
    mode: str,
    limit: int | None,
    boundary: int,
    progress=None,
) -> SyncResult:
    from twscrape import API

    if mode not in ("quick", "full"):
        raise SyncError(f"unknown sync mode: {mode!r}")

    result = SyncResult(mode=mode)
    api = API(
        str(db.accounts_db_path()),
        raise_when_no_account=True,
        wait_timeout=15,
        wait_interval=1,
    )
    db.secure_accounts_file()

    accounts = await api.pool.get_all()
    if not accounts:
        raise SyncError(
            "No X account is configured for twscrape. Run:\n"
            "  xbm login            # paste auth_token and ct0 cookies\n"
            "or:\n"
            "  unjar x.com -f header | xbm login --stdin"
        )

    conn = db.init()
    max_items = -1 if limit is None or limit <= 0 else limit
    known_streak = 0
    try:
        async for tweet in api.bookmarks(limit=max_items):
            rec = from_twscrape(tweet, observed_at=db.now_iso())
            was_known = (
                conn.execute(
                    "SELECT 1 FROM bookmarks WHERE tweet_id = ?", (rec["tweet_id"],)
                ).fetchone()
                is not None
            )
            outcome = db.upsert_bookmark(conn, rec)
            setattr(result, outcome, getattr(result, outcome) + 1)
            result.seen += 1

            if mode == "quick":
                known_streak = known_streak + 1 if was_known else 0
                if known_streak >= boundary:
                    result.stopped_reason = "reached known boundary"
                    break

            if result.seen % COMMIT_EVERY == 0:
                conn.commit()
                if progress:
                    progress(result)
        if mode == "full" and not result.errors:
            result.stopped_reason = "pagination ended"
            if max_items > 0:
                result.stopped_reason += f" (limit {max_items} requested)"
    except SyncError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface upstream failures verbatim
        result.errors.append(f"{type(exc).__name__}: {exc}")
        result.stopped_reason = "error"
    finally:
        conn.commit()

    # Identify the signed-in account from the pool rather than from a tweet author.
    for acct in accounts:
        if getattr(acct, "active", False):
            result.account = acct.username
            break

    db.set_meta(conn, "last_sync_at", db.now_iso())
    db.set_meta(conn, "last_sync_mode", mode)
    db.set_meta(conn, "last_sync_summary", result.summary())
    if mode == "full" and not result.errors:
        db.set_meta(conn, "last_full_sync_at", db.now_iso())
    conn.commit()
    conn.close()
    return result


def sync(
    mode: str = "quick",
    limit: int | None = None,
    boundary: int = DEFAULT_QUICK_BOUNDARY,
    progress=None,
) -> SyncResult:
    """Blocking entry point used by the CLI and the MCP server."""
    effective_limit = limit
    if effective_limit is None and mode == "full":
        effective_limit = DEFAULT_FULL_LIMIT
    return asyncio.run(_run(mode, effective_limit, boundary, progress))
