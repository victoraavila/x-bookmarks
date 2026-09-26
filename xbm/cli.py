"""Command line interface: ``xbm <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import db
from . import ingest
from . import query as q
from . import sync as sync_mod


def _print_json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _format_bookmark(item: dict, *, indent: str = "  ") -> str:
    lines = []
    stamp = (item.get("created_at") or item.get("first_seen_at") or "")[:10]
    handle = item.get("author_handle") or "?"
    name = item.get("author_name") or ""
    status = item.get("status") or "available"
    marker = "" if status == "available" else f"  [{status}]"
    lines.append(f"{stamp}  @{handle} ({name}){marker}")
    text = (item.get("full_text") or "").strip()
    if text:
        lines.append(f"{indent}{text}")
    if item.get("quoted_full_text"):
        lines.append(f"{indent}quoted @{item.get('quoted_author_handle') or '?'}: {item['quoted_full_text']}")
    metrics = [
        f"{k}={item[k]}"
        for k in ("likes", "retweets", "replies", "views")
        if item.get(k) is not None
    ]
    if metrics:
        lines.append(f"{indent}{' '.join(metrics)}")
    if item.get("folders"):
        lines.append(f"{indent}folders: {', '.join(item['folders'])}")
    if item.get("url"):
        lines.append(f"{indent}{item['url']}")
    return "\n".join(lines)


def cmd_init(args) -> int:
    db.init().close()
    print(f"initialized {db.db_path()}")
    return 0


def _clean_cookie_value(raw: str) -> str:
    return raw.strip().strip('"').strip("'").strip()


def _looks_like_cookie_string(text: str) -> bool:
    lowered = (text or "").lower()
    return "auth_token" in lowered and "ct0" in lowered


def _strip_cookie_prefix(text: str) -> str:
    value = text.strip()
    if value[:7].lower() == "cookie:":
        value = value.split(":", 1)[1].strip()
    return value


def _build_cookie_string(auth_token: str, ct0: str) -> str | None:
    parts = []
    for name, value in (("auth_token", auth_token), ("ct0", ct0)):
        cleaned = _clean_cookie_value(value)
        if not cleaned:
            print(f"{name} cannot be empty", file=sys.stderr)
            return None
        parts.append(f"{name}={cleaned}")
    return "; ".join(parts)


def cmd_login(args) -> int:
    cookies: str | None = None
    source = "prompt"

    if args.cookie_file:
        path = Path(args.cookie_file).expanduser()
        if not path.exists():
            print(f"cookie file not found: {path}", file=sys.stderr)
            return 2
        cookies = path.read_text(encoding="utf-8")
        source = str(path)
    elif args.cookie:
        cookies = args.cookie
        source = "--cookie"
    elif args.stdin:
        cookies = sys.stdin.read()
        source = "stdin"
    elif args.auth_token or args.ct0:
        if not (args.auth_token and args.ct0):
            print("--auth-token and --ct0 must be provided together", file=sys.stderr)
            return 2
        cookies = _build_cookie_string(args.auth_token, args.ct0)
        source = "--auth-token/--ct0"

    if cookies is not None:
        cookies = _strip_cookie_prefix(cookies)
        if not _looks_like_cookie_string(cookies):
            # Allow "auth_token\nct0" piped on two lines.
            values = [
                _clean_cookie_value(line)
                for line in cookies.splitlines()
                if line.strip()
            ]
            if len(values) >= 2:
                cookies = _build_cookie_string(values[0], values[1])
                source = f"{source} (two values)"

    if cookies is None:
        print(
            "Open x.com in your browser, then DevTools (F12) -> Application ->\n"
            "Cookies -> https://x.com, and paste each value when asked.\n"
            "Values are read as plain text and stored only in "
            f"{db.accounts_db_path()}\n"
        )
        try:
            auth_token = input("auth_token> ")
            if _looks_like_cookie_string(auth_token):
                # The whole cookie string was pasted at the first prompt.
                cookies = _strip_cookie_prefix(auth_token)
            else:
                ct0 = input("ct0>        ")
                cookies = _build_cookie_string(auth_token, ct0)
        except (EOFError, KeyboardInterrupt):
            print("\nlogin cancelled", file=sys.stderr)
            return 2

    if not cookies:
        print("no cookies provided", file=sys.stderr)
        return 2
    if not _looks_like_cookie_string(cookies):
        print(
            "could not find both auth_token and ct0 in the input.\n"
            "Paste the cookie *values* (or a full 'auth_token=...; ct0=...' "
            "string), not a bare token.",
            file=sys.stderr,
        )
        return 2

    try:
        asyncio.run(sync_mod.add_cookie(args.label, cookies))
    except sync_mod.SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"saved X session as '{args.label}' in {db.accounts_db_path()}")
    print("next: uv run xbm sync --mode full")
    return 0


def cmd_accounts(args) -> int:
    accounts = asyncio.run(sync_mod.list_accounts())
    if args.json:
        _print_json(accounts)
    elif not accounts:
        print("no accounts configured (run: xbm login)")
    else:
        for account in accounts:
            flag = "active" if account["active"] else "inactive"
            print(f"{account['username']}  {flag}  last_used={account['last_used'] or 'never'}")
    return 0


def cmd_sync(args) -> int:
    def progress(result) -> None:
        print(
            f"\rscanning... seen={result.seen} added={result.added} "
            f"updated={result.updated}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    try:
        result = sync_mod.sync(
            mode=args.mode,
            limit=args.limit,
            boundary=args.boundary,
            progress=None if args.json else progress,
        )
    except sync_mod.SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.json:
        print("", file=sys.stderr)
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)

    if args.json:
        _print_json(result.__dict__)
    else:
        print(result.summary())
        total = db.counts(db.init())["total"]
        print(f"archive now holds {total} bookmarks")
    return 1 if result.errors else 0


def cmd_import(args) -> int:
    try:
        summary = ingest.import_xarchive(args.path)
    except ingest.ImportError_ as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(summary)
    else:
        extras = f" ({len(summary['errors'])} skipped)" if summary["errors"] else ""
        print(
            f"imported from {summary['file']}: "
            f"{summary['added']} added, {summary['updated']} refreshed, "
            f"{summary['unchanged']} unchanged, {summary['folders']} folders{extras}"
        )
        for error in summary["errors"][:10]:
            print(f"  skipped: {error}", file=sys.stderr)
    return 0


def cmd_search(args) -> int:
    try:
        results = q.search_bookmarks(
            args.query, args.limit, args.author, args.folder, args.since, args.until
        )
    except ValueError as exc:
        print(f"invalid query: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(results)
    elif not results:
        print("no matches")
    else:
        print(f"{len(results)} match(es):\n")
        for item in results:
            print(_format_bookmark(item))
            print()
    return 0


def cmd_recent(args) -> int:
    results = q.recent_bookmarks(args.limit, args.days, args.author, args.folder)
    if args.json:
        _print_json(results)
    elif not results:
        print("archive is empty")
    else:
        for item in results:
            print(_format_bookmark(item))
            print()
    return 0


def cmd_get(args) -> int:
    found = q.get_bookmark(args.tweet)
    if found is None:
        print("not found in archive", file=sys.stderr)
        return 1
    _print_json(found) if args.json else print(_format_bookmark(found))
    return 0


def cmd_folders(args) -> int:
    folders = q.list_folders()
    if args.json:
        _print_json(folders)
    elif not folders:
        print("no folders (import an xarchive export to capture Premium folders)")
    else:
        for folder in folders:
            print(f"{folder['bookmarks']:>6}  {folder['name']}")
    return 0


def cmd_authors(args) -> int:
    authors = q.authors(args.limit, args.min_bookmarks)
    if args.json:
        _print_json(authors)
    elif not authors:
        print("no authors yet")
    else:
        for author in authors:
            print(f"{author['bookmarks']:>6}  @{author['author_handle']} ({author['author_name']})")
    return 0


def cmd_stats(args) -> int:
    stats = q.stats()
    if args.json:
        _print_json(stats)
    else:
        print(f"bookmarks : {stats['total']} ({stats['available']} available, {stats['unavailable']} unavailable)")
        print(f"folders   : {stats['folders']}")
        print(f"newest    : {stats['newest'] or 'n/a'}")
        print(f"last sync : {stats['last_sync_at'] or 'never'} ({stats['last_sync_mode'] or '-'})")
        print(f"full sync : {stats['last_full_sync_at'] or 'never'}")
        print(f"xarchive  : {stats['last_xarchive_import_at'] or 'never'}")
        if stats["by_origin"]:
            print("origins   : " + ", ".join(f"{r['origin']}={r['count']}" for r in stats["by_origin"]))
        if stats["top_authors"]:
            print("top authors:")
            for author in stats["top_authors"]:
                print(f"  {author['bookmarks']:>5}  @{author['author_handle']}")
    return 0


def cmd_sql(args) -> int:
    try:
        rows = q.run_sql(args.sql, args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"query failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            print(row)
    return 0


def cmd_doctor(args) -> int:
    conn = db.init()
    try:
        info = db.counts(conn)
        fts = db.fts_counts(conn)
    finally:
        conn.close()

    failures = 0
    warnings = 0

    def report(label: str, state: str, detail: str) -> None:
        nonlocal failures, warnings
        if state == "fail":
            failures += 1
        elif state == "warn":
            warnings += 1
        print(f"{state.upper():<4}  {label:<13} {detail}")

    print(f"archive       {info['total']} bookmarks "
          f"({info['available']} available), {info['folders']} folders")
    report(
        "search index",
        "pass" if fts["in_sync"] else "fail",
        f"indexed={fts['indexed']} orphaned={fts['orphaned']} missing={fts['missing']}"
        + ("" if fts["in_sync"] else "  (run: xbm reindex)"),
    )

    test = q.self_test()
    report(
        "retrieval",
        "pass" if test.get("ok") else "fail",
        test.get("reason")
        or (
            f"token={test['token']!r} -> {test['hits']} hit(s), found={test['found']}, "
            f"author_filter={test['author_filter_ok']}"
        ),
    )

    accounts = asyncio.run(sync_mod.list_accounts())
    active = [account for account in accounts if account["active"]]
    report(
        "x session",
        "pass" if active else "warn",
        ", ".join(account["username"] for account in active)
        or "none configured (run: xbm login)",
    )

    stats = q.stats()
    report(
        "freshness",
        "pass" if stats["last_sync_at"] else "warn",
        f"last sync {stats['last_sync_at'] or 'never'}"
        f" (mode {stats['last_sync_mode'] or '-'})",
    )

    if args.json:
        _print_json(
            {
                "counts": info,
                "fts": fts,
                "self_test": test,
                "active_accounts": [a["username"] for a in active],
                "last_sync_at": stats["last_sync_at"],
                "failures": failures,
                "warnings": warnings,
            }
        )
    else:
        print(f"\n{failures} failure(s), {warnings} warning(s)")
    return 1 if failures else 0


def cmd_reindex(args) -> int:
    conn = db.init()
    try:
        indexed = db.reindex(conn)
        fts = db.fts_counts(conn)
    finally:
        conn.close()
    if args.json:
        _print_json({"indexed": indexed, **fts})
    else:
        print(f"reindexed {indexed} bookmarks; in_sync={fts['in_sync']}")
    return 0 if fts["in_sync"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xbm", description="Local, searchable archive of your X bookmarks"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the archive database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("login", help="save an X session cookie for scraping")
    p.add_argument("--label", default="main", help="local name for the session")
    p.add_argument("--stdin", action="store_true", help="read cookies from stdin")
    p.add_argument("--cookie", help="full cookie string (visible in shell history)")
    p.add_argument("--cookie-file", help="read the cookie string from a file")
    p.add_argument("--auth-token", help="auth_token value only")
    p.add_argument("--ct0", help="ct0 value only")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("accounts", help="list configured X sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("sync", help="pull bookmarks from X")
    p.add_argument("--mode", choices=["quick", "full"], default="quick")
    p.add_argument("--limit", type=int, default=None, help="cap how many to scan")
    p.add_argument("--boundary", type=int, default=sync_mod.DEFAULT_QUICK_BOUNDARY,
                   help="quick mode: stop after this many consecutive known bookmarks")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("import-xarchive", help="import an xarchive JSON export")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("search", help="full-text search the archive")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--author")
    p.add_argument("--folder")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recent", help="newest bookmarks first")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--days", type=int)
    p.add_argument("--author")
    p.add_argument("--folder")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("get", help="show one bookmark by id or URL")
    p.add_argument("tweet")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("folders", help="list bookmark folders")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_folders)

    p = sub.add_parser("authors", help="authors ranked by bookmarks saved")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--min-bookmarks", type=int, default=2)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_authors)

    p = sub.add_parser("stats", help="archive coverage and freshness")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sql", help="run a read-only SQL query")
    p.add_argument("sql")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("doctor", help="check archive and retrieval health")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("reindex", help="rebuild the full-text search index")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reindex)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
