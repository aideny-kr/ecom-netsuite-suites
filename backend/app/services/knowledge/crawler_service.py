"""Knowledge crawler — fetch, parse, chunk, embed NetSuite documentation."""

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, field

import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import DocChunk
from app.services.knowledge.source_registry import KnowledgeSource

logger = structlog.get_logger()

# System-level knowledge uses a fixed tenant ID (not tenant-scoped)
SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@dataclass
class ParsedContent:
    title: str
    body_text: str
    code_blocks: list[str] = field(default_factory=list)
    published_date: str | None = None
    breadcrumb: str | None = None


@dataclass
class ChunkData:
    content: str
    token_count: int
    metadata: dict


@dataclass
class CrawlResult:
    source_name: str
    pages_crawled: int = 0
    chunks_created: int = 0
    chunks_updated: int = 0
    errors: list[str] = field(default_factory=list)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough estimate


def parse_oracle_help(html: str) -> ParsedContent:
    """Parse Oracle NetSuite Help Center pages."""
    soup = BeautifulSoup(html, "lxml")

    # Remove nav, header, footer, scripts
    for tag in soup.find_all(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    # Extract title
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    # Extract breadcrumb
    breadcrumb_tag = soup.find("nav", class_="breadcrumb") or soup.find("ol", class_="breadcrumb")
    breadcrumb = breadcrumb_tag.get_text(" > ", strip=True) if breadcrumb_tag else None

    # Extract main content
    main = (
        soup.find("div", class_="body")
        or soup.find("div", class_="section")
        or soup.find("main")
        or soup.find("article")
        or soup.body
    )

    # Extract code blocks before getting text
    code_blocks = []
    for pre in (main.find_all("pre") if main else []):
        code_blocks.append(pre.get_text())

    body_text = main.get_text("\n", strip=True) if main else ""

    return ParsedContent(
        title=title,
        body_text=body_text,
        code_blocks=code_blocks,
        breadcrumb=breadcrumb,
    )


def parse_blog(html: str) -> ParsedContent:
    """Parse blog posts (Tim Dietrich, SuiteRep, generic)."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["nav", "header", "footer", "script", "style", "aside", "sidebar"]):
        tag.decompose()

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    # Try article > entry-content > main > body
    article = (
        soup.find("article")
        or soup.find("div", class_="entry-content")
        or soup.find("div", class_="post-content")
        or soup.find("main")
        or soup.body
    )

    code_blocks = []
    for pre in (article.find_all(["pre", "code"]) if article else []):
        code_text = pre.get_text()
        if len(code_text) > 20:  # Skip tiny inline code
            code_blocks.append(code_text)

    # Try to find published date
    published_date = None
    time_tag = soup.find("time")
    if time_tag:
        published_date = time_tag.get("datetime") or time_tag.get_text(strip=True)

    body_text = article.get_text("\n", strip=True) if article else ""

    return ParsedContent(
        title=title,
        body_text=body_text,
        code_blocks=code_blocks,
        published_date=published_date,
    )


PARSERS = {
    "oracle_help": parse_oracle_help,
    "blog": parse_blog,
    "generic": parse_blog,
}


def chunk_parsed_content(
    content: ParsedContent,
    source_name: str,
    url: str,
) -> list[ChunkData]:
    """Split parsed content into embeddable chunks.

    Rules:
    - Target: 400 tokens, hard max: 600 tokens
    - Split at paragraph boundaries (double newline)
    - NEVER split SQL code blocks
    - Prepend title + source context to each chunk
    """
    chunks: list[ChunkData] = []
    prefix = f"# {content.title}\nSource: {source_name} ({url})\n\n"
    prefix_tokens = _estimate_tokens(prefix)

    # Split body into paragraphs
    paragraphs = re.split(r"\n\s*\n", content.body_text)

    current_chunk = prefix
    current_tokens = prefix_tokens

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_tokens = _estimate_tokens(para)

        # If this is a code block (contains SQL keywords), keep it intact
        is_code = any(kw in para.upper() for kw in ["SELECT ", "FROM ", "WHERE ", "JOIN ", "GROUP BY"])

        if is_code and para_tokens > 600:
            # Code block exceeds max — still keep it intact (spec says never split SQL)
            if current_tokens > prefix_tokens:
                chunks.append(ChunkData(
                    content=current_chunk.strip(),
                    token_count=current_tokens,
                    metadata={"source": source_name, "url": url, "title": content.title},
                ))
                current_chunk = prefix
                current_tokens = prefix_tokens

            chunks.append(ChunkData(
                content=prefix + para,
                token_count=prefix_tokens + para_tokens,
                metadata={"source": source_name, "url": url, "title": content.title, "type": "code_block"},
            ))
            continue

        if current_tokens + para_tokens > 600:
            # Flush current chunk
            if current_tokens > prefix_tokens:
                chunks.append(ChunkData(
                    content=current_chunk.strip(),
                    token_count=current_tokens,
                    metadata={"source": source_name, "url": url, "title": content.title},
                ))
            current_chunk = prefix + para + "\n\n"
            current_tokens = prefix_tokens + para_tokens
        else:
            current_chunk += para + "\n\n"
            current_tokens += para_tokens

    # Flush remaining
    if current_tokens > prefix_tokens:
        chunks.append(ChunkData(
            content=current_chunk.strip(),
            token_count=current_tokens,
            metadata={"source": source_name, "url": url, "title": content.title},
        ))

    return chunks


async def discover_urls(source: KnowledgeSource, client: httpx.AsyncClient) -> list[str]:
    """Discover crawlable URLs from a source."""
    urls = []

    # Try sitemap first
    try:
        sitemap_url = f"{source.base_url}/sitemap.xml"
        resp = await client.get(sitemap_url, timeout=10)
        if resp.status_code == 200 and "<urlset" in resp.text:
            soup = BeautifulSoup(resp.text, "lxml")
            for loc in soup.find_all("loc"):
                url = loc.get_text(strip=True)
                if any(re.match(source.base_url + pat.replace("*", ".*"), url) for pat in source.url_patterns):
                    urls.append(url)
            if urls:
                return urls[:source.max_pages_per_run]
    except Exception:
        pass

    # Fallback: try listing page / index
    try:
        resp = await client.get(source.base_url, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    href = source.base_url.rstrip("/") + "/" + href.lstrip("/")
                if any(re.match(source.base_url + pat.replace("*", ".*"), href) for pat in source.url_patterns):
                    urls.append(href)
    except Exception:
        pass

    return list(dict.fromkeys(urls))[:source.max_pages_per_run]  # dedupe, cap


async def crawl_source(source: KnowledgeSource, db: AsyncSession) -> CrawlResult:
    """Crawl a knowledge source, parse, chunk, embed, and store."""
    from app.services.chat.embeddings import embed_texts

    result = CrawlResult(source_name=source.name)
    parser_fn = PARSERS.get(source.parser, parse_blog)

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "SuiteStudio-KnowledgeCrawler/1.0"},
        timeout=15,
    ) as client:
        urls = await discover_urls(source, client)
        logger.info("crawler.urls_discovered", source=source.name, count=len(urls))

        for url in urls:
            try:
                # Check if already crawled (by source_path)
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                source_path = f"crawled/{source.name}/{url_hash}"

                existing = await db.execute(
                    select(DocChunk)
                    .where(DocChunk.source_path == source_path)
                    .limit(1)
                )
                existing_chunk = existing.scalar_one_or_none()

                # Fetch page
                await asyncio.sleep(source.crawl_delay_seconds)
                resp = await client.get(url)
                if resp.status_code != 200:
                    result.errors.append(f"{url}: HTTP {resp.status_code}")
                    continue

                result.pages_crawled += 1
                html = resp.text
                content_hash = _content_hash(html)

                # Skip if content unchanged
                if existing_chunk and (existing_chunk.metadata_ or {}).get("content_hash") == content_hash:
                    continue

                # Parse
                parsed = parser_fn(html)
                if not parsed.body_text or len(parsed.body_text) < 50:
                    continue

                # Chunk
                chunks = chunk_parsed_content(parsed, source.name, url)
                if not chunks:
                    continue

                # Embed
                texts = [c.content for c in chunks]
                embeddings = await embed_texts(texts)

                # Delete old chunks for this URL (if re-crawling)
                if existing_chunk:
                    old_chunks = await db.execute(
                        select(DocChunk).where(DocChunk.source_path == source_path)
                    )
                    for old in old_chunks.scalars():
                        await db.delete(old)
                    result.chunks_updated += len(chunks)
                else:
                    result.chunks_created += len(chunks)

                # Store
                for i, chunk in enumerate(chunks):
                    embedding = embeddings[i] if embeddings and i < len(embeddings) else None
                    doc_chunk = DocChunk(
                        tenant_id=SYSTEM_TENANT_ID,
                        source_path=source_path,
                        title=parsed.title,
                        chunk_index=i,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        embedding=embedding if embedding else None,
                        metadata_={
                            **chunk.metadata,
                            "content_hash": content_hash,
                            "source_type": "crawled",
                            "parser": source.parser,
                        },
                    )
                    db.add(doc_chunk)

                await db.flush()

            except Exception as e:
                result.errors.append(f"{url}: {str(e)[:100]}")
                logger.warning("crawler.page_error", url=url, error=str(e))

    await db.commit()
    logger.info(
        "crawler.source_complete",
        source=source.name,
        pages=result.pages_crawled,
        created=result.chunks_created,
        updated=result.chunks_updated,
        errors=len(result.errors),
    )
    return result
