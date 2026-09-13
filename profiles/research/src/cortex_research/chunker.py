"""Chunk full_text.md files by Markdown section structure."""

from __future__ import annotations

import re
from pathlib import Path

# Sections to skip entirely
SKIP_SECTIONS = re.compile(
    r"(references|bibliography|acknowledgment|appendix)",
    re.IGNORECASE,
)


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (--- delimited block at start of file)."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3 :].lstrip("\n")
    return text


def chunk_paper(
    full_text_path: Path, title: str, max_tokens: int = 1024
) -> list[dict]:
    """Split a full_text.md into chunks by section headers.

    Returns list of {chunk_id, text, section, chunk_idx, paper_dir}.
    """
    text = _strip_frontmatter(full_text_path.read_text(encoding="utf-8"))
    sections = _split_by_headers(text)
    paper_dir = full_text_path.parent.name

    chunks = []
    for section_name, section_text in sections:
        # Skip reference/bibliography sections
        if SKIP_SECTIONS.search(section_name):
            continue
        # Skip sections that are mostly LaTeX
        if _is_mostly_latex(section_text):
            continue

        sub_chunks = _split_oversized(section_text, max_tokens)
        for idx, sub_text in enumerate(sub_chunks):
            chunk_id = f"{paper_dir}::{section_name}::{idx}"
            prefixed = f"Paper: {title} | Section: {section_name}\n\n{sub_text}"
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "text": prefixed,
                    "section": section_name,
                    "chunk_idx": idx,
                    "paper_dir": paper_dir,
                }
            )
    return chunks


def _split_by_headers(text: str) -> list[tuple[str, str]]:
    """Split markdown by ## or ### headers. Preserves text before first header as Abstract."""
    pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))

    if not matches:
        return [("Full Text", text)]

    sections = []
    # Capture text before first header (usually abstract/introduction)
    preamble = text[: matches[0].start()].strip()
    if preamble and len(preamble) > 100:
        sections.append(("Abstract", preamble))

    for i, match in enumerate(matches):
        name = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((name, body))
    return sections


def _is_mostly_latex(text: str) -> bool:
    """Return True if >70% of content is LaTeX formulas."""
    stripped = text.strip()
    if not stripped:
        return True
    # Count chars inside $...$ or $$...$$ blocks
    formula_chars = sum(
        len(m.group()) for m in re.finditer(r"\$\$?[^$]+\$\$?", stripped)
    )
    return formula_chars / len(stripped) > 0.7


def _split_oversized(text: str, max_tokens: int) -> list[str]:
    """Split text by paragraphs if it exceeds max_tokens.
    Uses a rough 1 token ~ 4 chars estimate for speed.
    """
    char_limit = max_tokens * 4
    if len(text) <= char_limit:
        return [text]

    paragraphs = re.split(r"\n\n+", text)
    chunks = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > char_limit and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks
