# Notion Exporter

Syncs Notion pages to an [Obsidian](https://obsidian.md) vault as Markdown files. Supports regular pages and Notion AI Notes (meeting transcriptions), and downloads audio recordings.

## How it works

1. **Reads a checkpoint** from `.notion-sync/last_sync.json` in your vault to know which pages have changed since the last run.
2. **Queries the Notion API** for all pages edited after the checkpoint timestamp.
3. **Converts each page** to Markdown:
   - Regular pages → a single `.md` file with YAML frontmatter (`notion-id`, `title`, `created`, `updated`). The filename is prefixed with the creation date unless the title already contains a date.
   - Pages with **AI Notes** (Notion transcription blocks) → a three-file folder under `Meeting Notes/<page title>/`:
     - `Summary.md` — AI-generated summary, carries the `notion-id` used for deduplication
     - `Notes.md` — manual notes section
     - `Transcribing.md` — full transcript
     - Each file has Obsidian wikilinks to the other two for easy navigation.
4. **Downloads audio recordings** linked in pages to `recordings/` inside the vault.
5. **Updates the checkpoint** so the next run only fetches pages edited since this run started.

Pages already in the vault are overwritten in-place (matched by `notion-id` frontmatter), so renaming a page in Notion won't leave stale files behind. The `Notion backup` folder is excluded from scanning and writes.

## Setup

Copy `.env.example` to `.env` and fill in your values:

```
NOTION_TOKEN=ntn....
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

`NOTION_TOKEN` — a Notion Integration Token with read access to the pages you want to sync.  
`OBSIDIAN_VAULT_PATH` — absolute path to the root of your Obsidian vault.

Environment variables take precedence over `.env` values.

Install dependencies (none beyond the standard library and `requests`):

```bash
pip install requests
```

## Usage

```bash
python3 notion_sync.py [--all] [--dry-run]
```

### Arguments

| Argument    | Description |
|-------------|-------------|
| _(none)_    | Incremental sync — only pages edited after the last checkpoint are processed. |
| `--all`     | Full re-sync — ignores the checkpoint and processes all pages from the default start date. |
| `--dry-run` | Preview mode — prints what would be created or overwritten without writing any files or updating the checkpoint. |

### Examples

```bash
# Normal incremental sync
python3 notion_sync.py

# Re-sync everything from scratch
python3 notion_sync.py --all

# See what would change without touching the vault
python3 notion_sync.py --dry-run

# Preview a full re-sync
python3 notion_sync.py --all --dry-run
```

## Vault structure

```
<vault>/
├── .notion-sync/
│   └── last_sync.json          # sync checkpoint
├── Meeting Notes/
│   └── <Meeting Title>/
│       ├── Summary.md
│       ├── Notes.md
│       └── Transcribing.md
├── recordings/
│   └── <audio files>
└── <YYYY-MM-DD Page Title>.md  # regular pages
```
