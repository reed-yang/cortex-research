"""Parse notes.md files into structured catalog entries.

Handles 10+ heading format variants found across 134 papers.
Uses fuzzy heading matching to maximize coverage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Patterns for fuzzy heading detection
_KW_HEADING = re.compile(
    r"^(?:#{2,5}\s+.*(?:keyword|关键).*|(?:\*\*)+\s*(?:keyword|关键).*(?:\*\*)+)",
    re.IGNORECASE,
)
_SUMMARY_HEADING = re.compile(
    r"^#{1,4}\s+.*(?:summary|总结|摘要|takeaway)",
    re.IGNORECASE,
)
_ANY_HEADING = re.compile(r"^#{1,5}\s+")


def parse_notes(notes_path: Path) -> dict | None:
    """Parse a single notes.md into a catalog entry."""
    text = notes_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    dir_name = notes_path.parent.name

    entry = {
        "title": _extract_title(lines, dir_name),
        "date": _extract_date(lines, dir_name),
        "keywords": _extract_keywords(lines),
        "summary": _extract_summary(lines),
        "projects": _extract_projects(text),
        "has_embedding": False,
    }
    return entry


def _extract_title(lines: list[str], dir_name: str) -> str:
    for line in lines:
        if line.startswith("# Notes:"):
            return line.replace("# Notes:", "").strip()
        if line.startswith("# Paper Summary:"):
            return line.replace("# Paper Summary:", "").strip()
    # Fallback: derive from directory name (strip date prefix, replace underscores)
    parts = dir_name.split("-", 1)
    if len(parts) == 2:
        return parts[1].replace("_", " ")
    return dir_name


def _extract_date(lines: list[str], dir_name: str) -> str:
    for line in lines:
        m = re.match(r"^###\s+(\d{4}-\d{2}-\d{2})", line)
        if m:
            return m.group(1)
    # Fallback: extract from directory name prefix YYYYMMDD
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-", dir_name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _extract_keywords(lines: list[str]) -> list[str]:
    """Handle all keyword heading variants and content formats."""
    in_section = False
    keywords = []
    for line in lines:
        # Detect any keywords-like heading
        if _KW_HEADING.match(line):
            in_section = True
            continue
        # Stop at next heading (that isn't also a keyword heading)
        if in_section and _ANY_HEADING.match(line) and not _KW_HEADING.match(line):
            break
        if in_section:
            # Bullet list format: "- keyword" or "* keyword"
            bullet = re.match(r"^[-*]\s+(.+)", line)
            if bullet:
                kw = bullet.group(1).strip().strip("`").strip()
                if kw:
                    keywords.append(kw)
                continue
            # Inline format: `kw1`, `kw2`, kw3, kw4
            if line.strip():
                for part in re.split(r"[,，]\s*", line.strip()):
                    kw = part.strip().strip("`").strip()
                    if kw:
                        keywords.append(kw)
    return keywords


def _extract_summary(lines: list[str]) -> str:
    """Extract first ~300 chars after any summary-like heading."""
    in_section = False
    text_parts = []
    for line in lines:
        if _SUMMARY_HEADING.match(line):
            in_section = True
            continue
        if in_section and _ANY_HEADING.match(line):
            break
        if in_section and line.strip():
            text_parts.append(line.strip())
    summary = " ".join(text_parts)
    if summary:
        return summary[:300]
    # Fallback: take first substantial paragraph (skip headings and short lines)
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and len(stripped) > 50:
            return stripped[:300]
    return ""


def _extract_projects(text: str) -> list[str]:
    return re.findall(r"\[\[projects/([^\]|]+)", text)
