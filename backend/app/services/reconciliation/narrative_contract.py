"""No-LLM-numbers contract for agent narratives.

An agent narrative may contain a numeric token ONLY if that exact normalized
number appears in the evidence provided to the model. Violation → the caller
degrades the item to needs_human (never ships an invented figure).
Identifiers are exempt: tokens embedded in id-like words (ch_123, R628489275)
are not treated as numbers.
"""

from __future__ import annotations

import re

# Numbers NOT preceded/followed by id-ish characters (letters, _, digits-run
# glued to letters). Allows $ and thousands separators; captures decimals.
_NUM_RE = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w.])")


def _normalize(whole: str, frac: str | None) -> str:
    return whole.replace(",", "") + (frac or "")


def numeric_tokens(text: str) -> set[str]:
    return {_normalize(m.group(1), m.group(2)) for m in _NUM_RE.finditer(text or "")}


def narrative_respects_evidence(narrative: str, evidence_values: list[str]) -> bool:
    allowed: set[str] = set()
    for v in evidence_values:
        allowed |= numeric_tokens(str(v))
    return numeric_tokens(narrative) <= allowed
