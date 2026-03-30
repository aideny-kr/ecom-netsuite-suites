"""Three-tier matching engine: deterministic -> fuzzy -> unmatched.

All matching is deterministic (no LLM). All amounts use Decimal.
"""

from __future__ import annotations

import re
from decimal import Decimal

import structlog

from app.schemas.reconciliation import DepositRecord, MatchCandidate, PayoutRecord

logger = structlog.get_logger()

# Tolerances
_ROUNDING_TOLERANCE = Decimal("0.05")  # ±$0.05 for exact match rounding
_FX_TOLERANCE_PCT = Decimal("0.01")  # ±1% for FX variance
_DATE_WINDOW_DAYS = 3  # T+0..T+3 for timing matches


class MatchingEngine:
    """Orchestrates deterministic and fuzzy matching of payouts to deposits."""

    def __init__(
        self,
        rounding_tolerance: Decimal = _ROUNDING_TOLERANCE,
        fx_tolerance_pct: Decimal = _FX_TOLERANCE_PCT,
        date_window_days: int = _DATE_WINDOW_DAYS,
    ) -> None:
        self.rounding_tolerance = rounding_tolerance
        self.fx_tolerance_pct = fx_tolerance_pct
        self.date_window_days = date_window_days

    def match(
        self,
        payouts: list[PayoutRecord],
        deposits: list[DepositRecord],
    ) -> list[MatchCandidate]:
        """Run full matching pipeline: deterministic -> fuzzy -> unmatched.

        Returns one MatchCandidate per payout + one per unmatched deposit.
        Each deposit is consumed at most once (no double-matching).
        """
        results: list[MatchCandidate] = []
        matched_deposit_ids: set[str] = set()
        matched_payout_ids: set[str] = set()

        # --- Tier 1: Deterministic matching ---
        for payout in payouts:
            candidate = self._deterministic_match(payout, deposits, matched_deposit_ids)
            if candidate is not None:
                results.append(candidate)
                matched_payout_ids.add(payout.id)
                for d in candidate.deposits:
                    matched_deposit_ids.add(d.id)

        # --- Tier 2: Fuzzy matching (for remaining payouts) ---
        remaining_payouts = [p for p in payouts if p.id not in matched_payout_ids]
        for payout in remaining_payouts:
            candidate = self._fuzzy_match(payout, deposits, matched_deposit_ids)
            if candidate is not None:
                results.append(candidate)
                matched_payout_ids.add(payout.id)
                for d in candidate.deposits:
                    matched_deposit_ids.add(d.id)

        # --- Unmatched payouts ---
        for payout in payouts:
            if payout.id not in matched_payout_ids:
                results.append(
                    MatchCandidate(
                        payout=payout,
                        deposits=[],
                        match_type="unmatched",
                        confidence=Decimal("0"),
                        variance_amount=payout.net_amount,
                        variance_type="missing",
                        variance_explanation=f"No matching deposit found for payout {payout.source_id}",
                        match_rule="no_match",
                    )
                )

        # --- Unmatched deposits ---
        for deposit in deposits:
            if deposit.id not in matched_deposit_ids:
                results.append(
                    MatchCandidate(
                        payout=PayoutRecord(
                            id="",
                            source_id="",
                            amount=Decimal("0"),
                            net_amount=Decimal("0"),
                            fee_amount=Decimal("0"),
                            currency=deposit.currency,
                            arrival_date=None,
                        ),
                        deposits=[deposit],
                        match_type="unmatched",
                        confidence=Decimal("0"),
                        variance_amount=deposit.amount,
                        variance_type="missing",
                        variance_explanation=(f"No matching payout found for deposit {deposit.netsuite_internal_id}"),
                        match_rule="no_match",
                    )
                )

        logger.info(
            "matching_engine.complete",
            total_payouts=len(payouts),
            total_deposits=len(deposits),
            matched=len([r for r in results if r.match_type != "unmatched"]),
            unmatched=len([r for r in results if r.match_type == "unmatched"]),
        )

        return results

    # ------------------------------------------------------------------
    # Tier 1: Deterministic
    # ------------------------------------------------------------------
    def _deterministic_match(
        self,
        payout: PayoutRecord,
        deposits: list[DepositRecord],
        consumed: set[str],
    ) -> MatchCandidate | None:
        """Try exact payout ID + amount + date match."""
        # Sort deposits by ID for deterministic tie-breaking
        sorted_deposits = sorted(deposits, key=lambda d: d.id)

        for deposit in sorted_deposits:
            if deposit.id in consumed:
                continue
            if deposit.currency != payout.currency:
                continue

            # Date window check: deposit must be within T+0..T+3 of payout arrival
            if payout.arrival_date and deposit.transaction_date:
                day_diff = (deposit.transaction_date - payout.arrival_date).days
                if day_diff < 0 or day_diff > self.date_window_days:
                    continue
            elif payout.arrival_date or deposit.transaction_date:
                # One date missing — skip deterministic, let fuzzy handle it
                continue

            # Check explicit related_payout_id
            id_match = deposit.related_payout_id == payout.source_id

            # Check memo contains payout ID
            memo_match = False
            if not id_match and deposit.memo:
                memo_match = payout.source_id in deposit.memo

            if not (id_match or memo_match):
                continue

            # Amount check: net_amount vs deposit amount within rounding tolerance
            diff = abs(payout.net_amount - deposit.amount)
            if diff <= self.rounding_tolerance:
                confidence = Decimal("1.0") if id_match else Decimal("0.95")
                return MatchCandidate(
                    payout=payout,
                    deposits=[deposit],
                    match_type="deterministic",
                    confidence=confidence,
                    variance_amount=diff,
                    variance_type="fx_rounding" if diff > 0 else None,
                    match_rule="exact_payout_id" if id_match else "memo_payout_id",
                )

        return None

    # ------------------------------------------------------------------
    # Tier 2: Fuzzy
    # ------------------------------------------------------------------
    def _fuzzy_match(
        self,
        payout: PayoutRecord,
        deposits: list[DepositRecord],
        consumed: set[str],
    ) -> MatchCandidate | None:
        """Try fuzzy matching: amount tolerance, date window, narration similarity.

        Uses amount-range bucketing (±5%) to avoid O(n²) on large datasets.
        Also attempts split-payout matching (one payout -> multiple deposits summing to net).
        """
        best_candidate: MatchCandidate | None = None
        best_confidence = Decimal("0")

        # Amount-range bucketing: only consider deposits within ±5% of net or gross amount
        bucket_pct = Decimal("0.05")
        lo = min(payout.net_amount, payout.amount) * (1 - bucket_pct)
        hi = max(payout.net_amount, payout.amount) * (1 + bucket_pct)

        # Sort by ID for deterministic tie-breaking
        sorted_deposits = sorted(deposits, key=lambda d: d.id)

        for deposit in sorted_deposits:
            if deposit.id in consumed:
                continue
            if deposit.currency != payout.currency:
                continue
            # Blocking strategy: skip deposits outside ±5% amount range
            if deposit.amount < lo or deposit.amount > hi:
                continue

            confidence = Decimal("0")
            signals: list[str] = []

            # --- Amount similarity ---
            # Try matching against net_amount (Stripe net)
            amount_diff = abs(payout.net_amount - deposit.amount)
            amount_pct = amount_diff / payout.net_amount * 100 if payout.net_amount > 0 else Decimal("100")

            if amount_diff <= self.rounding_tolerance:
                confidence += Decimal("0.40")
                signals.append("amount_exact")
            elif amount_pct <= self.fx_tolerance_pct * 100:
                confidence += Decimal("0.30")
                signals.append("amount_within_fx_tolerance")
            elif amount_diff == payout.fee_amount:
                # NetSuite recorded gross, not net — fee variance
                confidence += Decimal("0.35")
                signals.append("fee_variance")
            else:
                # Amount too far off for single-deposit fuzzy match
                continue

            # --- Date proximity ---
            if payout.arrival_date and deposit.transaction_date:
                day_diff = abs((deposit.transaction_date - payout.arrival_date).days)
                if day_diff == 0:
                    confidence += Decimal("0.30")
                    signals.append("same_day")
                elif day_diff <= self.date_window_days:
                    confidence += Decimal("0.25")
                    signals.append(f"within_{day_diff}_days")
                else:
                    confidence += Decimal("0.10")
                    signals.append(f"date_diff_{day_diff}_days")

            # --- Narration / memo similarity ---
            if deposit.memo and payout.source_id:
                if payout.source_id.lower() in deposit.memo.lower():
                    confidence += Decimal("0.20")
                    signals.append("memo_contains_payout_id")
                elif _word_overlap(deposit.memo, f"stripe payout {payout.source_id}") >= 0.5:
                    confidence += Decimal("0.10")
                    signals.append("memo_partial_overlap")

            if confidence > best_confidence:
                # Classify the variance
                from app.services.reconciliation.variance_classifier import classify_variance

                variance_type, explanation = classify_variance(
                    payout=payout,
                    deposit=deposit,
                    amount_diff=amount_diff,
                    day_diff=(
                        abs((deposit.transaction_date - payout.arrival_date).days)
                        if payout.arrival_date and deposit.transaction_date
                        else 0
                    ),
                    signals=signals,
                )

                best_confidence = confidence
                best_candidate = MatchCandidate(
                    payout=payout,
                    deposits=[deposit],
                    match_type="fuzzy",
                    confidence=min(confidence, Decimal("0.94")),  # Cap fuzzy at 0.94
                    variance_amount=amount_diff,
                    variance_type=variance_type,
                    variance_explanation=explanation,
                    match_rule="+".join(signals),
                )

        # --- Split payout matching ---
        if best_candidate is None:
            split_candidate = self._split_payout_match(payout, deposits, consumed)
            if split_candidate is not None:
                return split_candidate

        return best_candidate

    def _split_payout_match(
        self,
        payout: PayoutRecord,
        deposits: list[DepositRecord],
        consumed: set[str],
    ) -> MatchCandidate | None:
        """Try matching one payout to multiple deposits that sum to its net amount.

        Also detects duplicates: multiple deposits referencing the same payout ID.
        """
        available = [d for d in deposits if d.id not in consumed and d.currency == payout.currency]

        if len(available) < 2:
            return None

        # Duplicate detection: multiple deposits claim the same payout
        payout_refs = [
            d for d in available if d.related_payout_id == payout.source_id or (d.memo and payout.source_id in d.memo)
        ]
        if len(payout_refs) >= 2:
            return MatchCandidate(
                payout=payout,
                deposits=payout_refs,
                match_type="exception",
                confidence=Decimal("0.60"),
                variance_amount=sum(d.amount for d in payout_refs[1:]),
                variance_type="duplicate",
                variance_explanation=(f"Duplicate: {len(payout_refs)} deposits reference payout {payout.source_id}"),
                match_rule="duplicate_detection",
            )

        # Only try deposits on the same day or within date window
        if payout.arrival_date:
            available = [
                d
                for d in available
                if d.transaction_date and abs((d.transaction_date - payout.arrival_date).days) <= self.date_window_days
            ]

        # Sort by amount descending for greedy matching, then by ID for tie-breaking
        available.sort(key=lambda d: (-d.amount, d.id))

        # Greedy subset-sum: try to find deposits summing to net_amount
        target = payout.net_amount
        selected: list[DepositRecord] = []
        running_sum = Decimal("0")

        for dep in available:
            if running_sum + dep.amount <= target + self.rounding_tolerance:
                selected.append(dep)
                running_sum += dep.amount

        diff = abs(target - running_sum)
        if len(selected) >= 2 and diff <= self.rounding_tolerance:
            return MatchCandidate(
                payout=payout,
                deposits=selected,
                match_type="fuzzy",
                confidence=Decimal("0.80"),
                variance_amount=diff,
                variance_type="fx_rounding" if diff > 0 else None,
                variance_explanation=f"Split payout: {len(selected)} deposits sum to net amount",
                match_rule="split_payout",
            )

        return None


def _word_overlap(text_a: str, text_b: str) -> float:
    """Simple word overlap ratio (Jaccard) between two strings."""
    words_a = set(re.findall(r"\w+", text_a.lower()))
    words_b = set(re.findall(r"\w+", text_b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
