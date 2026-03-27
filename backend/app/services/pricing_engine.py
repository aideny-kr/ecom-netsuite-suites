"""Deterministic pricing engine — currency conversion, VAT, and charm-price rounding.

All arithmetic uses ``Decimal`` (never ``float``).  Every function is pure —
no database, no async, no side effects.
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.schemas.pricing import (
    CurrencyConfig,
    CurrencyResult,
    PricingInput,
    PricingOutput,
    TenantPricingConfig,
)

# ---------------------------------------------------------------------------
# Rounding helpers (standalone pure functions)
# ---------------------------------------------------------------------------

def round_nearest_9(value: Decimal) -> Decimal:
    """Round UP to nearest integer ending in 9."""
    n = int(math.ceil(value))
    remainder = n % 10
    if remainder <= 9:
        n = n + (9 - remainder)
    return Decimal(n)


def round_nearest_100(value: Decimal) -> Decimal:
    """Standard rounding to nearest 100 (ROUND_HALF_UP)."""
    return (value / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 100


def round_nearest_990(value: Decimal) -> Decimal:
    """Round to nearest X490 or X990 charm-price point.

    Values close to a 1000 boundary (offset < 100) snap to the previous
    block's X990 point, otherwise snap up to X490 or X990 within the block.
    """
    n = int(math.ceil(value))
    base_1000 = (n // 1000) * 1000
    offset = n - base_1000
    if offset < 100 and base_1000 >= 10:
        return Decimal(base_1000 - 10)
    elif offset <= 490:
        return Decimal(base_1000 + 490)
    else:
        return Decimal(base_1000 + 990)


def round_nearest_50(value: Decimal) -> Decimal:
    """Round to nearest 50.

    Fractional values (non-integer) round UP (ceiling) to the next 50
    boundary; integer values use standard ROUND_HALF_UP.
    """
    if value % 50 == 0:
        return value
    if value != value.to_integral_value():
        return (value / 50).quantize(Decimal("1"), rounding=ROUND_CEILING) * 50
    return (value / 50).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 50


# Dispatch table
_ROUNDING_FNS: dict[str, callable] = {
    "nearest_9": round_nearest_9,
    "nearest_100": round_nearest_100,
    "nearest_990": round_nearest_990,
    "nearest_50": round_nearest_50,
    "no_rounding": lambda v: v,
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class PricingEngine:
    """Stateless pricing engine.  Instantiate and call methods."""

    def convert_single(
        self,
        usd_price: Decimal,
        currency: str,
        config: CurrencyConfig,
        eur_fx_rate: Decimal,
    ) -> CurrencyResult:
        """Convert a single USD price to the target currency."""
        # Step 1: FX conversion
        if config.tier == "usd_based":
            converted = usd_price * config.fx_rate
        else:
            # Tier 2: USD → EUR → local
            eur_amount = usd_price * eur_fx_rate
            converted = eur_amount * config.fx_rate

        # Step 2: VAT (applied after FX, before rounding)
        if config.vat_rate is not None:
            vat_amount = converted * config.vat_rate
            pre_round = converted + vat_amount
        else:
            vat_amount = None
            pre_round = converted

        # Step 3: Rounding (only at the final step)
        rounding_fn = _ROUNDING_FNS[config.rounding_rule]
        final_price = rounding_fn(pre_round)

        return CurrencyResult(
            currency=currency,
            fx_rate=config.fx_rate,
            tier=config.tier,
            converted_amount=converted,
            vat_rate=config.vat_rate,
            vat_amount=vat_amount,
            pre_round_amount=pre_round,
            final_price=final_price,
            rounding_rule=config.rounding_rule,
        )

    def convert_batch(
        self,
        items: list[PricingInput],
        config: TenantPricingConfig,
    ) -> list[PricingOutput]:
        """Convert a list of items across all configured currencies."""
        outputs: list[PricingOutput] = []
        for item in items:
            results: dict[str, CurrencyResult] = {}
            for currency_code, currency_config in config.currencies.items():
                results[currency_code] = self.convert_single(
                    usd_price=item.usd_price,
                    currency=currency_code,
                    config=currency_config,
                    eur_fx_rate=config.eur_fx_rate,
                )
            outputs.append(
                PricingOutput(
                    sku=item.sku,
                    item_name=item.item_name,
                    usd_price=item.usd_price,
                    results=results,
                )
            )
        return outputs
