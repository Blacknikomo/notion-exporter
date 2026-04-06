Task: Write a Python script notion_sync.py that syncs Notion pages to an Obsidian vault without any AI involvement.
Configuration (via .env or environment variables):

NOTION_TOKEN — Notion Integration Token
OBSIDIAN_VAULT_PATH — path to the vault

What the script does:

Reads .notion-sync/last_sync.json from the vault root. If missing, defaults to 2020-01-01.
Calls the Notion REST API (api.notion.com/v1) to find all pages edited after last_sync, using POST /v1/search with a last_edited_time filter.
For each page:

Fetches all blocks via GET /v1/blocks/{page_id}/children?recursive=true
Converts blocks to Markdown using a deterministic type map: heading_1/2/3 → #/##/###, paragraph, bulleted_list_item → - , numbered_list_item → 1., to_do → - [ ] / - [x], code → fenced code block, quote → >, divider → ---, callout → > **emoji** text, image → ![](url), child_page → [[title]]
Prepends Obsidian-compatible frontmatter:



yaml     ---
     notion-id: {page_id}
     title: {title}
     created: {created_time}
     updated: {last_edited_time}
     ---

Saves to the vault root. Filename: {YYYY-MM-DD} {sanitized_title}.md if the page title contains a date mention, otherwise {sanitized_title}.md. Sanitize by stripping / : * ? " < > | \.


For pages containing AI Notes (meeting notes blocks with transcript content):

Appends the transcript under a ## Transcript heading
If an audio file URL is present, downloads it via requests into recordings/
Adds [[recordings/filename.ext]] under a ## Recording heading


If a file already exists in the vault with a matching notion-id in its frontmatter — overwrite it fully.
After processing all pages, writes the current UTC timestamp to .notion-sync/last_sync.json.
Prints a summary: pages synced, pages with AI Notes/audio, any errors.

CLI:
bashpython notion_sync.py           # sync from last checkpoint
python notion_sync.py --all     # full re-sync (ignores last_sync)
python notion_sync.py --dry-run # print what would be synced, no writes
Dependencies: requests + stdlib only. No AI SDKs, no heavy frameworks