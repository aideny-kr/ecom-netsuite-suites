"""Shared text-processing utilities."""

from __future__ import annotations


def sanitize_utf8(text: str) -> str:
    """Strip invalid UTF-8 sequences that crash PostgreSQL ILIKE queries.

    Some SuiteScript content arrives with broken box-drawing or other
    non-UTF-8 byte sequences (e.g. 0xe2 0x94).  Encode → decode with
    'replace' to swap them for the Unicode replacement character, then
    strip those replacement characters out entirely.
    """
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace").replace("\ufffd", "")
