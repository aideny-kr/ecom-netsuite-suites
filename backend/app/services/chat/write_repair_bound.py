"""Persisted bound for the POST-approval write-repair loop.

This is the part of the agentic-repair design (requirement B/D — see
``.superpowers/sdd/2026-08-19-agentic-netsuite-write-loop/agentic-repair-design.json``
``bounding``) that decides, when a human-approved write is rejected BY
NETSUITE, whether the turn may re-enter the agent to compose a revised
proposal, or must stop. Everything here is pure and DB-free — the orchestrator
resolves the persisted chain (the previous card's ``failure_fingerprint``)
and calls :func:`decide_repair_bound` with the result. Keeping the decision
pure is what makes it testable without a database and without risking the
ceiling being "advisory" — the persisted row IS the counter; this module only
computes what to write into it.

Decision table:
    error   — the rejection could not be fingerprinted at all (no
              ``o:errorDetails`` AND an empty raw-text fallback). Never guess
              a repair is safe to attempt from nothing to compare against.
    stall   — this rejection's fingerprint equals the fingerprint of the
              attempt immediately before it in the chain: recomposing changed
              nothing NetSuite cares about, so retrying again would not help.
    budget  — ``current_attempt >= max_attempts`` (ceiling matches
              ``WriteRepairState.max_attempts``, the PRE-approval repair
              loop's own budget).
    reenter — otherwise. A genuinely different repair still consumes budget
              unconditionally; a distinct fingerprint only means THIS
              particular exit doesn't fire on stall grounds, not that the
              budget stops counting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

# The design's "raw error, first ~2000 chars" fallback cap.
_RAW_FALLBACK_CAP = 2000


def _find_error_details(node: Any) -> list[str]:
    """Mechanical JSON key traversal for ``o:errorDetails[].detail`` — NEVER
    regex, NEVER field-name parsing. Returns the first non-empty list found
    (depth-first); NetSuite's RFC-9110 problem docs nest this array at
    varying depths depending on which layer wrapped the response."""
    if isinstance(node, dict):
        details = node.get("o:errorDetails")
        if isinstance(details, list):
            found = [
                d["detail"]
                for d in details
                if isinstance(d, dict) and isinstance(d.get("detail"), str) and d["detail"].strip()
            ]
            if found:
                return found
        for value in node.values():
            found = _find_error_details(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_error_details(item)
            if found:
                return found
    return []


def extract_netsuite_error_details(raw_result_str: str) -> list[str]:
    """Extract ``o:errorDetails[].detail`` strings from a NetSuite error
    response. Returns ``[]`` if the shape isn't present, the JSON is
    malformed, or the input is empty — never raises."""
    try:
        parsed = json.loads(raw_result_str)
    except (json.JSONDecodeError, TypeError):
        return []
    return _find_error_details(parsed)


def compute_failure_fingerprint(raw_result_str: str, raw_fallback: str) -> tuple[str | None, str]:
    """Return ``(fingerprint, basis)``.

    Prefers the sorted, joined ``o:errorDetails`` text (stable regardless of
    key order or which detail arrived first); falls back to *raw_fallback*
    (stripped, capped at ``_RAW_FALLBACK_CAP`` chars) when no details were
    found. ``fingerprint`` is ``None`` — and ``basis`` is ``""`` — only when
    BOTH are empty: the caller must treat that as ``exit_reason='error'``,
    never attempt a stall comparison against a fingerprint that stands for
    nothing.
    """
    details = extract_netsuite_error_details(raw_result_str)
    if details:
        basis = "|".join(sorted(details))
    else:
        basis = (raw_fallback or "").strip()[:_RAW_FALLBACK_CAP]

    if not basis:
        return None, ""
    return hashlib.sha256(basis.encode("utf-8")).hexdigest(), basis


@dataclass(frozen=True)
class RepairBoundDecision:
    reason: Literal["reenter", "stall", "budget", "error"]
    # The attempt number the NEXT card (if any) would carry. Terminal
    # decisions (stall/budget/error) still report it for symmetry/logging —
    # callers must not build a new card on a terminal decision regardless.
    next_attempt: int


def decide_repair_bound(
    *,
    current_attempt: int,
    current_fingerprint: str | None,
    previous_fingerprint: str | None,
    max_attempts: int = 2,
) -> RepairBoundDecision:
    """Pure decision — no DB access.

    ``previous_fingerprint`` is the ``failure_fingerprint`` persisted on the
    chain entry immediately before *current_attempt* (``None`` when
    *current_attempt* is 0 — the root's own first failure has no
    predecessor to stall against).
    """
    if current_fingerprint is None:
        return RepairBoundDecision(reason="error", next_attempt=current_attempt + 1)
    if previous_fingerprint is not None and previous_fingerprint == current_fingerprint:
        return RepairBoundDecision(reason="stall", next_attempt=current_attempt + 1)
    if current_attempt >= max_attempts:
        return RepairBoundDecision(reason="budget", next_attempt=current_attempt + 1)
    return RepairBoundDecision(reason="reenter", next_attempt=current_attempt + 1)
