"""Normalize bookmark data from the two supported sources into one record shape.

Sources:
  * twscrape  - live GraphQL scraping (backfill + incremental refresh)
  * xarchive  - the Chrome extension's JSON export (enrichment: Premium folders)

Every normalized record is a flat dict whose keys match ``xbm.db.ALL_FIELDS``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# Provenance markers. A bookmark seen through both sources records "twscrape+xarchive".
ORIGIN_TWSCRAPE = "twscrape"
ORIGIN_XARCHIVE = "xarchive"


def _iso(value: Any) -> str | None:
    """Best-effort ISO-8601 with timezone, accepting datetimes, epochs and strings."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        normalized = cleaned.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            # X's classic format: "Wed Jan 06 18:40:40 +0000 2021"
            for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S%z"):
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue
            else:
                return cleaned
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else None
    return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tweet_url(tweet_id: str, handle: str | None) -> str:
    if handle:
        return f"https://x.com/{handle}/status/{tweet_id}"
    return f"https://x.com/i/status/{tweet_id}"


# --------------------------------------------------------------------------- #
# twscrape
# --------------------------------------------------------------------------- #

def _media_from_twscrape(media: dict) -> list[dict]:
    items: list[dict] = []
    for photo in media.get("photos") or []:
        if photo.get("url"):
            items.append({"type": "photo", "url": photo["url"], "thumbnail_url": photo["url"]})
    for video in media.get("videos") or []:
        variants = sorted(
            (v for v in (video.get("variants") or []) if v.get("url")),
            key=lambda v: v.get("bitrate") or 0,
            reverse=True,
        )
        items.append(
            {
                "type": "video",
                "url": variants[0]["url"] if variants else video.get("thumbnailUrl"),
                "thumbnail_url": video.get("thumbnailUrl"),
                "duration_ms": video.get("duration"),
                "variants": video.get("variants"),
            }
        )
    for gif in media.get("animated") or []:
        items.append(
            {
                "type": "gif",
                "url": gif.get("videoUrl"),
                "thumbnail_url": gif.get("thumbnailUrl"),
            }
        )
    return items


def _entities_from_twscrape(tw: dict) -> dict:
    mentions = [
        {
            "screen_name": m.get("username"),
            "user_id": str(m.get("id_str") or m.get("id") or ""),
        }
        for m in tw.get("mentionedUsers") or []
    ]
    return {
        "hashtags": list(tw.get("hashtags") or []),
        "cashtags": list(tw.get("cashtags") or []),
        "mentions": mentions,
    }


def _links_from_twscrape(tw: dict) -> list[str]:
    urls = []
    for link in tw.get("links") or []:
        url = link.get("url") or link.get("tcourl")
        if url:
            urls.append(url)
    return urls


def from_twscrape(tw: Any, observed_at: str | None = None) -> dict:
    """Normalize a ``twscrape.models.Tweet`` (or its ``.dict()``)."""
    data = tw.dict() if hasattr(tw, "dict") else dict(tw)

    tweet_id = str(data.get("id_str") or data.get("id") or "")
    if not tweet_id:
        raise ValueError("twscrape record is missing an id")

    user = data.get("user") or {}
    handle = user.get("username")
    quoted = data.get("quotedTweet") or {}
    quoted_user = quoted.get("user") or {}
    quoted_id = quoted.get("id_str") or quoted.get("id")
    article = data.get("article") or {}
    media = _media_from_twscrape(data.get("media") or {})
    entities = _entities_from_twscrape(data)
    links = _links_from_twscrape(data)

    # `date` is the tweet's own creation time.
    created_at = _iso(data.get("date"))
    text = data.get("rawContent") or ""

    return {
        "tweet_id": tweet_id,
        "url": data.get("url") or tweet_url(tweet_id, handle),
        "full_text": text or None,
        "lang": data.get("lang"),
        "created_at": created_at,
        "status": "available",
        "source": data.get("sourceUrl"),
        "source_label": data.get("sourceLabel"),
        "conversation_id": _str_or_none(data.get("conversationIdStr") or data.get("conversationId")),
        "in_reply_to_tweet_id": _str_or_none(
            data.get("inReplyToTweetIdStr") or data.get("inReplyToTweetId")
        ),
        "in_reply_to_screen_name": data.get("inReplyToScreenName"),
        "author_id": _str_or_none(user.get("id_str") or user.get("id")),
        "author_name": user.get("displayname"),
        "author_handle": handle,
        "author_followers": _int(user.get("followersCount")),
        "author_profile_image": user.get("profileImageUrl"),
        "likes": _int(data.get("likeCount")),
        "retweets": _int(data.get("retweetCount")),
        "replies": _int(data.get("replyCount")),
        "quotes": _int(data.get("quoteCount")),
        "bookmarks": _int(data.get("bookmarkedCount")),
        "views": _int(data.get("viewCount")),
        "quoted_tweet_id": _str_or_none(quoted_id),
        "quoted_full_text": quoted.get("rawContent") or None,
        "quoted_author_handle": quoted_user.get("username"),
        "quoted_author_name": quoted_user.get("displayname"),
        "card_type": (data.get("card") or {}).get("_type"),
        "card_title": (data.get("card") or {}).get("title"),
        "card_url": (data.get("card") or {}).get("url"),
        "article_title": article.get("title"),
        "article_summary": article.get("summaryText") or article.get("preview_text"),
        "media_json": _json(media),
        "entities_json": _json(entities),
        "hashtags_json": _json(entities.get("hashtags")),
        "links_json": _json(links),
        "raw_json": None,
        "sort_index": None,
        "origin": ORIGIN_TWSCRAPE,
        "last_seen_at": observed_at,
    }


