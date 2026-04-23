"""Paragraph-boundary text chunker with token-based size limit and overlap.

Uses tiktoken (cl100k_base) if available, else falls back to a ~4 chars/token
heuristic. Returns overlapping chunks suitable for RAG embeddings.
"""

from __future__ import annotations

import re

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_TIKTOKEN_ENCODER = None
_TIKTOKEN_TRIED = False


def _get_encoder():
    global _TIKTOKEN_ENCODER, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENCODER
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken

        _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENCODER = None
    return _TIKTOKEN_ENCODER


def count_tokens(text: str) -> int:
    enc = _get_encoder()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in _PARAGRAPH_RE.split(text)]
    return [p for p in parts if p]


def _tail_by_tokens(text: str, target_tokens: int) -> str:
    """Return trailing text whose token count is close to (but not over) target_tokens."""
    words = text.split()
    if not words:
        return ""
    # Binary-search-ish: grow from the end until we reach the budget.
    # Use a linear sweep from a reasonable upper bound for simplicity.
    acc: list[str] = []
    tokens = 0
    for w in reversed(words):
        w_tokens = count_tokens(w + " ")
        if tokens + w_tokens > target_tokens and acc:
            break
        acc.append(w)
        tokens += w_tokens
    acc.reverse()
    return " ".join(acc)


def _split_by_words(text: str, target_tokens: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    total_tokens = count_tokens(text)
    if total_tokens <= target_tokens:
        return [text]
    n_pieces = (total_tokens + target_tokens - 1) // target_tokens
    words_per_piece = max(1, len(words) // n_pieces + 1)
    return [" ".join(words[i : i + words_per_piece]) for i in range(0, len(words), words_per_piece)]


def chunk_text(
    text: str,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
) -> list[dict]:
    """Split text into overlapping chunks honoring paragraph boundaries.

    Returns list of {"chunk_index": int, "content": str, "token_count": int}.
    """
    if not text or not text.strip():
        return []

    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []

    units: list[str] = []
    for p in paragraphs:
        if count_tokens(p) > max_tokens:
            units.extend(_split_by_words(p, max_tokens))
        else:
            units.append(p)

    chunks: list[dict] = []
    buf: list[str] = []
    buf_tokens = 0

    def flush(overlap_from_prev: str = "") -> str:
        nonlocal buf, buf_tokens
        content = "\n\n".join(buf)
        if overlap_from_prev:
            content = overlap_from_prev + "\n\n" + content
        chunks.append(
            {
                "chunk_index": len(chunks),
                "content": content,
                "token_count": count_tokens(content),
            }
        )
        if overlap_tokens <= 0:
            tail_text = ""
        else:
            tail_text = _tail_by_tokens(content, overlap_tokens)
        buf = []
        buf_tokens = 0
        return tail_text

    pending_overlap = ""
    for unit in units:
        unit_tokens = count_tokens(unit)
        if buf_tokens + unit_tokens > max_tokens and buf:
            pending_overlap = flush(pending_overlap)
        buf.append(unit)
        buf_tokens += unit_tokens

    if buf:
        flush(pending_overlap)

    return chunks
