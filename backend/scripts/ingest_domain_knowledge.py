"""Ingest curated golden dataset markdown files into domain_knowledge_chunks.

Code-aware chunking: splits at H2/H3 boundaries, never severs SQL code blocks
from their preceding text, prepends H1 title to each chunk for embedding context.

Usage:
    cd backend && python -m scripts.ingest_domain_knowledge
    cd backend && python -m scripts.ingest_domain_knowledge --dir ../knowledge/golden_dataset
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import async_session_factory
from app.models.domain_knowledge import DomainKnowledgeChunk
from app.services.chat.domain_knowledge import embed_domain_texts

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "golden_dataset"
EMBED_BATCH_SIZE = 20
CHUNK_MAX_TOKENS = 600  # Hard ceiling before splitting within a section
CHUNK_TARGET_TOKENS = 400


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from markdown content.

    Returns (frontmatter_dict, remaining_content).
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    remaining = content[match.end():]
    return fm, remaining


def extract_h1(content: str) -> str | None:
    """Extract the first H1 heading from content."""
    match = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else None


def chunk_markdown(content: str, source_uri: str) -> list[dict]:
    """Split markdown into chunks at H2/H3 boundaries.

    Rules:
    1. Never sever ```sql code blocks from their preceding paragraph
    2. Prepend H1 title to each chunk for embedding context
    3. Target 300-500 tokens per chunk
    4. If a section exceeds CHUNK_MAX_TOKENS, split at paragraph boundaries
    """
    frontmatter, body = parse_frontmatter(content)
    h1_title = extract_h1(body) or Path(source_uri).stem.replace("-", " ").title()
    topic_tags = frontmatter.get("topic_tags", [])
    source_type = frontmatter.get("source_type", "expert_rules")

    # Split by H2/H3 headers (keep the header with its section)
    sections = re.split(r"(?=^#{2,3}\s+)", body, flags=re.MULTILINE)

    chunks: list[dict] = []
    chunk_index = 0

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Skip if this is just the H1 title line
        if re.match(r"^#\s+", section) and "\n" not in section.strip():
            continue

        # Check if section is too large and needs splitting
        if estimate_tokens(section) > CHUNK_MAX_TOKENS:
            sub_chunks = _split_large_section(section, h1_title)
            for sc in sub_chunks:
                chunks.append({
                    "source_uri": source_uri,
                    "chunk_index": chunk_index,
                    "raw_text": sc,
                    "token_count": estimate_tokens(sc),
                    "topic_tags": topic_tags,
                    "source_type": source_type,
                })
                chunk_index += 1
        else:
            # Prepend H1 title for context
            chunk_text = f"# {h1_title}\n\n{section}"
            chunks.append({
                "source_uri": source_uri,
                "chunk_index": chunk_index,
                "raw_text": chunk_text,
                "token_count": estimate_tokens(chunk_text),
                "topic_tags": topic_tags,
                "source_type": source_type,
            })
            chunk_index += 1

    return chunks


def _split_large_section(section: str, h1_title: str) -> list[str]:
    """Split a large section at paragraph boundaries without severing code blocks.

    A 'paragraph' is a block of text separated by blank lines, but we treat
    a code fence (```) and its content as a single unit with the preceding paragraph.
    """
    # First, merge code blocks with their preceding paragraph
    # Split into paragraphs
    raw_paragraphs = re.split(r"\n\s*\n", section)
    merged: list[str] = []
    i = 0
    while i < len(raw_paragraphs):
        para = raw_paragraphs[i].strip()
        if not para:
            i += 1
            continue

        # Check if the NEXT paragraph is a code block
        if i + 1 < len(raw_paragraphs) and raw_paragraphs[i + 1].strip().startswith("```"):
            # Merge current paragraph with code block
            merged.append(para + "\n\n" + raw_paragraphs[i + 1].strip())
            i += 2
        elif para.startswith("```"):
            # Code block without preceding text — attach to previous if possible
            if merged:
                merged[-1] = merged[-1] + "\n\n" + para
            else:
                merged.append(para)
            i += 1
        else:
            merged.append(para)
            i += 1

    # Now group merged paragraphs into chunks under the token ceiling
    result: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0
    header_prefix = f"# {h1_title}\n\n"

    for para in merged:
        para_tokens = estimate_tokens(para)
        if current_tokens + para_tokens > CHUNK_TARGET_TOKENS and current_parts:
            result.append(header_prefix + "\n\n".join(current_parts))
            current_parts = []
            current_tokens = 0
        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        result.append(header_prefix + "\n\n".join(current_parts))

    return result


async def ingest(docs_dir: Path | None = None) -> int:
    """Main ingestion pipeline. Returns number of chunks ingested."""
    if docs_dir is None:
        docs_dir = DEFAULT_DIR

    docs_dir = Path(docs_dir).resolve()
    if not docs_dir.exists():
        print(f"Error: Directory {docs_dir} does not exist")
        return 0

    md_files = sorted(docs_dir.glob("*.md"))
    print(f"Found {len(md_files)} markdown files in {docs_dir}")
    if not md_files:
        return 0

    all_chunks: list[dict] = []
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        source_uri = f"golden_dataset/{md_file.name}"
        chunks = chunk_markdown(content, source_uri)
        print(f"  {source_uri}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    print(f"\nTotal chunks: {len(all_chunks)}")

    # Embed in batches
    print("Generating embeddings...")
    all_embeddings: list[list[float] | None] = []
    texts = [c["raw_text"] for c in all_chunks]
    has_embeddings = True

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i: i + EMBED_BATCH_SIZE]
        embeddings = await embed_domain_texts(batch)
        if embeddings is None:
            print("  WARNING: OPENAI_EMBEDDING_API_KEY not set — inserting without embeddings")
            all_embeddings.extend([None] * len(batch))
            has_embeddings = False
        else:
            all_embeddings.extend(embeddings)
        print(f"  Processed {min(i + EMBED_BATCH_SIZE, len(texts))}/{len(texts)}")

    if not has_embeddings:
        print("  TIP: Set OPENAI_EMBEDDING_API_KEY in .env to enable vector search")

    # Upsert to DB
    print("Upserting to database...")
    async with async_session_factory() as db:
        for chunk_data, embedding in zip(all_chunks, all_embeddings):
            stmt = pg_insert(DomainKnowledgeChunk).values(
                id=uuid.uuid4(),
                source_uri=chunk_data["source_uri"],
                chunk_index=chunk_data["chunk_index"],
                raw_text=chunk_data["raw_text"],
                token_count=chunk_data["token_count"],
                embedding=embedding,
                topic_tags=chunk_data["topic_tags"],
                source_type=chunk_data["source_type"],
                is_deprecated=False,
            ).on_conflict_do_update(
                constraint="uq_dk_source_chunk",
                set_={
                    "raw_text": chunk_data["raw_text"],
                    "token_count": chunk_data["token_count"],
                    "embedding": embedding,
                    "topic_tags": chunk_data["topic_tags"],
                    "source_type": chunk_data["source_type"],
                    "is_deprecated": False,
                },
            )
            await db.execute(stmt)

        await db.commit()
        print(f"Upserted {len(all_chunks)} chunks into domain_knowledge_chunks")

    return len(all_chunks)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Ingest golden dataset into domain knowledge store")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="Path to golden dataset directory")
    args = parser.parse_args()
    asyncio.run(ingest(Path(args.dir)))


if __name__ == "__main__":
    main()
