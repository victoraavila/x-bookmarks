# x-bookmarks

A local, cumulative, searchable archive of your X (Twitter) bookmarks, exposed
to OpenCode through an MCP server — with a sync engine so recent saves stay
fresh without paying for the official X API.

## Why this shape

- X's official data archive **does not include bookmarks**, so "download your
  data" cannot help.
- The official API's bookmarks endpoint needs the **Basic tier ($200/mo)** and
  caps at ~800 bookmarks with no folders.
- `twscrape` talks to X's internal GraphQL with your own logged-in session. It
  paginates the **whole** bookmark collection (no 800 cap) and has a real
  `bookmarks()` method. That makes it the sync engine for both backfill and
  incremental refresh.
- The [xarchive](https://github.com/sytelus/xarchive) Chrome extension export is
  **optional enrichment**: it adds X Premium folder assignments and acts as a
  second, independent source.

```
            twscrape (live)          xarchive JSON (optional)
                   │                          │
                   └────────► xbm import ◄────┘
                                 │
                          data/bookmarks.db  (SQLite + FTS5)
                                 │
                     xbm.mcp_server  ──stdio──►  OpenCode
```

## Quick start

From this directory:

```bash
# 1. Save your X session (needs auth_token and ct0 cookies)
uv run xbm login --label main

# 2. Backfill everything you have saved so far
uv run xbm sync --mode full

# 3. Check what landed
uv run xbm stats
uv run xbm search "durable objects"
```

### Getting the cookies

By hand, one value at a time (recommended — the prompt does not echo what you
type):

```bash
uv run xbm login --label main
#   auth_token>  <paste the auth_token value>
#   ct0>         <paste the ct0 value>
```

On x.com open DevTools (F12) → Application → Cookies → `https://x.com`, then
copy the `auth_token` and `ct0` **values** (not the names). Pasting the whole
`auth_token=...; ct0=...` string at the first prompt also works — the second
prompt is then skipped.

Other accepted inputs:

```bash
# full cookie header in one go
unjar x.com -f header | uv run xbm login --label main --stdin

# two lines on stdin: auth_token first, then ct0
printf '%s\n%s\n' "$AUTH_TOKEN" "$CT0" | uv run xbm login --label main --stdin

# explicit flags — avoid: the values land in your shell history and `ps` output
uv run xbm login --label main --auth-token "$AUTH_TOKEN" --ct0 "$CT0"

# from a file
uv run xbm login --label main --cookie-file ~/.secrets/x-cookies.txt
```

A bare token cannot be accepted on its own: the cookie has to be named, because
X sets many cookies and only `auth_token` + `ct0` identify the session.

The session is stored in `data/accounts.db`. Cookies are credentials — that file
is gitignored and never written into your OpenCode config.

## Keeping it fresh

```bash
uv run xbm sync --mode quick     # newest-first, stops at the first known run
uv run xbm sync --mode full      # walks the entire collection
```

`quick` is the everyday command: it paginates newest-first and stops after
`--boundary` (default 25) consecutive bookmarks that are already archived, so it
is cheap and fast.

You can also refresh from inside OpenCode — the MCP server exposes
`refresh_bookmarks`:

> "refresh my bookmarks and then show me anything new about Rust"

### Optional: run it on a schedule (macOS)

`scripts/com.xbookmarks.sync.plist` is a launchd template that runs a quick sync
every 6 hours. Install it only if you want unattended syncing:

```bash
sed "s|__PROJECT__|$PWD|g" scripts/com.xbookmarks.sync.plist \
  > ~/Library/LaunchAgents/com.xbookmarks.sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xbookmarks.sync.plist
```

Remove it with `launchctl bootout gui/$(id -u)/com.xbookmarks.sync`.
Note that scraping runs against X's rate limits and its Terms of Service; the
schedule is conservative (one quick sync per 6 hours) and entirely opt-in.

## Adding the xarchive export (Premium folders)

1. Install the extension: `git clone https://github.com/sytelus/xarchive`, then
   `chrome://extensions` → Developer mode → **Load unpacked** → select the repo.
2. Visit x.com, click the extension, run **Full refresh**, and
   **Download combined archive**.
3. Import it:

```bash
uv run xbm import-xarchive ~/Downloads/xarchive-*.json
uv run xbm folders
```

Folder semantics follow xarchive's own merge rules: a *complete* folder
observation replaces membership, a *partial* one only adds. Nothing is ever
removed by a partial scan.

### Importing an existing `tweet_bookmarks`/`tweetxvault` archive?

The importer accepts any xarchive-format JSON (schema versions 1 and 2).

## MCP tools

Registered in OpenCode as `x-bookmarks` (see below). Tools become
`x-bookmarks_<tool>`, and in Code Mode they are grouped under
`tools["x-bookmarks"]` (bracket notation, because the name contains a hyphen):

| Tool | Purpose |
| --- | --- |
| `archive_status` | counts, coverage, freshness, top authors |
| `search_bookmarks` | full-text search, filter by author/folder/date |
| `recent_bookmarks` | newest saves first, optional `days` window |
| `get_bookmark` | one bookmark by id or URL, with media and links |
| `list_folders` | folder names and counts |
| `top_authors` | authors you save most |
| `sql_query` | read-only SQL for custom analysis |
| `refresh_bookmarks` | pull new bookmarks from X (quick/full) |

## CLI reference

```
xbm init                       create the database
xbm login [--label NAME]       save an X session cookie
xbm accounts                   list configured sessions
xbm sync --mode quick|full     pull bookmarks from X
xbm import-xarchive PATH       import an xarchive JSON export
xbm search QUERY [--author A] [--folder F] [--since D] [--until D]
xbm recent [--days N] [--author A]
xbm get ID_OR_URL
xbm folders | authors | stats
xbm sql "SELECT ..."
xbm doctor                     check archive + retrieval health
xbm reindex                    rebuild the full-text index
```

`sync --mode full` has no cap and walks until X stops returning pages.
`--limit N` sets an approximate cap (twscrape's limit is page-granular, so it
can overshoot).

Add `--json` to any read command for machine-readable output.

## How retrieval works

Search is **keyword-based**, using SQLite **FTS5** — not semantic/vector search.

- **Tokenizer**: `porter unicode61` — case-insensitive, Unicode-aware, with
  English stemming, so `launches` and `launch` are the same term (verified: both
  return 138 hits).
- **Matching**: quoted **prefix** terms, so `vector` also matches `vectorize`
  and `vectordb`.
- **Multi-word queries** are precise-first: all terms must match (implicit AND).
  If that returns nothing, the same terms are OR'd and BM25 puts documents
  matching more of them on top. This avoids the classic "no results" dead end.
- **Ranking**: BM25 relevance (`ORDER BY rank`) — *not* recency. For "what's new",
  use `recent_bookmarks` / `xbm recent`.
- **Indexed fields**: tweet text, author name/handle, expanded links, quoted-tweet
  text, and article title/summary. Metrics and media are stored but not indexed.

**What this is good and bad at**

| Good | Bad |
| --- | --- |
| Exact product/model names (`GRPO` → 81) | Paraphrase with no shared vocabulary |
| Identifiers, error strings, handles, URLs | Synonyms you didn't think of |
| Fast, offline, no API cost | Fuzzy/conceptual similarity |

The agent supplies the semantic layer: a question like *"posts about making
agents cheaper"* gets expanded into concrete terms (cost, pricing, KV cache,
quantization, distillation) and searched as several keyword queries. If you want
true semantic search, that means adding an embedding index — ask and it can be
added.

