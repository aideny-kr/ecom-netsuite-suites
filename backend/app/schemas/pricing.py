"""Pydantic schemas for pricing engine — currency conversion, VAT, rounding."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Rounding rule identifiers
# ---------------------------------------------------------------------------
RoundingRule = Literal[
    "nearest_9",
    "nearest_100",
    "nearest_990",
    "nearest_50",
    "no_rounding",
]

ConversionTier = Literal["usd_based", "eur_based"]


# ---------------------------------------------------------------------------
# Per-currency configuration
# ---------------------------------------------------------------------------
class CurrencyConfig(BaseModel):
    """Configuration for a single target currency."""

    fx_rate: Decimal = Field(gt=0, description="Exchange rate from base currency")
    tier: ConversionTier
    vat_rate: Decimal | None = Field(
        default=None, ge=0, le=1, description="VAT/GST rate as decimal (0.20 = 20%). None = no VAT."
    )
    rounding_rule: RoundingRule = "nearest_9"

    @field_validator("fx_rate", mode="before")
    @classmethod
    def coerce_fx_rate(cls, v: object) -> Decimal:
        return Decimal(str(v))

    @field_validator("vat_rate", mode="before")
    @classmethod
    def coerce_vat_rate(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))


class TenantPricingConfig(BaseModel):
    """Full pricing configuration for a tenant."""

    version: int = 1
    base_currency: str = "USD"
    currencies: dict[str, CurrencyConfig]
    eur_fx_rate: Decimal = Field(
        default=Decimal("0.92"),
        gt=0,
        description="USD→EUR rate used for Tier 2 (EUR-based) currencies",
    )

    @field_validator("eur_fx_rate", mode="before")
    @classmethod
    def coerce_eur_fx_rate(cls, v: object) -> Decimal:
        return Decimal(str(v))


# ---------------------------------------------------------------------------
# Conversion input / output
# ---------------------------------------------------------------------------
class PricingInput(BaseModel):
    """Single item to convert."""

    sku: str = Field(min_length=1, max_length=100)
    item_name: str | None = None
    usd_price: Decimal = Field(ge=0)

    @field_validator("usd_price", mode="before")
    @classmethod
    def coerce_usd_price(cls, v: object) -> Decimal:
        return Decimal(str(v))


class CurrencyResult(BaseModel):
    """Conversion result for a single currency."""

    currency: str
    fx_rate: Decimal
    tier: ConversionTier
    converted_amount: Decimal  # After FX, before VAT
    vat_rate: Decimal | None
    vat_amount: Decimal | None
    pre_round_amount: Decimal  # After VAT, before rounding
    final_price: Decimal  # After rounding
    rounding_rule: RoundingRule


class PricingOutput(BaseModel):
    """Full conversion result for a single item."""

    sku: str
    item_name: str | None
    usd_price: Decimal
    results: dict[str, CurrencyResult]  # currency_code → result


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------
class PricingConfigResponse(BaseModel):
    id: str
    tenant_id: str
    config: TenantPricingConfig
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PricingConfigUpdate(BaseModel):
    config: TenantPricingConfig


class PricingConvertRequest(BaseModel):
    file_id: str
    output_format: Literal["excel", "netsuite_csv"] = "excel"


class PricingConvertResponse(BaseModel):
    output_file_id: str
    sku_count: int
    currency_count: int
    summary: list[dict]  # First 5 rows preview
