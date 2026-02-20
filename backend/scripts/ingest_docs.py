"""Ingest markdown docs into doc_chunks table with vector embeddings."""

import argparse
import asyncio
import re
import uuid
from pathlib import Path

from sqlalchemy import delete

from app.core.database import async_session_factory
from app.models.chat import DocChunk
from app.services.chat.embeddings import embed_texts

SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")
CHUNK_TARGET_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
EMBED_BATCH_SIZE = 20


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def chunk_text(
    text: str,
    target_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    """Split text into chunks at paragraph boundaries."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    current_chunk: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_tokens = estimate_tokens(para)

        if current_tokens + para_tokens > target_tokens and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            # Keep overlap
            overlap_text = ""
            overlap_count = 0
            for p in reversed(current_chunk):
                p_tokens = estimate_tokens(p)
                if overlap_count + p_tokens > overlap_tokens:
                    break
                overlap_text = p + "\n\n" + overlap_text if overlap_text else p
                overlap_count += p_tokens
            current_chunk = [overlap_text] if overlap_text else []
            current_tokens = overlap_count

        current_chunk.append(para)
        current_tokens += para_tokens

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def extract_title(content: str, file_path: str) -> str:
    """Extract title from first H1 heading or use filename."""
    match = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return Path(file_path).stem.replace("-", " ").replace("_", " ").title()


async def ingest(docs_dir: str, source_prefix: str = "") -> None:
    """Main ingestion pipeline."""
    docs_path = Path(docs_dir).resolve()
    if not docs_path.exists():
        print(f"Error: Directory {docs_path} does not exist")
        return

    md_files = sorted(docs_path.rglob("*.md"))
    print(f"Found {len(md_files)} markdown files in {docs_path}")

    if not md_files:
        return

    all_chunks: list[dict] = []
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        rel_path = str(md_file.relative_to(docs_path))
        prefixed_path = f"{source_prefix}{rel_path}" if source_prefix else rel_path
        title = extract_title(content, rel_path)
        chunks = chunk_text(content)
        print(f"  {prefixed_path}: {len(chunks)} chunks")
        for idx, chunk_content in enumerate(chunks):
            all_chunks.append(
                {
                    "source_path": prefixed_path,
                    "title": title,
                    "chunk_index": idx,
                    "content": chunk_content,
                    "token_count": estimate_tokens(chunk_content),
                }
            )

    print(f"\nTotal chunks: {len(all_chunks)}")
    print("Generating embeddings...")

    # Embed in batches
    all_embeddings: list[list[float] | None] = []
    texts = [c["content"] for c in all_chunks]
    has_embeddings = True
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        embeddings = await embed_texts(batch)
        if embeddings is None:
            print("  WARNING: VOYAGE_API_KEY not set — inserting chunks without embeddings (keyword search only)")
            all_embeddings.extend([None] * len(batch))
            has_embeddings = False
        else:
            all_embeddings.extend(embeddings)
        print(f"  Processed {min(i + EMBED_BATCH_SIZE, len(texts))}/{len(texts)}")
    if not has_embeddings:
        print("  TIP: Set VOYAGE_API_KEY in .env and re-run to enable vector similarity search")

    # Write to DB
    print("Writing to database...")
    async with async_session_factory() as db:
        # Delete existing system tenant chunks (scoped to prefix if provided)
        if source_prefix:
            await db.execute(
                delete(DocChunk).where(
                    DocChunk.tenant_id == SYSTEM_TENANT_ID,
                    DocChunk.source_path.like(f"{source_prefix}%"),
                )
            )
        else:
            await db.execute(delete(DocChunk).where(DocChunk.tenant_id == SYSTEM_TENANT_ID))

        for chunk_data, embedding in zip(all_chunks, all_embeddings):
            doc_chunk = DocChunk(
                tenant_id=SYSTEM_TENANT_ID,
                source_path=chunk_data["source_path"],
                title=chunk_data["title"],
                chunk_index=chunk_data["chunk_index"],
                content=chunk_data["content"],
                token_count=chunk_data["token_count"],
                embedding=embedding,
            )
            db.add(doc_chunk)

        await db.commit()
        print(f"Inserted {len(all_chunks)} chunks into doc_chunks table")


def main():
    parser = argparse.ArgumentParser(description="Ingest markdown docs into vector store")
    parser.add_argument("--docs-dir", default="../docs", help="Path to docs directory")
    parser.add_argument(
        "--source-prefix",
        default="",
        help="Prefix for source_path (e.g., 'netsuite_docs/'). Scopes deletion to this prefix only.",
    )
    args = parser.parse_args()
    asyncio.run(ingest(args.docs_dir, source_prefix=args.source_prefix))


if __name__ == "__main__":
    main()
