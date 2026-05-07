"""Seed Oracle SuiteCloud SDK agent-skill markdown into RAG partitions.

Reads .md files from .claude/skills/netsuite-*/ (vendored from oracle/netsuite-suitecloud-sdk
via PR #74), chunks by H2/H3 sections with a 1500-token sub-split cap, embeds via the
existing OpenAI text-embedding-3-small pipeline, and writes DomainKnowledgeChunk rows
under per-skill partition_ids (oracle/<slug>).

Idempotent — deletes existing oracle/<slug> chunks before re-seeding.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete

from app.models.domain_knowledge import DomainKnowledgeChunk
from app.services.chat.domain_knowledge import embed_domain_texts

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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


def _records_json_to_synthetic_markdown(json_path: Path) -> Iterator[tuple[Path, str]]:
    """Convert records.json to one synthetic markdown chunk per NetSuite record type.

    The records reference skill ships its actual data as JSON, not markdown. This helper
    emits one synthetic markdown document per record so the chunker (text-based) can
    embed it. Each yielded path is a synthetic Path derived from the record's script ID
    (e.g. records.json#salesorder -> Path("records.json/salesorder.md")) for traceability
    in source_uri.

    Yields (synthetic_path, markdown_text) tuples.
    """
    try:
        data = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to parse %s", json_path, exc_info=True)
        return
    records = data.get("records") or {}
    for script_id, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        lines = [f"## Record: {script_id}", ""]
        record_name = record.get("recordName") or record.get("displayName")
        if record_name:
            lines.append(f"Display name: {record_name}")
        for key in ("internalId", "recordCategory", "scriptingLevel", "supportsCustomFields"):
            if key in record:
                lines.append(f"{key}: {record[key]}")
        lines.append("")

        fields = record.get("fields") or []
        if isinstance(fields, list) and fields:
            lines.append("### Body Fields")
            lines.append("")
            for f in fields:
                if not isinstance(f, dict):
                    continue
                fid = f.get("id") or f.get("scriptId") or "?"
                ftype = f.get("type") or "?"
                label = f.get("label") or ""
                req = " (required)" if f.get("required") else ""
                lines.append(f"- `{fid}` ({ftype}){req}{(': ' + label) if label else ''}")
            lines.append("")

        sublists = record.get("sublists") or []
        if isinstance(sublists, list) and sublists:
            lines.append("### Sublists")
            lines.append("")
            for s in sublists:
                if not isinstance(s, dict):
                    continue
                sid = s.get("id") or s.get("scriptId") or "?"
                slabel = s.get("label") or ""
                lines.append(f"- `{sid}`{(': ' + slabel) if slabel else ''}")
            lines.append("")

        synthetic_path = json_path.parent / f"{json_path.stem}__{script_id}.md"
        yield synthetic_path, "\n".join(lines)


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

        # The records-reference skill ships field data as JSON, not markdown.
        # Emit one synthetic markdown chunk per record type so the chunker can embed it.
        if skill_name == "netsuite-suitescript-records-reference":
            json_path = skill_dir / "references" / "records.json"
            if json_path.is_file():
                for synthetic_path, synthetic_content in _records_json_to_synthetic_markdown(
                    json_path
                ):
                    yield slug, synthetic_path, synthetic_content

    if not found_any:
        raise FileNotFoundError(
            f"No Oracle skills found under {skills_root}. "
            "run scripts/refresh-oracle-skills.sh to vendor them first."
        )


async def seed_all_oracle_skills(
    db: AsyncSession,
    root: Path | str | None = None,
) -> int:
    """Seed all 7 Oracle skill partitions into the RAG store.

    Walks .claude/skills/netsuite-*/, chunks each markdown file, embeds the
    chunks via OpenAI text-embedding-3-small, and writes DomainKnowledgeChunk
    rows. Idempotent — deletes all existing oracle/* partition rows via an
    explicit IN clause before re-inserting.

    Args:
        db: Async DB session. Caller is responsible for committing.
        root: Repo root directory. Defaults to the repo root derived from this
              file's location (``Path(__file__).resolve().parents[3]``).

    Returns:
        Total number of chunks written across all partitions.
    """
    if root is None:
        # oracle_skill_seeder.py is at backend/app/services/oracle_skill_seeder.py
        # parents[0] = services/, [1] = app/, [2] = backend/, [3] = repo root
        root = Path(__file__).resolve().parents[3]

    root = Path(root)

    # Collect all chunks grouped by partition_id.
    partition_chunks: dict[str, list[tuple[Path, str]]] = {
        slug: [] for slug in SLUG_MAP.values()
    }
    for slug, md_path, content in walk_oracle_skills(root):
        for chunk_text in chunk_markdown(content):
            partition_chunks[slug].append((md_path, chunk_text))

    # Guard: if every skill file was empty / whitespace-only, chunk_markdown returns []
    # for each file, leaving all partition lists empty. Deleting without re-inserting
    # would silently wipe the RAG store — bail out instead.
    all_chunks = [c for chunks in partition_chunks.values() for c in chunks]
    if not all_chunks:
        logger.warning("[ORACLE_SEEDER] No chunks found — aborting to avoid empty re-seed.")
        return 0

    # Idempotent delete — explicit IN list, not LIKE.
    partition_ids = list(SLUG_MAP.values())
    await db.execute(
        delete(DomainKnowledgeChunk).where(
            DomainKnowledgeChunk.partition_id.in_(partition_ids)
        )
    )

    total = 0
    for partition_id, file_chunks in partition_chunks.items():
        if not file_chunks:
            continue

        # Embed the whole partition in a single batch.
        texts = [chunk_text for _, chunk_text in file_chunks]
        embeddings = await embed_domain_texts(texts)
        if embeddings is None:
            logger.warning(
                "[ORACLE_SEEDER] embed_domain_texts returned None for partition %s "
                "— writing rows without embeddings",
                partition_id,
            )

        for idx, (md_path, chunk_text) in enumerate(file_chunks):
            vec = embeddings[idx] if embeddings is not None else None
            db.add(
                DomainKnowledgeChunk(
                    source_uri=f"{partition_id}/{md_path.name}#chunk-{idx}",
                    chunk_index=idx,
                    raw_text=chunk_text,
                    token_count=len(chunk_text) // 4,
                    source_type="oracle_skill",
                    partition_id=partition_id,
                    embedding=vec,
                    is_deprecated=False,
                )
            )
            total += 1

    await db.flush()
    print(
        f"[ORACLE_SEEDER] Seeded {total} chunks across {len(partition_ids)} partitions",
        flush=True,
    )
    return total
