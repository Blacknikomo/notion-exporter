"""Convert Notion block trees to Markdown."""

from dataclasses import dataclass, field


@dataclass
class ConversionResult:
    md_lines: list[str] = field(default_factory=list)
    transcript_lines: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)


def extract_rich_text(rich_text_list: list) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text_list)


def _file_url(block_data: dict) -> str:
    return (
        block_data.get("file", {}).get("url", "")
        or block_data.get("external", {}).get("url", "")
    )


def blocks_to_markdown(blocks: list, indent: int = 0) -> ConversionResult:
    """Recursively convert a list of Notion block dicts to Markdown.

    Returns a ConversionResult with:
      - md_lines: body markdown lines
      - transcript_lines: plain text lines collected from transcript sections
      - audio_urls: audio file URLs found in the tree
    """
    result = ConversionResult()
    prefix = "  " * indent
    capturing_transcript = False

    for block in blocks:
        btype = block.get("type", "")
        bdata = block.get(btype, {})
        children = block.get("children", [])

        if btype in ("heading_1", "heading_2", "heading_3"):
            _handle_heading(btype, bdata, prefix, result)
            text = extract_rich_text(bdata.get("rich_text", []))
            capturing_transcript = text.strip().lower() == "transcript"
            continue

        if btype == "paragraph":
            _handle_paragraph(bdata, children, prefix, indent, capturing_transcript, result)
            continue

        # Any non-paragraph/heading block ends a transcript-capture streak
        capturing_transcript = False

        if btype == "bulleted_list_item":
            _handle_list_item(bdata, children, prefix, "- ", indent, result)
        elif btype == "numbered_list_item":
            _handle_list_item(bdata, children, prefix, "1. ", indent, result)
        elif btype == "to_do":
            _handle_todo(bdata, children, prefix, indent, result)
        elif btype == "code":
            _handle_code(bdata, prefix, result)
        elif btype == "quote":
            _handle_quote(bdata, children, prefix, indent, result)
        elif btype == "divider":
            result.md_lines += [f"{prefix}---", ""]
        elif btype == "callout":
            _handle_callout(bdata, children, prefix, indent, result)
        elif btype == "image":
            caption = extract_rich_text(bdata.get("caption", []))
            result.md_lines += [f"{prefix}![{caption}]({_file_url(bdata)})", ""]
        elif btype == "audio":
            url = _file_url(bdata)
            if url:
                result.audio_urls.append(url)
        elif btype == "child_page":
            result.md_lines += [f"{prefix}[[{bdata.get('title', '')}]]", ""]
        elif btype == "toggle":
            _handle_list_item(bdata, children, prefix, "- ", indent, result)
        elif btype == "table":
            _merge_child_result(blocks_to_markdown(children, indent), result)
        elif btype == "table_row":
            cells = bdata.get("cells", [])
            result.md_lines.append(prefix + "| " + " | ".join(extract_rich_text(c) for c in cells) + " |")
        elif btype == "transcription":
            _handle_transcription(bdata, children, prefix, indent, result)

    return result


# ---------------------------------------------------------------------------
# Per-block handlers
# ---------------------------------------------------------------------------

def _handle_heading(btype: str, bdata: dict, prefix: str, result: ConversionResult):
    level = {"heading_1": 1, "heading_2": 2, "heading_3": 3}[btype]
    text = extract_rich_text(bdata.get("rich_text", []))
    result.md_lines += [f"{prefix}{'#' * level} {text}", ""]


def _handle_paragraph(bdata, children, prefix, indent, capturing_transcript, result):
    text = extract_rich_text(bdata.get("rich_text", []))
    if capturing_transcript and text:
        result.transcript_lines.append(text)
    else:
        result.md_lines += [f"{prefix}{text}", ""]
    if children:
        _merge_child_result(blocks_to_markdown(children, indent + 1), result)


def _handle_list_item(bdata, children, prefix, marker, indent, result):
    text = extract_rich_text(bdata.get("rich_text", []))
    result.md_lines.append(f"{prefix}{marker}{text}")
    if children:
        _merge_child_result(blocks_to_markdown(children, indent + 1), result)


def _handle_todo(bdata, children, prefix, indent, result):
    text = extract_rich_text(bdata.get("rich_text", []))
    box = "[x]" if bdata.get("checked") else "[ ]"
    result.md_lines.append(f"{prefix}- {box} {text}")
    if children:
        _merge_child_result(blocks_to_markdown(children, indent + 1), result)


def _handle_code(bdata, prefix, result):
    text = extract_rich_text(bdata.get("rich_text", []))
    lang = bdata.get("language", "")
    result.md_lines.append(f"{prefix}```{lang}")
    result.md_lines.extend(f"{prefix}{line}" for line in text.splitlines())
    result.md_lines += [f"{prefix}```", ""]


def _handle_quote(bdata, children, prefix, indent, result):
    text = extract_rich_text(bdata.get("rich_text", []))
    for line in text.splitlines() or [""]:
        result.md_lines.append(f"{prefix}> {line}")
    result.md_lines.append("")
    if children:
        child = blocks_to_markdown(children, indent + 1)
        result.md_lines.extend(f"> {line}" for line in child.md_lines)
        result.transcript_lines.extend(child.transcript_lines)
        result.audio_urls.extend(child.audio_urls)


def _handle_callout(bdata, children, prefix, indent, result):
    text = extract_rich_text(bdata.get("rich_text", []))
    icon = bdata.get("icon", {})
    emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
    body = f"**{emoji}** {text}" if emoji else text
    result.md_lines += [f"{prefix}> {body}", ""]
    if children:
        child = blocks_to_markdown(children, indent + 1)
        result.md_lines.extend(f"> {line}" for line in child.md_lines)
        result.transcript_lines.extend(child.transcript_lines)
        result.audio_urls.extend(child.audio_urls)


def _handle_transcription(bdata, children, prefix, indent, result):
    """Notion AI Notes block: meeting title, recording time, summary, notes, transcript."""
    meeting_title = extract_rich_text(bdata.get("title", []))
    if meeting_title:
        result.md_lines += [f"{prefix}## {meeting_title}", ""]

    recording = bdata.get("recording", {})
    if recording.get("start_time") and recording.get("end_time"):
        result.md_lines += [
            f"{prefix}*Recording: {recording['start_time']} → {recording['end_time']}*",
            "",
        ]

    child_ids = bdata.get("children", {})
    section_map = {
        child_ids.get("summary_block_id"):    ("### Summary",    False),
        child_ids.get("notes_block_id"):      ("### Notes",      False),
        child_ids.get("transcript_block_id"): ("### Transcript", True),
    }
    section_map.pop(None, None)  # remove any keys that were missing

    for child in children:
        label, is_transcript = section_map.get(child.get("id"), (None, False))
        grandchildren = child.get("children", [])
        if not grandchildren:
            continue
        gc = blocks_to_markdown(grandchildren, indent)
        has_body = any(line.strip() for line in gc.md_lines)
        if is_transcript:
            result.transcript_lines.extend(line for line in gc.md_lines if line.strip())
        elif has_body:
            if label:
                result.md_lines += [f"{prefix}{label}", ""]
            result.md_lines.extend(gc.md_lines)
        result.transcript_lines.extend(gc.transcript_lines)
        result.audio_urls.extend(gc.audio_urls)


def _merge_child_result(child: ConversionResult, parent: ConversionResult):
    parent.md_lines.extend(child.md_lines)
    parent.transcript_lines.extend(child.transcript_lines)
    parent.audio_urls.extend(child.audio_urls)
