"""Obsidian vault operations: sync state, file index, page writing."""

import json
import re
import urllib.parse
from pathlib import Path

from .converter import ConversionResult, blocks_to_markdown, extract_rich_text

# Default earliest date to sync from (used when no checkpoint exists)
DEFAULT_SINCE = "2026-03-25T00:00:00.000Z"

SYNC_STATE_REL = ".notion-sync/last_sync.json"
RECORDINGS_DIR = "recordings"

# Folders excluded from vault scanning and writes (like .gitignore entries)
EXCLUDED_DIRS: frozenset[str] = frozenset({"Notion backup"})

_FORBIDDEN_CHARS = re.compile(r'[/:*?"<>|\\]')
_DATE_IN_TITLE = re.compile(
    r'\b(\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b'
)


# ---------------------------------------------------------------------------
# Sync state (checkpoint)
# ---------------------------------------------------------------------------

def read_last_sync(vault_path: Path) -> str:
    state_file = vault_path / SYNC_STATE_REL
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data.get("last_sync", DEFAULT_SINCE)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_SINCE


def write_last_sync(vault_path: Path, timestamp: str, dry_run: bool):
    state_file = vault_path / SYNC_STATE_REL
    if dry_run:
        print(f"[dry-run] Would write last_sync: {timestamp} → {state_file}")
        return
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_sync": timestamp}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def sanitize_title(title: str) -> str:
    cleaned = _FORBIDDEN_CHARS.sub("", title)
    return re.sub(r"\s+", " ", cleaned).strip() or "Untitled"


def make_filename(title: str, created_time: str) -> str:
    """Return the vault filename for a page.

    Prefixes with YYYY-MM-DD (from created_time) unless the title already
    contains a date pattern.
    """
    sanitized = sanitize_title(title)
    if _DATE_IN_TITLE.search(title):
        return f"{sanitized}.md"
    return f"{created_time[:10]} {sanitized}.md"


# ---------------------------------------------------------------------------
# Page → Markdown document
# ---------------------------------------------------------------------------

def extract_page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            rt = prop.get("title", [])
            if rt:
                return extract_rich_text(rt)
    return "Untitled"


def build_page_document(page: dict, blocks: list) -> tuple[str, str]:
    """Convert a Notion page and its blocks into a (filename, markdown) pair.

    The markdown document contains:
      - YAML frontmatter (notion-id, title, created, updated)
      - Body converted from blocks
      - Optional ## Transcript section
      - Optional ## Recording section with Obsidian wikilinks
    """
    page_id = page["id"]
    title = extract_page_title(page)
    created = page.get("created_time", "")
    updated = page.get("last_edited_time", "")

    conversion = blocks_to_markdown(blocks)

    parts = [
        "---",
        f"notion-id: {page_id}",
        f"title: {title}",
        f"created: {created}",
        f"updated: {updated}",
        "---",
        "",
        *conversion.md_lines,
    ]

    if conversion.transcript_lines:
        parts += ["", "## Transcript", "", *conversion.transcript_lines]

    if conversion.audio_urls:
        parts.append("")
        parts.append("## Recording")
        parts.append("")
        for url in conversion.audio_urls:
            fname = Path(urllib.parse.urlparse(url).path).name or "recording"
            parts.append(f"[[{RECORDINGS_DIR}/{fname}]]")

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    return make_filename(title, created), content


# ---------------------------------------------------------------------------
# Vault index
# ---------------------------------------------------------------------------

def _read_notion_id(file_path: Path) -> str | None:
    """Read only enough of a file to extract notion-id from its frontmatter."""
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


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def build_vault_index(vault_path: Path) -> dict[str, Path]:
    """Return {notion_id: file_path} for all synced .md files in the vault."""
    index = {}
    for md_file in vault_path.rglob("*.md"):
        if _is_excluded(md_file):
            continue
        notion_id = _read_notion_id(md_file)
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
    """Write page content to the vault.

    If existing_path is given (and not in an excluded dir), overwrites it in
    place so renamed pages don't leave stale files behind.
    """
    if existing_path and _is_excluded(existing_path):
        existing_path = None

    target = existing_path or vault_path / filename

    if dry_run:
        action = "overwrite" if existing_path else "create"
        print(f"[dry-run] Would {action}: {target}")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
