"""Convert Notion block trees to Markdown."""

from dataclasses import dataclass, field


@dataclass
class MeetingSection:
    """Structured content extracted from a Notion AI Notes (transcription) block."""
    meeting_title: str
    recording_start: str
    recording_end: str
    summary_lines: list[str] = field(default_factory=list)
    notes_lines: list[str] = field(default_factory=list)
    transcript_lines: list[str] = field(default_factory=list)


@dataclass
class ConversionResult:
    md_lines: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    meeting_sections: list[MeetingSection] = field(default_factory=list)


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
      - audio_urls: audio file URLs found in the tree
      - meeting_sections: structured MeetingSection objects for transcription blocks
    """
    result = ConversionResult()
    prefix = "  " * indent

    for block in blocks:
        btype = block.get("type", "")
        bdata = block.get(btype, {})
        children = block.get("children", [])

        if btype in ("heading_1", "heading_2", "heading_3"):
            _handle_heading(btype, bdata, prefix, result)
            continue

        if btype == "paragraph":
            _handle_paragraph(bdata, children, prefix, indent, False, result)
            continue

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


def _handle_paragraph(bdata, children, prefix, indent, _unused, result):
    text = extract_rich_text(bdata.get("rich_text", []))
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
        result.audio_urls.extend(child.audio_urls)


def _handle_transcription(bdata, children, prefix, indent, result):
    """Notion AI Notes block → produces a MeetingSection (not inline md)."""
    recording = bdata.get("recording", {})
    section = MeetingSection(
        meeting_title=extract_rich_text(bdata.get("title", [])),
        recording_start=recording.get("start_time", ""),
        recording_end=recording.get("end_time", ""),
    )

    child_ids = bdata.get("children", {})
    section_map = {
        child_ids.get("summary_block_id"):    "summary",
        child_ids.get("notes_block_id"):      "notes",
        child_ids.get("transcript_block_id"): "transcript",
    }
    section_map.pop(None, None)

    for child in children:
        dest = section_map.get(child.get("id"))
        grandchildren = child.get("children", [])
        if not dest or not grandchildren:
            continue
        gc = blocks_to_markdown(grandchildren, 0)
        lines = [line for line in gc.md_lines if line.strip()]
        if dest == "summary":
            section.summary_lines = gc.md_lines
        elif dest == "notes":
            section.notes_lines = gc.md_lines
        elif dest == "transcript":
            section.transcript_lines = lines
        result.audio_urls.extend(gc.audio_urls)

    result.meeting_sections.append(section)


def _merge_child_result(child: ConversionResult, parent: ConversionResult):
    parent.md_lines.extend(child.md_lines)
    parent.audio_urls.extend(child.audio_urls)
    parent.meeting_sections.extend(child.meeting_sections)
