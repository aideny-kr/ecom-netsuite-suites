"""Rung-1 autonomy envelope (Bet 3): which recon lines a system actor may
auto-approve. v1 is deliberately the tightest possible envelope —
bucket 'matches' + deterministic match + zero variance + non-terminal status
(decision doc D2: docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md).

PURE module: no DB access, no side effects. The dry-run worker feeds it rows
and reports what WOULD be auto-approved; nothing here mutates state. NOT
confidence-gated — the advisory scorer is uncalibrated (0-approval corpus).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_MATCHES,
    TERMINAL_RESULT_STATUSES,
)

ENVELOPE_VERSION = "v1"

# Audit payloads (and Job.result_summary, which InstrumentedTask copies the task
# result into) must stay bounded — a Framework-scale run has tens of thousands of
# qualifying rows. Counts/totals stay exact; ids are a capped sample, capped at
# construction so the report never holds tens of thousands of UUID strings.
MAX_PAYLOAD_CANDIDATE_IDS = 200


@dataclass(frozen=True)
class EnvelopeReport:
    envelope_version: str
    candidate_ids: tuple[str, ...]
    candidate_count: int
    candidate_total_amount: Decimal
    excluded: dict[str, int]

    def to_payload(self) -> dict:
        """JSON-safe dict for audit payloads (Decimal → str)."""
        return {
            "envelope_version": self.envelope_version,
            "candidate_count": self.candidate_count,
            "candidate_total_amount": str(self.candidate_total_amount),
            "candidate_ids": list(self.candidate_ids),
            "candidate_ids_truncated": self.candidate_count > len(self.candidate_ids),
            "excluded": dict(self.excluded),
        }


def evaluate(results: Iterable[Any]) -> EnvelopeReport:
    """Classify rows into envelope candidates vs excluded-with-reason.

    Accepts any row exposing id/status/bucket/match_type/variance_amount/
    stripe_amount (ORM objects or column-only Row tuples).
    """
    excluded: Counter[str] = Counter()
    candidate_ids: list[str] = []
    candidate_count = 0
    total = Decimal("0")

    for row in results:
        if row.status in TERMINAL_RESULT_STATUSES:
            excluded["terminal_status"] += 1
        elif row.bucket != BUCKET_MATCHES:
            excluded["bucket_not_matches"] += 1
        elif row.match_type != "deterministic":
            excluded["not_deterministic"] += 1
        elif row.variance_amount is None or row.variance_amount != Decimal("0"):
            excluded["has_variance"] += 1
        elif row.stripe_amount is None:
            # Amount-unknown rows must not be "$0-blessed": the per-run dollar
            # exposure would silently understate them. Out, with their own reason.
            excluded["amount_unknown"] += 1
        else:
            candidate_count += 1
            total += row.stripe_amount
            if len(candidate_ids) < MAX_PAYLOAD_CANDIDATE_IDS:
                candidate_ids.append(str(row.id))

    return EnvelopeReport(
        envelope_version=ENVELOPE_VERSION,
        candidate_ids=tuple(candidate_ids),
        candidate_count=candidate_count,
        candidate_total_amount=total,
        excluded=dict(excluded),
    )
