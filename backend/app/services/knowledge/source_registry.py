"""Knowledge source registry — defines crawlable NetSuite documentation sources."""

from dataclasses import dataclass


@dataclass
class KnowledgeSource:
    name: str
    base_url: str
    url_patterns: list[str]      # URL path patterns to crawl
    parser: str                   # "oracle_help" | "blog" | "generic"
    priority: int                 # 1=high, 3=low
    max_pages_per_run: int        # Cap pages per crawl session
    crawl_delay_seconds: float    # Politeness delay between requests


SOURCES = [
    KnowledgeSource(
        name="oracle_netsuite_help",
        base_url="https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help",
        url_patterns=[
            "/section_N*.html",         # Record type docs
            "/chapter_N*.html",         # Feature chapters
            "/bridgehead_N*.html",      # SuiteQL reference
        ],
        parser="oracle_help",
        priority=1,
        max_pages_per_run=50,
        crawl_delay_seconds=2.0,
    ),
    KnowledgeSource(
        name="tim_dietrich_suiteql",
        base_url="https://timdietrich.me/blog",
        url_patterns=[
            "/netsuite-suiteql-*",      # SuiteQL specific posts
            "/netsuite-analytics-*",    # Analytics posts
        ],
        parser="blog",
        priority=1,
        max_pages_per_run=30,
        crawl_delay_seconds=1.5,
    ),
    KnowledgeSource(
        name="suiterep",
        base_url="https://suiterep.com",
        url_patterns=[
            "/netsuite-*",
            "/suiteql-*",
        ],
        parser="blog",
        priority=2,
        max_pages_per_run=20,
        crawl_delay_seconds=2.0,
    ),
]
