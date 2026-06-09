"""Recon R2 confidence scorer — pure, deterministic, Decimal-safe.

Computes a transparent "match confidence" from two signals over a
(Stripe charge, NetSuite deposit) pair:

  - Amount signal  (weight 0.6): relative variance between amounts.
  - Temporal signal (weight 0.4): linear decay over a 14-day window.

No I/O, no DB, no app coupling. Safe to import and call from tests
or engine code without side-effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

SCORER_VERSION = "v1"

W_AMOUNT = Decimal("0.6")
W_TEMPORAL = Decimal("0.4")  # weights sum to 1.0 — no renormalization needed

WINDOW_DAYS = 14

# 4-dp quantum — matches the DB column Numeric(5,4)
_Q = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp01(x: Decimal) -> Decimal:
    """Clamp *x* to [0, 1] then quantize to 4 decimal places (ROUND_HALF_UP)."""
    if x < Decimal("0"):
        x = Decimal("0")
    elif x > Decimal("1"):
        x = Decimal("1")
    return x.quantize(_Q, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Signal functions
# ---------------------------------------------------------------------------


def amount_score(charge_amount: Decimal, deposit_amount: Decimal) -> Decimal:
    """Score based on relative variance between *charge_amount* and *deposit_amount*.

    Returns a ``Decimal`` in ``[0, 1]`` quantized to 4 dp.

    - Exact match → ``1.0000``
    - Relative-variance decay: ``1 - |variance| / |charge_amount|``
    - Large variance floors at ``0.0000``
    - ``charge_amount == 0`` handled without division: var==0 → 1.0, else → 0.0
    """
    # Defensive coercion — caller should already pass Decimal, but guard float leaks
    charge_amount = Decimal(str(charge_amount))
    deposit_amount = Decimal(str(deposit_amount))

    variance = abs(charge_amount - deposit_amount)

    if charge_amount == Decimal("0"):
        return Decimal("1.0000") if variance == Decimal("0") else Decimal("0.0000")

    return _clamp01(Decimal("1") - variance / abs(charge_amount))


def temporal_score(
    charge_date: date | None,
    deposit_date: date | None,
) -> Decimal | None:
    """Score based on gap between *charge_date* and *deposit_date*.

    Returns a ``Decimal`` in ``[0, 1]`` quantized to 4 dp, or ``None`` when
    either date is unavailable (signal unavailable).

    - Same-day (gap 0) → ``1.0000``
    - Linear decay: ``1 - gap_days / WINDOW_DAYS``
    - ``≥ WINDOW_DAYS`` → ``0.0000`` (clamped, never negative)
    """
    if charge_date is None or deposit_date is None:
        return None

    gap_days = abs((deposit_date - charge_date).days)
    return _clamp01(Decimal("1") - Decimal(gap_days) / Decimal(WINDOW_DAYS))


def composite(amount: Decimal, temporal: Decimal | None) -> Decimal:
    """Weighted composite of amount and temporal signals.

    If *temporal* is ``None`` (signal unavailable), falls back to *amount* alone
    (already quantized — returned as-is after ensuring 4-dp quantization).

    Otherwise: ``W_AMOUNT * amount + W_TEMPORAL * temporal``
    """
    if temporal is None:
        # Amount-only fallback; ensure 4-dp quantization
        return _clamp01(amount)

    return _clamp01(W_AMOUNT * amount + W_TEMPORAL * temporal)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceSignals:
    """Immutable container for a single scorer run.

    Attributes:
        amount_score:    Amount signal in [0, 1], 4 dp.
        temporal_score:  Temporal signal in [0, 1], 4 dp, or None if unavailable.
        composite:       Weighted composite in [0, 1], 4 dp.
        scorer_version:  Version tag (e.g. ``"v1"``).
        weights:         Mapping of signal name → Decimal weight.
    """

    amount_score: Decimal
    temporal_score: Decimal | None
    composite: Decimal
    scorer_version: str
    weights: dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_signals(
    charge_amount: Decimal,
    deposit_amount: Decimal,
    charge_date: date | None,
    deposit_date: date | None,
) -> ConfidenceSignals:
    """Compute all confidence signals for a (charge, deposit) pair.

    Args:
        charge_amount:  Stripe charge gross amount (``Decimal``).
        deposit_amount: NetSuite deposit/payment amount (``Decimal``).
        charge_date:    Date of the Stripe charge, or ``None``.
        deposit_date:   Date of the NS payment/deposit, or ``None``.

    Returns:
        A frozen :class:`ConfidenceSignals` with all fields populated.
    """
    a_score = amount_score(charge_amount, deposit_amount)
    t_score = temporal_score(charge_date, deposit_date)
    c_score = composite(a_score, t_score)

    return ConfidenceSignals(
        amount_score=a_score,
        temporal_score=t_score,
        composite=c_score,
        scorer_version=SCORER_VERSION,
        weights={"amount": W_AMOUNT, "temporal": W_TEMPORAL},
    )


def signals_to_evidence(signals: ConfidenceSignals) -> dict:
    """Serialize *signals* to a JSON-safe dict for persisting into an evidence column.

    All ``Decimal`` values are stringified. ``temporal_score`` is ``None`` when
    unavailable. ``weights`` values are also strings (``"0.6"``, ``"0.4"``).

    The returned dict passes ``json.dumps(...)`` without raising.
    """
    return {
        "amount_score": str(signals.amount_score),
        "temporal_score": str(signals.temporal_score) if signals.temporal_score is not None else None,
        "composite": str(signals.composite),
        "scorer_version": signals.scorer_version,
        "weights": {
            "amount": str(signals.weights["amount"]),
            "temporal": str(signals.weights["temporal"]),
        },
    }
