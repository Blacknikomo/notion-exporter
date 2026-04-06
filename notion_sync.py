#!/usr/bin/env python3
"""
notion_sync.py — Sync Notion pages to an Obsidian vault.

Usage:
    python notion_sync.py           # sync from last checkpoint
    python notion_sync.py --all     # full re-sync (ignores last_sync)
    python notion_sync.py --dry-run # print what would be synced, no writes

Config (via .env or environment variables):
    NOTION_TOKEN         — Notion Integration Token
    OBSIDIAN_VAULT_PATH  — path to the vault, e.g. /Users/you/Documents/Obsidian Vault
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_SINCE = "2026-03-25T00:00:00.000Z"
SYNC_STATE_REL = ".notion-sync/last_sync.json"
RECORDINGS_DIR = "recordings"

# Folders to exclude from vault index scanning and file writes (like .gitignore)
EXCLUDED_DIRS = {"Notion backup"}

# Characters forbidden in filenames on most OSes (plus backslash)
FORBIDDEN_CHARS_RE = re.compile(r'[/:*?"<>|\\]')

# Simple date pattern in titles: YYYY-MM-DD, YYYY/MM/DD, D/M/YY, etc.
DATE_IN_TITLE_RE = re.compile(
    r'\b(\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b'
)


# ---------------------------------------------------------------------------
# .env parser
# ---------------------------------------------------------------------------

def parse_dotenv(path: Path) -> dict:
    result = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    env = {}
    dotenv_path = Path(".env")
    if dotenv_path.exists():
        env = parse_dotenv(dotenv_path)

    # os.environ takes precedence
    token = os.environ.get("NOTION_TOKEN") or env.get("NOTION_TOKEN")
    vault_path_str = os.environ.get("OBSIDIAN_VAULT_PATH") or env.get("OBSIDIAN_VAULT_PATH")

    missing = []
    if not token:
        missing.append("NOTION_TOKEN")
    if not vault_path_str:
        missing.append("OBSIDIAN_VAULT_PATH")

    if missing:
        print(f"Error: missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        print("Set them in a .env file or as environment variables.", file=sys.stderr)
        sys.exit(1)

    vault_path = Path(vault_path_str).expanduser()
    if not vault_path.exists():
        print(f"Error: OBSIDIAN_VAULT_PATH does not exist: {vault_path}", file=sys.stderr)
        sys.exit(1)

    return {"token": token, "vault_path": vault_path}


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------

def read_last_sync(vault_path: Path) -> str:
    state_file = vault_path / SYNC_STATE_REL
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data.get("last_sync", DEFAULT_SINCE)
    except (OSError, json.JSONDecodeError, KeyError):
        return DEFAULT_SINCE


def write_last_sync(vault_path: Path, timestamp: str, dry_run: bool):
    state_file = vault_path / SYNC_STATE_REL
    if dry_run:
        print(f"[dry-run] Would write last_sync: {timestamp} → {state_file}")
        return
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_sync": timestamp}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Notion API client
# ---------------------------------------------------------------------------

class NotionClient:
    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict = None, timeout: int = 60) -> dict:
        url = f"{NOTION_API_BASE}{path}"
        resp = self._session.get(url, params=params, timeout=timeout)
        self._handle_rate_limit(resp)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict = None) -> dict:
        url = f"{NOTION_API_BASE}{path}"
        resp = self._session.post(url, json=body or {}, timeout=30)
        self._handle_rate_limit(resp)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _handle_rate_limit(resp: requests.Response):
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 1))
            print(f"Rate limited. Waiting {retry_after}s…")
            time.sleep(retry_after)

    def search_pages(self, since: str) -> list:
        """Return all pages with last_edited_time > since, sorted desc."""
        pages = []
        cursor = None

        while True:
            body = {
                "filter": {"value": "page", "property": "object"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor

            data = self._post("/search", body)
            results = data.get("results", [])

            for page in results:
                last_edited = page.get("last_edited_time", "")
                if last_edited <= since:
                    # Results are descending; everything from here is older
                    return pages
                pages.append(page)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return pages

    def get_blocks(self, block_id: str, _depth: int = 0) -> list:
        """Fetch all children of a block, recursing into nested blocks (max depth 5)."""
        blocks = []
        cursor = None

        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor

            data = self._get(f"/blocks/{block_id}/children", params, timeout=60)
            results = data.get("results", [])

            for block in results:
                btype = block.get("type", "")
                # child_page blocks are separate pages; don't recurse into them
                if block.get("has_children") and _depth < 5 and btype != "child_page":
                    try:
                        block["children"] = self.get_blocks(block["id"], _depth + 1)
                    except requests.HTTPError as exc:
                        block["children"] = []
                        print(f"  Warning: could not fetch children of block {block['id']}: {exc}")
                else:
                    block["children"] = []
                blocks.append(block)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return blocks

    def download_audio(self, url: str, dest_path: Path, dry_run: bool) -> bool:
        if dry_run:
            print(f"[dry-run] Would download audio: {url} → {dest_path}")
            return True
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with self._session.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with dest_path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)
            return True
        except (requests.HTTPError, requests.RequestException, OSError) as exc:
            print(f"  Warning: failed to download audio {url}: {exc}", file=sys.stderr)
            return False


# ---------------------------------------------------------------------------
# Block → Markdown converter
# ---------------------------------------------------------------------------

def extract_rich_text(rich_text_list: list) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text_list)


def _get_file_url(block_data: dict) -> str:
    """Extract URL from a file-type block (image, audio, video, file)."""
    file_obj = block_data.get("file") or block_data.get("external") or {}
    return (
        block_data.get("file", {}).get("url", "")
        or block_data.get("external", {}).get("url", "")
        or file_obj.get("url", "")
    )


def blocks_to_markdown(blocks: list, indent: int = 0) -> tuple:
    """
    Convert a list of Notion block dicts to Markdown.

    Returns:
        (markdown_lines: list[str], transcript_lines: list[str], audio_urls: list[str])
    """
    md_lines = []
    transcript_lines = []
    audio_urls = []
    prefix = "  " * indent
    capturing_transcript = False

    for block in blocks:
        btype = block.get("type", "")
        bdata = block.get(btype, {})
        children = block.get("children", [])

        # Detect transcript section (heading_2 with "Transcript" text)
        if btype in ("heading_1", "heading_2", "heading_3"):
            text = extract_rich_text(bdata.get("rich_text", []))
            level = {"heading_1": 1, "heading_2": 2, "heading_3": 3}[btype]
            hashes = "#" * level
            md_lines.append(f"{prefix}{hashes} {text}")
            md_lines.append("")
            capturing_transcript = text.strip().lower() == "transcript"
            continue

        if btype == "paragraph":
            text = extract_rich_text(bdata.get("rich_text", []))
            if capturing_transcript and text:
                transcript_lines.append(text)
            else:
                md_lines.append(f"{prefix}{text}")
                md_lines.append("")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                md_lines.extend(child_md)
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)
            continue

        # All non-paragraph/heading blocks stop transcript capture
        capturing_transcript = False

        if btype == "bulleted_list_item":
            text = extract_rich_text(bdata.get("rich_text", []))
            md_lines.append(f"{prefix}- {text}")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                md_lines.extend(child_md)
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "numbered_list_item":
            text = extract_rich_text(bdata.get("rich_text", []))
            md_lines.append(f"{prefix}1. {text}")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                md_lines.extend(child_md)
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "to_do":
            text = extract_rich_text(bdata.get("rich_text", []))
            checked = bdata.get("checked", False)
            box = "[x]" if checked else "[ ]"
            md_lines.append(f"{prefix}- {box} {text}")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                md_lines.extend(child_md)
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "code":
            text = extract_rich_text(bdata.get("rich_text", []))
            lang = bdata.get("language", "")
            md_lines.append(f"{prefix}```{lang}")
            for code_line in text.splitlines():
                md_lines.append(f"{prefix}{code_line}")
            md_lines.append(f"{prefix}```")
            md_lines.append("")

        elif btype == "quote":
            text = extract_rich_text(bdata.get("rich_text", []))
            for qline in text.splitlines() or [""]:
                md_lines.append(f"{prefix}> {qline}")
            md_lines.append("")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                for cline in child_md:
                    md_lines.append(f"> {cline}")
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "divider":
            md_lines.append(f"{prefix}---")
            md_lines.append("")

        elif btype == "callout":
            text = extract_rich_text(bdata.get("rich_text", []))
            icon = bdata.get("icon", {})
            emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
            callout_text = f"**{emoji}** {text}" if emoji else text
            md_lines.append(f"{prefix}> {callout_text}")
            md_lines.append("")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                for cline in child_md:
                    md_lines.append(f"> {cline}")
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "image":
            url = _get_file_url(bdata)
            caption = extract_rich_text(bdata.get("caption", []))
            md_lines.append(f"{prefix}![{caption}]({url})")
            md_lines.append("")

        elif btype == "audio":
            url = _get_file_url(bdata)
            if url:
                audio_urls.append(url)

        elif btype == "child_page":
            title = bdata.get("title", "")
            md_lines.append(f"{prefix}[[{title}]]")
            md_lines.append("")

        elif btype == "toggle":
            text = extract_rich_text(bdata.get("rich_text", []))
            md_lines.append(f"{prefix}- {text}")
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent + 1)
                md_lines.extend(child_md)
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "table":
            # Tables: delegate to children (table_row blocks)
            if children:
                child_md, child_tr, child_au = blocks_to_markdown(children, indent)
                md_lines.extend(child_md)
                transcript_lines.extend(child_tr)
                audio_urls.extend(child_au)

        elif btype == "table_row":
            cells = bdata.get("cells", [])
            row_texts = [extract_rich_text(cell) for cell in cells]
            md_lines.append(f"{prefix}| " + " | ".join(row_texts) + " |")

        elif btype == "transcription":
            # Notion AI Notes block. Contains summary, manual notes, and transcript
            # as three child paragraph wrappers whose IDs are listed in bdata["children"].
            meeting_title = extract_rich_text(bdata.get("title", []))
            child_ids = bdata.get("children", {})
            summary_id = child_ids.get("summary_block_id", "")
            notes_id = child_ids.get("notes_block_id", "")
            transcript_id = child_ids.get("transcript_block_id", "")

            if meeting_title:
                md_lines.append(f"{prefix}## {meeting_title}")
                md_lines.append("")

            recording = bdata.get("recording", {})
            if recording.get("start_time") and recording.get("end_time"):
                md_lines.append(
                    f"{prefix}*Recording: {recording['start_time']} → {recording['end_time']}*"
                )
                md_lines.append("")

            # Map each child wrapper block by its ID so we can label each section
            section_map = {
                summary_id:    ("### Summary",    False),
                notes_id:      ("### Notes",      False),
                transcript_id: ("### Transcript", True),  # True = collect as transcript
            }

            for child in children:
                child_id = child.get("id", "")
                label, is_transcript = section_map.get(child_id, (None, False))
                grandchildren = child.get("children", [])
                if not grandchildren:
                    continue
                gc_md, gc_tr, gc_au = blocks_to_markdown(grandchildren, indent)
                has_content = any(line.strip() for line in gc_md)
                if is_transcript:
                    # Content goes to transcript section at bottom — no heading here
                    transcript_lines.extend(line for line in gc_md if line.strip())
                elif has_content:
                    if label:
                        md_lines.append(f"{prefix}{label}")
                        md_lines.append("")
                    md_lines.extend(gc_md)
                # Empty sections are silently skipped
                transcript_lines.extend(gc_tr)
                audio_urls.extend(gc_au)

        # Unknown / unsupported block types are silently skipped.

    return md_lines, transcript_lines, audio_urls


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def sanitize_title(title: str) -> str:
    cleaned = FORBIDDEN_CHARS_RE.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Untitled"


def make_filename(title: str, created_time: str) -> str:
    sanitized = sanitize_title(title)
    if DATE_IN_TITLE_RE.search(title):
        return f"{sanitized}.md"
    # Prefix with date from created_time (ISO format: 2024-03-15T…)
    date_prefix = created_time[:10]  # YYYY-MM-DD
    return f"{date_prefix} {sanitized}.md"


# ---------------------------------------------------------------------------
# Page title extraction
# ---------------------------------------------------------------------------

def extract_page_title(page: dict) -> str:
    props = page.get("properties", {})
    # The title property can be named anything; find the one with type "title"
    for prop in props.values():
        if prop.get("type") == "title":
            rt = prop.get("title", [])
            if rt:
                return extract_rich_text(rt)
    return "Untitled"


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def build_markdown(
    page: dict,
    blocks: list,
    transcript_lines: list,
    audio_urls: list,
    vault_path: Path,
    dry_run: bool,
) -> tuple:
    """
    Returns (filename: str, full_content: str).
    """
    page_id = page["id"]
    title = extract_page_title(page)
    created = page.get("created_time", "")
    updated = page.get("last_edited_time", "")

    # YAML frontmatter
    frontmatter_lines = [
        "---",
        f"notion-id: {page_id}",
        f"title: {title}",
        f"created: {created}",
        f"updated: {updated}",
        "---",
        "",
    ]

    # Body
    md_lines, _, _ = blocks_to_markdown(blocks)

    parts = frontmatter_lines + md_lines

    # Transcript section
    if transcript_lines:
        parts.append("")
        parts.append("## Transcript")
        parts.append("")
        parts.extend(transcript_lines)

    # Recording section
    if audio_urls:
        parts.append("")
        parts.append("## Recording")
        parts.append("")
        for url in audio_urls:
            parsed = urllib.parse.urlparse(url)
            fname = Path(parsed.path).name or "recording"
            parts.append(f"[[{RECORDINGS_DIR}/{fname}]]")

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    filename = make_filename(title, created)
    return filename, content


# ---------------------------------------------------------------------------
# Vault index
# ---------------------------------------------------------------------------

def _parse_frontmatter_notion_id(file_path: Path) -> str | None:
    """Read only enough of the file to extract notion-id from YAML frontmatter.
    Returns the notion-id value, or None if not present."""
    try:
        with file_path.open(encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return None
            for _ in range(20):
                line = fh.readline()
                if not line or line.strip() == "---":
                    return None
                if line.startswith("notion-id:"):
                    return line.partition(":")[2].strip()
    except OSError:
        pass
    return None


def build_vault_index(vault_path: Path) -> dict:
    """Return {notion_id: file_path} for all .md files that have a notion-id,
    skipping any paths that contain an excluded directory."""
    index = {}
    for md_file in vault_path.rglob("*.md"):
        if any(part in EXCLUDED_DIRS for part in md_file.parts):
            continue
        notion_id = _parse_frontmatter_notion_id(md_file)
        if notion_id:
            index[notion_id] = md_file
    return index


# ---------------------------------------------------------------------------
# Vault writer
# ---------------------------------------------------------------------------

def write_page(
    vault_path: Path,
    filename: str,
    content: str,
    existing_path: Path | None,
    dry_run: bool,
) -> Path:
    # Never write into excluded directories
    if existing_path and any(part in EXCLUDED_DIRS for part in existing_path.parts):
        existing_path = None
    target = existing_path if existing_path else vault_path / filename

    if dry_run:
        action = "overwrite" if existing_path else "create"
        print(f"[dry-run] Would {action}: {target}")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(synced: int, ai_notes_count: int, errors: list, dry_run: bool):
    mode = "DRY RUN" if dry_run else "LIVE"
    print()
    print("Notion Sync Summary")
    print("-------------------")
    print(f"Mode: {mode}")
    print(f"Pages synced: {synced}")
    print(f"Pages with AI Notes / audio: {ai_notes_count}")
    print(f"Errors: {len(errors)}")
    for err in errors:
        print(f"  - {err}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Notion pages to an Obsidian vault."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Full re-sync: ignore last_sync checkpoint.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be synced without writing files.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    config = load_config()
    vault_path: Path = config["vault_path"]

    # Capture run start before any API calls (checkpoint consistency)
    run_start_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    since = DEFAULT_SINCE if args.all else read_last_sync(vault_path)
    print(f"Syncing pages edited after: {since}")

    client = NotionClient(config["token"])

    print("Building vault index…")
    vault_index = build_vault_index(vault_path)
    print(f"Found {len(vault_index)} already-synced page(s) in vault.")

    print("Fetching pages from Notion…")
    try:
        pages = client.search_pages(since)
    except requests.HTTPError as exc:
        print(f"Error fetching pages from Notion: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(pages)} page(s) to sync.")

    synced = 0
    ai_notes_count = 0
    errors = []

    for page in pages:
        page_id = page["id"]
        title = extract_page_title(page)
        print(f"  Processing: {title!r} ({page_id})")

        try:
            blocks = client.get_blocks(page_id)
            md_lines, transcript_lines, audio_urls = blocks_to_markdown(blocks)

            filename, content = build_markdown(
                page, blocks, transcript_lines, audio_urls, vault_path, args.dry_run
            )

            existing_path = vault_index.get(page_id)
            written_path = write_page(vault_path, filename, content, existing_path, args.dry_run)
            vault_index[page_id] = written_path

            # Handle audio downloads
            for url in audio_urls:
                parsed = urllib.parse.urlparse(url)
                audio_filename = Path(parsed.path).name or "recording"
                dest = vault_path / RECORDINGS_DIR / audio_filename
                client.download_audio(url, dest, args.dry_run)

            synced += 1
            if transcript_lines or audio_urls:
                ai_notes_count += 1

        except Exception as exc:  # noqa: BLE001
            err_msg = f"Page {page_id} ({title!r}): {exc}"
            print(f"  Error: {err_msg}", file=sys.stderr)
            errors.append(err_msg)

    write_last_sync(vault_path, run_start_utc, args.dry_run)
    print_summary(synced, ai_notes_count, errors, args.dry_run)


if __name__ == "__main__":
    main()
