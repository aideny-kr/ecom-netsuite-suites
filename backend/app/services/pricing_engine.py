"""Deterministic pricing engine — currency conversion, VAT, and charm-price rounding.

All arithmetic uses ``Decimal`` (never ``float``).  Every function is pure —
no database, no async, no side effects.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

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

    Simple rule: offset <= 490 → X490, else → X990.
    """
    n = int(math.ceil(value))
    base_1000 = (n // 1000) * 1000
    offset = n - base_1000
    if offset <= 490:
        return Decimal(base_1000 + 490)
    else:
        return Decimal(base_1000 + 990)


def round_nearest_50(value: Decimal) -> Decimal:
    """Round to nearest 50 (ROUND_HALF_UP consistently)."""
    if value % 50 == 0:
        return value
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
        eur_config: CurrencyConfig | None = None,
        *,
        uplift: Decimal | None = None,
    ) -> CurrencyResult:
        """Convert a single USD price to the target currency.

        For eur_based currencies: VAT is applied at the EUR intermediate step
        (using EUR's VAT rate), rounded, then multiplied by local FX rate with
        NO local VAT. This matches the 2-tier conversion model:
          Step 1: USD → EUR with EUR VAT → round EUR
          Step 2: Rounded EUR × local FX → round local (no local VAT)

        ``uplift`` (Decimal, e.g. ``Decimal("0.05")`` for +5%) is applied to
        the post-VAT, pre-rounding value. This keeps charm-price rounding
        rules (``nearest_9``, ``nearest_990``, ``nearest_50``) terminating on
        their charm digits even after a "+5% on GBP" override.
        """
        if config.tier == "usd_based":
            # USD-based: direct FX + local VAT
            converted = usd_price * config.fx_rate
            if config.vat_rate is not None:
                vat_amount = converted * config.vat_rate
                pre_round = converted + vat_amount
            else:
                vat_amount = None
                pre_round = converted
        else:
            # EUR-based: USD → EUR with EUR VAT → round → local FX → round
            eur_amount = usd_price * eur_fx_rate

            # Apply EUR's VAT at intermediate step
            eur_vat_rate = eur_config.vat_rate if eur_config and eur_config.vat_rate else None
            if eur_vat_rate is not None:
                eur_with_vat = eur_amount + (eur_amount * eur_vat_rate)
            else:
                eur_with_vat = eur_amount

            # Round EUR intermediate price
            eur_rounding = eur_config.rounding_rule if eur_config else config.rounding_rule
            eur_rounded = _ROUNDING_FNS[eur_rounding](eur_with_vat)

            # Convert rounded EUR to local currency (no local VAT)
            converted = eur_rounded * config.fx_rate
            vat_amount = eur_amount * eur_vat_rate if eur_vat_rate else None
            pre_round = converted

        if uplift is not None:
            pre_round = pre_round * (Decimal("1") + uplift)

        rounding_fn = _ROUNDING_FNS[config.rounding_rule]
        final_price = rounding_fn(pre_round)

        return CurrencyResult(
            currency=currency,
            fx_rate=config.fx_rate,
            tier=config.tier,
            converted_amount=converted,
            vat_rate=config.vat_rate if config.tier == "usd_based" else (eur_config.vat_rate if eur_config else None),
            vat_amount=vat_amount,
            pre_round_amount=pre_round,
            final_price=final_price,
            rounding_rule=config.rounding_rule,
        )

    def convert_batch(
        self,
        items: list[PricingInput],
        config: TenantPricingConfig,
        *,
        uplift_by_currency: dict[str, Decimal] | None = None,
    ) -> list[PricingOutput]:
        """Convert a list of items across all configured currencies.

        ``uplift_by_currency`` is a per-currency multiplicative adjustment
        applied PRE-rounding (e.g. ``{"GBP": Decimal("0.05")}`` adds 5% to GBP
        prior to the rounding rule). Currencies absent from the dict are
        unaffected.
        """
        # Get EUR config for 2-tier VAT application
        eur_config = config.currencies.get("EUR")
        uplift_by_currency = uplift_by_currency or {}

        outputs: list[PricingOutput] = []
        for item in items:
            results: dict[str, CurrencyResult] = {}
            for currency_code, currency_config in config.currencies.items():
                results[currency_code] = self.convert_single(
                    usd_price=item.usd_price,
                    currency=currency_code,
                    config=currency_config,
                    eur_fx_rate=config.eur_fx_rate,
                    eur_config=eur_config,
                    uplift=uplift_by_currency.get(currency_code),
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
