import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.notion.com/v1"
API_VERSION = "2022-06-28"


class NotionClient:
    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": API_VERSION,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None, timeout: int = 60) -> dict:
        resp = self._session.get(f"{API_BASE}{path}", params=params, timeout=timeout)
        self._raise_for_status(resp)
        return resp.json()

    def _post(self, path: str, body: dict = None, timeout: int = 30) -> dict:
        resp = self._session.post(f"{API_BASE}{path}", json=body or {}, timeout=timeout)
        self._raise_for_status(resp)
        return resp.json()

    def _raise_for_status(self, resp: requests.Response):
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 1))
            print(f"Rate limited. Waiting {retry_after}s…")
            time.sleep(retry_after)
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_pages(self, since: str) -> list[dict]:
        """Return pages with last_edited_time > since, newest first.

        Stops paginating as soon as a page older than `since` is encountered
        (the API returns results in descending order).
        """
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
            for page in data.get("results", []):
                if page.get("last_edited_time", "") <= since:
                    return pages
                pages.append(page)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return pages

    def get_blocks(self, block_id: str, _depth: int = 0) -> list[dict]:
        """Fetch all direct children of a block, recursing up to depth 5.

        Attaches fetched children as block["children"] on each block dict.
        child_page blocks are not recursed into — they are separate pages.
        """
        blocks = []
        cursor = None

        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor

            data = self._get(f"/blocks/{block_id}/children", params)
            for block in data.get("results", []):
                block["children"] = self._fetch_children(block, _depth)
                blocks.append(block)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return blocks

    def _fetch_children(self, block: dict, parent_depth: int) -> list[dict]:
        if not block.get("has_children"):
            return []
        if parent_depth >= 5:
            return []
        if block.get("type") == "child_page":
            return []
        try:
            return self.get_blocks(block["id"], parent_depth + 1)
        except requests.HTTPError as exc:
            print(f"  Warning: could not fetch children of block {block['id']}: {exc}")
            return []

    def download_file(self, url: str, dest_path: Path) -> bool:
        """Stream-download a file to dest_path. Returns True on success."""
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with self._session.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with dest_path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)
            return True
        except (requests.RequestException, OSError) as exc:
            print(f"  Warning: failed to download {url}: {exc}", file=sys.stderr)
            return False
