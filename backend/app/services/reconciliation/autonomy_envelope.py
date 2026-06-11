"""Rung-1 autonomy envelope (Bet 3): which recon lines a system actor may
auto-approve. v1 is deliberately the tightest possible envelope —
bucket 'matches' + deterministic match + zero variance + non-terminal status
(decision doc D2: docs/superpowers/specs/2026-06-10-bet3-autonomous-posting-trust-model.md).

PURE module: no DB access, no side effects. The dry-run worker feeds it rows
and reports what WOULD be auto-approved; nothing here mutates state. NOT
confidence-gated — the advisory scorer is uncalibrated (0-approval corpus).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import BUCKET_MATCHES

ENVELOPE_VERSION = "v1"

# Mirrors the bulk-approve guard (reconciliation.py approve_bucket): rows
# already approved/rejected/locked can never be acted on again.
_TERMINAL_STATUSES = ("approved", "rejected", "locked")


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
            "excluded": dict(self.excluded),
        }


def evaluate(results: Iterable[ReconciliationResult]) -> EnvelopeReport:
    """Classify rows into envelope candidates vs excluded-with-reason."""
    candidates: list[ReconciliationResult] = []
    excluded: dict[str, int] = {}

    def _exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for row in results:
        if row.status in _TERMINAL_STATUSES:
            _exclude("terminal_status")
        elif row.bucket != BUCKET_MATCHES:
            _exclude("bucket_not_matches")
        elif row.match_type != "deterministic":
            _exclude("not_deterministic")
        elif row.variance_amount is None or row.variance_amount != Decimal("0"):
            _exclude("has_variance")
        else:
            candidates.append(row)

    total = sum((c.stripe_amount or Decimal("0") for c in candidates), Decimal("0"))
    return EnvelopeReport(
        envelope_version=ENVELOPE_VERSION,
        candidate_ids=tuple(str(c.id) for c in candidates),
        candidate_count=len(candidates),
        candidate_total_amount=total,
        excluded=excluded,
    )
