"""No-LLM-numbers contract for agent narratives.

An agent narrative may contain a numeric token ONLY if either:
  (a) its normalized form appears among the evidence's own numeric tokens, or
  (b) the whole whitespace-delimited word containing it appears verbatim in
      some evidence value (keeps ids like ch_123 / R628489275 working when
      they come from evidence).
Violation → the caller degrades the item to needs_human (never ships an
invented figure). Digit runs glued to letters (e.g. "USD1284.55") are
extracted as candidate tokens too — they are NOT exempt just because they
aren't a standalone number, otherwise a fabricated figure could hide by
gluing itself to a unit/currency code.
"""

from __future__ import annotations

import re

# Digit runs, with optional thousands separators and a decimal tail. No
# lookaround: this deliberately matches numbers glued to letters too (see
# module docstring) — narrative_respects_evidence decides what's ALLOWED.
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _normalize(num: str) -> str:
    return num.replace(",", "")


def numeric_tokens(text: str) -> set[str]:
    return {_normalize(m.group(0)) for m in _NUM_RE.finditer(text or "")}


def narrative_respects_evidence(narrative: str, evidence_values: list[str]) -> bool:
    allowed_numbers: set[str] = set()
    allowed_words: set[str] = set()
    for v in evidence_values:
        s = str(v)
        allowed_numbers |= numeric_tokens(s)
        allowed_words.update(s.split())

    for word in (narrative or "").split():
        for m in _NUM_RE.finditer(word):
            candidate = _normalize(m.group(0))
            if candidate in allowed_numbers:
                continue
            if word in allowed_words:
                continue
            return False
    return True
