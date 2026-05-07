"""Seed Oracle SuiteCloud SDK agent-skill markdown into RAG partitions.

Reads .md files from .claude/skills/netsuite-*/ (vendored from oracle/netsuite-suitecloud-sdk
via PR #74), chunks by H2/H3 sections with a 1500-token sub-split cap, embeds via the
existing OpenAI text-embedding-3-small pipeline, and writes DomainKnowledgeChunk rows
under per-skill partition_ids (oracle/<slug>).

Idempotent — deletes existing oracle/<slug> chunks before re-seeding.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_TOKENS_DEFAULT = 1500
_HEADER_RE = re.compile(r"^(?:## |### )", re.MULTILINE)


def _estimate_tokens(text: str) -> int:
    """Match the chars/4 heuristic used by bigquery_schema_seeder."""
    return len(text) // 4


def chunk_markdown(text: str, max_tokens: int = _MAX_TOKENS_DEFAULT) -> list[str]:
    """Chunk markdown by H2/H3 headers, sub-splitting sections that exceed max_tokens.

    Section starts at any line beginning with `## ` or `### `. Sub-split walks
    paragraphs (`\\n\\n`) inside oversized sections; if a single paragraph still
    exceeds cap, hard-split at character boundaries that preserve code fences.
    """
    if not text.strip():
        return []

    sections = _split_by_headers(text)
    chunks: list[str] = []
    for section in sections:
        if _estimate_tokens(section) <= max_tokens:
            chunks.append(section.rstrip())
        else:
            chunks.extend(_subsplit(section, max_tokens))
    return [c for c in chunks if c.strip()]


def _split_by_headers(text: str) -> list[str]:
    """Split on H2/H3 boundaries, keeping the header with its body."""
    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return [text]
    sections: list[str] = []
    if matches[0].start() > 0:
        sections.append(text[: matches[0].start()])
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[m.start() : end])
    return sections


def _subsplit(section: str, max_tokens: int) -> list[str]:
    """Sub-split an oversized section on paragraph boundaries, fence-aware."""
    paragraphs = section.split("\n\n")
    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    in_fence = False

    for para in paragraphs:
        # Track ``` parity across paragraphs to avoid flushing mid-fence.
        fence_count = para.count("```")
        para_tokens = _estimate_tokens(para)

        if buf and buf_tokens + para_tokens > max_tokens and not in_fence:
            chunks.append("\n\n".join(buf).rstrip())
            buf = []
            buf_tokens = 0

        buf.append(para)
        buf_tokens += para_tokens
        if fence_count % 2 == 1:
            in_fence = not in_fence

    if buf:
        chunks.append("\n\n".join(buf).rstrip())

    final: list[str] = []
    for chunk in chunks:
        if _estimate_tokens(chunk) <= max_tokens:
            final.append(chunk)
        elif chunk.count("```") > 0:
            # Chunk contains code fence markers; character-boundary slicing would cut
            # between opening and closing backticks, producing unbalanced fences.
            # Emit as-is and accept the token-cap violation — fence correctness wins.
            final.append(chunk)
        else:
            char_cap = max_tokens * 4
            for i in range(0, len(chunk), char_cap):
                final.append(chunk[i : i + char_cap])
    return final


# Vendored skill name → partition slug. Locked in spec; do not rename without a migration.
SLUG_MAP: dict[str, str] = {
    "netsuite-ai-connector-instructions": "oracle/ai-connector",
    "netsuite-owasp-secure-coding": "oracle/owasp",
    "netsuite-sdf-project-documentation": "oracle/sdf-docs",
    "netsuite-sdf-roles-and-permissions": "oracle/sdf-roles",
    "netsuite-suitescript-records-reference": "oracle/records",
    "netsuite-suitescript-upgrade": "oracle/upgrade",
    "netsuite-uif-spa-reference": "oracle/uif-spa",
}


def walk_oracle_skills(root: Path | str) -> Iterator[tuple[str, Path, str]]:
    """Yield (partition_slug, file_path, markdown_text) for every .md under .claude/skills/netsuite-*/.

    Skips .json, .d.ts, and any non-markdown file. Logs and skips missing skill dirs.
    Raises FileNotFoundError if NO expected skill dir exists (signals a fresh checkout
    that never ran scripts/refresh-oracle-skills.sh).
    """
    root = Path(root)
    skills_root = root / ".claude" / "skills"
    found_any = False

    for skill_name, slug in SLUG_MAP.items():
        skill_dir = skills_root / skill_name
        if not skill_dir.is_dir():
            logger.warning("Skill dir missing, skipping: %s", skill_dir)
            continue
        found_any = True
        for md_path in sorted(skill_dir.rglob("*.md")):
            try:
                content = md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                logger.warning("Failed to read %s", md_path, exc_info=True)
                continue
            yield slug, md_path, content

    if not found_any:
        raise FileNotFoundError(
            f"No Oracle skills found under {skills_root}. "
            "run scripts/refresh-oracle-skills.sh to vendor them first."
        )