# --------------------------------------------------------------------------- #
# xarchive
# --------------------------------------------------------------------------- #

def _media_from_xarchive(media: list[dict]) -> list[dict]:
    items = []
    for entry in media or []:
        if not isinstance(entry, dict):
            continue
        items.append(
            {
                "type": entry.get("type"),
                "url": entry.get("url") or entry.get("thumbnail_url"),
                "thumbnail_url": entry.get("thumbnail_url"),
                "alt_text": entry.get("alt_text"),
                "duration_ms": entry.get("duration_ms"),
                "variants": entry.get("variants"),
            }
        )
    return items


def _links_from_xarchive(entities: dict) -> list[str]:
    urls = []
    for entry in (entities or {}).get("urls") or []:
        if isinstance(entry, dict):
            url = entry.get("expanded_url") or entry.get("url")
            if url:
                urls.append(url)
        elif isinstance(entry, str) and entry:
            urls.append(entry)
    return urls


def from_xarchive(bookmark: dict, exported_at: str | None = None) -> dict:
    """Normalize one entry of an xarchive JSON ``bookmarks`` array."""
    tweet_id = str(bookmark.get("tweet_id") or "")
    if not tweet_id:
        raise ValueError("xarchive record is missing tweet_id")

    author = bookmark.get("author") or {}
    metrics = bookmark.get("metrics") or {}
    quoted = bookmark.get("quoted_tweet") or {}
    quoted_author = quoted.get("author") or {}
    card = bookmark.get("card") or {}
    entities = bookmark.get("entities") or {}
    archive = bookmark.get("_archive") or {}

    handle = author.get("screen_name")
    links = _links_from_xarchive(entities)
    media = _media_from_xarchive(bookmark.get("media") or [])

    status = bookmark.get("status")
    if status not in ("available", "unavailable"):
        status = "available" if bookmark.get("full_text") else "unavailable"

    return {
        "tweet_id": tweet_id,
        "url": tweet_url(tweet_id, handle),
        "full_text": bookmark.get("full_text") or None,
        "lang": bookmark.get("lang"),
        "created_at": _iso(bookmark.get("created_at")),
        "status": status,
        "unavailable_reason": bookmark.get("unavailable_reason"),
        "source": bookmark.get("source"),
        "source_label": None,
        "conversation_id": _str_or_none(bookmark.get("conversation_id")),
        "in_reply_to_tweet_id": _str_or_none(bookmark.get("in_reply_to_tweet_id")),
        "in_reply_to_screen_name": None,
        "author_id": _str_or_none(author.get("user_id")),
        "author_name": author.get("name"),
        "author_handle": handle,
        "author_followers": _int(author.get("followers_count")),
        "author_profile_image": author.get("profile_image_url"),
        "likes": _int(metrics.get("likes")),
        "retweets": _int(metrics.get("retweets")),
        "replies": _int(metrics.get("replies")),
        "quotes": _int(metrics.get("quotes")),
        "bookmarks": _int(metrics.get("bookmarks")),
        "views": _int(metrics.get("views")),
        "quoted_tweet_id": _str_or_none(quoted.get("tweet_id")),
        "quoted_full_text": quoted.get("full_text") or None,
        "quoted_author_handle": quoted_author.get("screen_name"),
        "quoted_author_name": quoted_author.get("name"),
        "card_type": card.get("type"),
        "card_title": card.get("title"),
        "card_url": card.get("url"),
        "article_title": None,
        "article_summary": None,
        "media_json": _json(media),
        "entities_json": _json(
            {
                "hashtags": list(entities.get("hashtags") or []),
                "mentions": entities.get("mentions") or [],
            }
        ),
        "hashtags_json": _json(list(entities.get("hashtags") or [])),
        "links_json": _json(links),
        "raw_json": None,
        "sort_index": _int(bookmark.get("sort_index")),
        "origin": ORIGIN_XARCHIVE,
        "first_seen_at": _iso(archive.get("first_seen_at")) or exported_at,
        "last_seen_at": _iso(archive.get("last_seen_at")) or exported_at,
        "content_updated_at": _iso(archive.get("content_updated_at")),
    }


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def folder_ids_for(bookmark: dict) -> list[str]:
    """xarchive records folder membership by stable id, falling back to name."""
    ids = bookmark.get("folder_ids")
    if isinstance(ids, list) and ids:
        return [str(x) for x in ids if x]
    names = bookmark.get("folders")
    if isinstance(names, list):
        return [f"legacy:{x}" for x in names if x]
    return []


def folders_complete_for(bookmark: dict, default: bool = False) -> bool:
    """Whether this record's folder membership is a complete observation.

    A complete observation may replace (and therefore remove) memberships; an
    incomplete one may only add. Missing per-record metadata falls back to the
    archive-level completeness flag.
    """
    archive = bookmark.get("_archive") or {}
    value = archive.get("folders_complete")
    if isinstance(value, bool):
        return value
    return default
