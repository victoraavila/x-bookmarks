"""Import an xarchive JSON export (backfill + Premium folder assignments)."""

from __future__ import annotations

import json
from pathlib import Path

from . import db
from .importers import folder_ids_for, folders_complete_for, from_xarchive


class ImportError_(RuntimeError):
    pass


def import_xarchive(path: str | Path) -> dict:
    source = Path(path).expanduser()
    if not source.exists():
        raise ImportError_(f"file not found: {source}")

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImportError_(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("bookmarks"), list):
        raise ImportError_(
            "expected an xarchive JSON object with a 'bookmarks' array"
        )

    metadata = data.get("export_metadata") or {}
    schema_version = metadata.get("schema_version")
    if isinstance(schema_version, int) and schema_version > 2:
        raise ImportError_(
            f"archive schema version {schema_version} is newer than this tool supports"
        )
    exported_at = db.now_iso()
    if metadata.get("exported_at"):
        exported_at = str(metadata["exported_at"])
    collection = metadata.get("collection") or {}
    folders_are_complete = collection.get("folders") == "complete"

    conn = db.init()
    summary = {
        "file": str(source),
        "account": metadata.get("screen_name"),
        "user_id": metadata.get("user_id"),
        "kind": metadata.get("kind"),
        "exported_at": exported_at,
        "folders": 0,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "errors": [],
    }

    try:
        for folder in data.get("folders") or []:
            fid = str(folder.get("id") or "")
            name = folder.get("name")
            if fid and name:
                db.upsert_folder(conn, fid, str(name))
                summary["folders"] += 1

        for bookmark in data["bookmarks"]:
            try:
                rec = from_xarchive(bookmark, exported_at=exported_at)
            except ValueError as exc:
                summary["errors"].append(str(exc))
                continue

            outcome = db.upsert_bookmark(conn, rec)
            summary[outcome] += 1

            folder_ids = folder_ids_for(bookmark)
            for fid in folder_ids:
                if fid.startswith("legacy:"):
                    db.upsert_folder(conn, fid, fid.split(":", 1)[1])
            complete = folders_complete_for(bookmark, default=folders_are_complete)
            if complete:
                # Authoritative observation: may add or remove memberships.
                db.set_bookmark_folders(conn, rec["tweet_id"], folder_ids)
            elif folder_ids:
                # Partial observation: record observed memberships, never remove.
                db.add_bookmark_folders(conn, rec["tweet_id"], folder_ids)

        db.set_meta(conn, "last_xarchive_import_at", db.now_iso())
        if summary["account"]:
            db.set_meta(conn, "account_screen_name", str(summary["account"]))
        if summary["user_id"]:
            db.set_meta(conn, "account_user_id", str(summary["user_id"]))
        conn.commit()
    finally:
        conn.close()
    return summary
