"""Accept a rounding difference only where a config says so (Aiden, 2026-10-07).

Per config and off by default: `mapping_json.rounding_tolerance`, at most 5 minor units of
the order's currency. Reconciliation applies it only when the order total AND the tax are
both within it and refunds agree exactly, and records it as an explicit adjustment.
"""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.schemas.transaction_ops import EvidenceModel

MAX_MINOR_UNITS = 5


class RoundingTolerance(EvidenceModel):
    schema_version: Literal[1]
    minor_units: int = Field(ge=1, le=MAX_MINOR_UNITS, strict=True)


def configured_tolerance(config, precision: int) -> tuple[int, Decimal] | None:
    """(minor units, amount in the currency) when the config sets a valid tolerance, else None.

    An absent or invalid setting never widens a match.
    """
    try:
        value = (config.get("mapping_json") or {}).get("rounding_tolerance")
        if value is None:
            return None
        tolerance = RoundingTolerance.model_validate(value)
        return tolerance.minor_units, Decimal(tolerance.minor_units).scaleb(-precision)
    except (ValueError, TypeError, AttributeError):
        return None
