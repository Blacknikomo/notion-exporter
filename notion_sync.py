#!/usr/bin/env python3
"""
Sync Notion pages to an Obsidian vault.

Usage:
    python notion_sync.py           # sync from last checkpoint
    python notion_sync.py --all     # full re-sync (ignores last_sync)
    python notion_sync.py --dry-run # print what would be synced, no writes

Config (via .env or environment variables):
    NOTION_TOKEN         — Notion Integration Token
    OBSIDIAN_VAULT_PATH  — path to the vault
"""

import argparse
import datetime
import urllib.parse
from pathlib import Path

import requests

from notion.api import NotionClient
from notion.config import load_config
from notion.vault import (
    DEFAULT_SINCE,
    RECORDINGS_DIR,
    build_page_document,
    build_vault_index,
    read_last_sync,
    write_last_sync,
    write_page,
)
from notion.converter import blocks_to_markdown


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Notion pages to an Obsidian vault.")
    parser.add_argument("--all", action="store_true", help="Full re-sync, ignore last_sync checkpoint.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing files.")
    return parser.parse_args()


def print_summary(synced: int, ai_notes_count: int, errors: list, dry_run: bool):
    print()
    print("Notion Sync Summary")
    print("-------------------")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Pages synced: {synced}")
    print(f"Pages with AI Notes / audio: {ai_notes_count}")
    print(f"Errors: {len(errors)}")
    for err in errors:
        print(f"  - {err}")


def main():
    args = parse_args()
    config = load_config()
    vault_path: Path = config["vault_path"]
    client = NotionClient(config["token"])

    # Capture before API calls so edits during the run are picked up next time
    run_start = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    since = DEFAULT_SINCE if args.all else read_last_sync(vault_path)
    print(f"Syncing pages edited after: {since}")

    print("Building vault index…")
    vault_index = build_vault_index(vault_path)
    print(f"Found {len(vault_index)} already-synced page(s) in vault.")

    print("Fetching pages from Notion…")
    try:
        pages = client.search_pages(since)
    except requests.HTTPError as exc:
        print(f"Error: could not fetch pages from Notion: {exc}")
        raise SystemExit(1) from exc

    print(f"Found {len(pages)} page(s) to sync.")

    synced = 0
    ai_notes_count = 0
    errors = []

    for page in pages:
        page_id = page["id"]
        title = _page_title(page)
        print(f"  Processing: {title!r} ({page_id})")

        try:
            blocks = client.get_blocks(page_id)
            conversion = blocks_to_markdown(blocks)
            filename, content = build_page_document(page, blocks)

            existing = vault_index.get(page_id)
            written = write_page(vault_path, filename, content, existing, args.dry_run)
            vault_index[page_id] = written

            for url in conversion.audio_urls:
                fname = Path(urllib.parse.urlparse(url).path).name or "recording"
                dest = vault_path / RECORDINGS_DIR / fname
                if args.dry_run:
                    print(f"[dry-run] Would download audio: {url} → {dest}")
                else:
                    client.download_file(url, dest)

            synced += 1
            if conversion.transcript_lines or conversion.audio_urls:
                ai_notes_count += 1

        except Exception as exc:  # noqa: BLE001
            msg = f"Page {page_id} ({title!r}): {exc}"
            print(f"  Error: {msg}")
            errors.append(msg)

    write_last_sync(vault_path, run_start, args.dry_run)
    print_summary(synced, ai_notes_count, errors, args.dry_run)


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            rt = prop.get("title", [])
            if rt:
                return "".join(item.get("plain_text", "") for item in rt)
    return "Untitled"


if __name__ == "__main__":
    main()