## Checking that retrieval works

```bash
uv run xbm doctor      # health check, exits non-zero on failure
uv run xbm reindex     # rebuild the full-text index if it ever drifts
```

`doctor` verifies the one failure mode that degrades search silently:

- the FTS index is in sync with the `bookmarks` table (no orphaned/missing rows);
- a randomized **end-to-end round-trip** — it picks a real bookmark, finds a token
  unique to it, and confirms the normal query path retrieves that same bookmark
  (plus the author filter). A corpus-unique token is used deliberately so the
  check is deterministic rather than at the mercy of BM25 ordering;
- your X session is present, and reports sync freshness.

The index is updated in the same database write as each bookmark, so it does not
drift during normal syncing.

`doctor` proves retrieval *works*; it does not prove ranking is *good*. That needs
a labelled query set (query → expected tweet ids), which can be added if you want
retrieval quality tracked over time.

## How content is preserved

The archive is cumulative and deliberately lossy-proof:

- An incoming record with an empty field **never** overwrites stored content.
- A bookmark that disappears from X is **retained**, not deleted.
- A tombstone (unavailable) updates the status but keeps the text, media URLs
  and author you already had.
- `first_seen_at` / `last_seen_at` / `content_updated_at` track observation
  history independently, so newer observations never erase older content.

## Data, privacy, and layout

```
data/bookmarks.db   archive (SQLite + FTS5)
data/accounts.db    X session cookies (twscrape)  — gitignored
```

Everything stays on this machine. Nothing is uploaded. Override locations with
`XB_DATA_DIR`, `XB_DB`, or `XB_ACCOUNTS_DB`. `XB_DATA_DIR` is what the OpenCode
MCP entry sets.

twscrape's anonymous operation-name telemetry is disabled by default
(`TWS_TELEMETRY=0`) and its logging is set to warnings (`TWS_LOG_LEVEL`); export
either variable yourself to change that.

## OpenCode MCP entry

Already added to `~/.config/opencode/opencode.json`:

```jsonc
"mcp": {
  "servers": {
    "x-bookmarks": {
      "type": "local",
      "command": ["/absolute/path/to/x-bookmarks/.venv/bin/python",
                  "-m", "xbm.mcp_server"],
      "cwd": "/absolute/path/to/x-bookmarks",
      "environment": { "XB_DATA_DIR": "/absolute/path/to/x-bookmarks/data" }
    }
  }
}
```

Verify with `opencode mcp list` and look for `x-bookmarks  connected`.

## Troubleshooting

- **`refresh_bookmarks` says no account configured** → run `xbm login`.
- **Sync starts erroring with a GraphQL/400 error** → X rotated an internal
  query id. Upgrade twscrape: `uv lock --upgrade-package twscrape && uv sync`.
  This is the usual failure mode for cookie-based scraping.
- **Sync is slow or rate-limited** → `quick` mode with a smaller `--boundary`,
  or add a proxy via `TWS_PROXY` (see twscrape docs).
- **Search matches nothing that clearly exists** → the FTS index uses prefix
  matching on whole terms; try fewer, more distinctive words.

## Development

```bash
uv run python scripts/mcp_smoke.py    # end-to-end MCP protocol check
```
