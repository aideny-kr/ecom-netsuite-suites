"""TDD RED phase — pricing engine tests. Implementation does not exist yet."""

from decimal import Decimal

import pytest

from app.schemas.pricing import (
    CurrencyConfig,
    PricingInput,
    TenantPricingConfig,
)
from app.services.pricing_engine import (
    PricingEngine,
    round_nearest_9,
    round_nearest_50,
    round_nearest_100,
    round_nearest_990,
)


@pytest.fixture
def accounting_config():
    return TenantPricingConfig(
        base_currency="USD",
        eur_fx_rate=Decimal("0.92"),
        currencies={
            "GBP": CurrencyConfig(
                fx_rate=Decimal("0.79"), tier="usd_based", vat_rate=Decimal("0.20"), rounding_rule="nearest_9"
            ),
            "EUR": CurrencyConfig(
                fx_rate=Decimal("0.92"), tier="eur_based", vat_rate=Decimal("0.23"), rounding_rule="nearest_9"
            ),
            "CAD": CurrencyConfig(fx_rate=Decimal("1.36"), tier="usd_based", vat_rate=None, rounding_rule="nearest_9"),
            "AUD": CurrencyConfig(
                fx_rate=Decimal("1.53"), tier="usd_based", vat_rate=Decimal("0.10"), rounding_rule="nearest_9"
            ),
            "JPY": CurrencyConfig(
                fx_rate=Decimal("149.50"), tier="usd_based", vat_rate=Decimal("0.10"), rounding_rule="nearest_100"
            ),
            "KRW": CurrencyConfig(
                fx_rate=Decimal("1380.00"), tier="usd_based", vat_rate=Decimal("0.10"), rounding_rule="nearest_990"
            ),
            "INR": CurrencyConfig(
                fx_rate=Decimal("83.50"), tier="usd_based", vat_rate=Decimal("0.18"), rounding_rule="nearest_50"
            ),
            "AED": CurrencyConfig(
                fx_rate=Decimal("3.67"), tier="usd_based", vat_rate=Decimal("0.05"), rounding_rule="nearest_9"
            ),
            "SEK": CurrencyConfig(
                fx_rate=Decimal("11.20"), tier="eur_based", vat_rate=Decimal("0.25"), rounding_rule="nearest_9"
            ),
            "NOK": CurrencyConfig(
                fx_rate=Decimal("11.50"), tier="eur_based", vat_rate=Decimal("0.25"), rounding_rule="nearest_9"
            ),
            "DKK": CurrencyConfig(
                fx_rate=Decimal("7.45"), tier="eur_based", vat_rate=Decimal("0.25"), rounding_rule="nearest_9"
            ),
            "PLN": CurrencyConfig(
                fx_rate=Decimal("4.32"), tier="eur_based", vat_rate=Decimal("0.23"), rounding_rule="nearest_9"
            ),
            "CZK": CurrencyConfig(
                fx_rate=Decimal("25.10"), tier="eur_based", vat_rate=Decimal("0.21"), rounding_rule="nearest_9"
            ),
            "CHF": CurrencyConfig(
                fx_rate=Decimal("0.94"), tier="eur_based", vat_rate=Decimal("0.081"), rounding_rule="nearest_9"
            ),
            "HUF": CurrencyConfig(
                fx_rate=Decimal("395.00"), tier="eur_based", vat_rate=Decimal("0.27"), rounding_rule="nearest_990"
            ),
            "RON": CurrencyConfig(
                fx_rate=Decimal("4.97"), tier="eur_based", vat_rate=Decimal("0.19"), rounding_rule="nearest_9"
            ),
        },
    )


class TestRoundingFunctions:
    def test_nearest_9_basic(self):
        assert round_nearest_9(Decimal("1572.73")) == 1579

    def test_nearest_9_already_ends_9(self):
        assert round_nearest_9(Decimal("1579")) == 1579

    def test_nearest_9_just_above(self):
        assert round_nearest_9(Decimal("1580.01")) == 1589

    def test_nearest_9_small(self):
        assert round_nearest_9(Decimal("12.50")) == 19

    def test_nearest_9_zero(self):
        assert round_nearest_9(Decimal("0")) == 9

    def test_nearest_100_basic(self):
        assert round_nearest_100(Decimal("248525")) == 248500

    def test_nearest_100_exact(self):
        assert round_nearest_100(Decimal("248500")) == 248500

    def test_nearest_100_round_up(self):
        assert round_nearest_100(Decimal("248550")) == 248600

    def test_nearest_100_small(self):
        assert round_nearest_100(Decimal("50")) == 100

    def test_nearest_990_low(self):
        assert round_nearest_990(Decimal("2153200")) == 2153490

    def test_nearest_990_high(self):
        assert round_nearest_990(Decimal("2153600")) == 2153990

    def test_nearest_990_exact_990(self):
        assert round_nearest_990(Decimal("2153990")) == 2153990

    def test_nearest_990_exact_490(self):
        assert round_nearest_990(Decimal("2153490")) == 2153490

    def test_nearest_990_boundary(self):
        assert round_nearest_990(Decimal("2153491")) == 2153990

    def test_nearest_50_basic(self):
        assert round_nearest_50(Decimal("138227")) == 138250

    def test_nearest_50_exact(self):
        assert round_nearest_50(Decimal("138250")) == 138250

    def test_nearest_50_round_down(self):
        assert round_nearest_50(Decimal("138224")) == 138200


class TestTier1Conversion:
    """FRANVD0009 at $1,659 — USD-based currencies."""

    def test_gbp_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("1579")

    def test_cad_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "CAD",
            accounting_config.currencies["CAD"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("2259")

    def test_aud_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "AUD",
            accounting_config.currencies["AUD"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("2799")

    def test_jpy_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "JPY",
            accounting_config.currencies["JPY"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("272800")

    def test_krw_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "KRW",
            accounting_config.currencies["KRW"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("2518490")

    def test_inr_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "INR",
            accounting_config.currencies["INR"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("163450")

    def test_aed_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "AED",
            accounting_config.currencies["AED"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("6399")


class TestTier2Conversion:
    """FRANVD0009 at $1,659 — EUR-based currencies."""

    def test_sek_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "SEK",
            accounting_config.currencies["SEK"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("21049")

    def test_nok_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "NOK",
            accounting_config.currencies["NOK"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("21609")

    def test_dkk_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "DKK",
            accounting_config.currencies["DKK"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("13999")

    def test_pln_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "PLN",
            accounting_config.currencies["PLN"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("8119")

    def test_czk_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "CZK",
            accounting_config.currencies["CZK"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("47169")

    def test_chf_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "CHF",
            accounting_config.currencies["CHF"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("1769")

    def test_huf_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "HUF",
            accounting_config.currencies["HUF"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("742490")

    def test_ron_conversion(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "RON",
            accounting_config.currencies["RON"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("9339")


class TestSecondSKU:
    """FRANKK00A1 at $199."""

    def test_gbp_199(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("199"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("189")

    def test_jpy_199(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("199"),
            "JPY",
            accounting_config.currencies["JPY"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("32700")

    def test_krw_199(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("199"),
            "KRW",
            accounting_config.currencies["KRW"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price == Decimal("302490")

    def test_sek_199(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("199"),
            "SEK",
            accounting_config.currencies["SEK"],
            accounting_config.eur_fx_rate,
            eur_config=accounting_config.currencies["EUR"],
        )
        assert result.final_price == Decimal("2569")


class TestBatchConversion:
    def test_batch_two_skus(self, accounting_config):
        engine = PricingEngine()
        items = [
            PricingInput(sku="FRANVD0009", usd_price=Decimal("1659")),
            PricingInput(sku="FRANKK00A1", usd_price=Decimal("199")),
        ]
        results = engine.convert_batch(items, accounting_config)
        assert len(results) == 2
        assert results[0].sku == "FRANVD0009"
        assert results[1].sku == "FRANKK00A1"
        assert len(results[0].results) == 16
        assert len(results[1].results) == 16

    def test_batch_empty(self, accounting_config):
        engine = PricingEngine()
        results = engine.convert_batch([], accounting_config)
        assert results == []

    def test_batch_single(self, accounting_config):
        engine = PricingEngine()
        items = [PricingInput(sku="FRANVD0009", usd_price=Decimal("1659"))]
        results = engine.convert_batch(items, accounting_config)
        assert len(results) == 1
        assert len(results[0].results) == 16


class TestEdgeCases:
    def test_zero_price(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("0"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert result.converted_amount == Decimal("0")

    def test_one_cent(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("0.01"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price >= 0  # No crash

    def test_large_price(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("99999"),
            "JPY",
            accounting_config.currencies["JPY"],
            accounting_config.eur_fx_rate,
        )
        assert result.final_price > 0  # No overflow

    def test_missing_currency_in_batch(self):
        config = TenantPricingConfig(
            currencies={
                "GBP": CurrencyConfig(
                    fx_rate=Decimal("0.79"),
                    tier="usd_based",
                    vat_rate=Decimal("0.20"),
                    rounding_rule="nearest_9",
                )
            }
        )
        engine = PricingEngine()
        items = [PricingInput(sku="TEST", usd_price=Decimal("100"))]
        results = engine.convert_batch(items, config)
        assert len(results[0].results) == 1
        assert "GBP" in results[0].results

    def test_no_vat_currency(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "CAD",
            accounting_config.currencies["CAD"],
            accounting_config.eur_fx_rate,
        )
        assert result.vat_amount is None
        assert result.vat_rate is None

    def test_decimal_precision(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert isinstance(result.converted_amount, Decimal)
        assert isinstance(result.final_price, Decimal)


class TestUpliftByCurrency:
    """Per-currency multiplicative uplift applied PRE-rounding so charm-price
    rules still terminate on the charm digits (e.g. nearest_9, nearest_990)."""

    def test_none_preserves_existing_behavior(self, accounting_config):
        """convert_batch with no uplift kwarg matches previous output bit-for-bit."""
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("1659"))]
        baseline = engine.convert_batch(items, accounting_config)
        with_kwarg = engine.convert_batch(items, accounting_config, uplift_by_currency=None)
        assert with_kwarg[0].results["GBP"].final_price == baseline[0].results["GBP"].final_price
        assert with_kwarg[0].results["JPY"].final_price == baseline[0].results["JPY"].final_price

    def test_uplift_applied_pre_rounding_nearest_9(self):
        """Base 100 + 5% uplift = 105, then round_9 = 109. Charm digit preserved."""
        config = TenantPricingConfig(
            currencies={
                "TST": CurrencyConfig(
                    fx_rate=Decimal("1.0"),
                    tier="usd_based",
                    vat_rate=None,
                    rounding_rule="nearest_9",
                ),
            }
        )
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("100"))]
        results = engine.convert_batch(
            items, config, uplift_by_currency={"TST": Decimal("0.05")}
        )
        # Uplift pre-round: 100 * 1.05 = 105 → round_9(105) = 109.
        # Post-round (the bug we're avoiding): round_9(100) * 1.05 = 109 * 1.05 = 114.45.
        assert results[0].results["TST"].final_price == Decimal("109")

    def test_uplift_applied_pre_rounding_nearest_50(self):
        """nearest_50 uplift lands on a 50-multiple."""
        config = TenantPricingConfig(
            currencies={
                "TST": CurrencyConfig(
                    fx_rate=Decimal("1.0"),
                    tier="usd_based",
                    vat_rate=None,
                    rounding_rule="nearest_50",
                ),
            }
        )
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("5197"))]
        results = engine.convert_batch(
            items, config, uplift_by_currency={"TST": Decimal("0.05")}
        )
        # 5197 * 1.05 = 5456.85 → nearest_50 = 5450.
        assert results[0].results["TST"].final_price == Decimal("5450")
        # And the result IS a 50-multiple.
        assert results[0].results["TST"].final_price % 50 == 0

    def test_uplift_applied_pre_rounding_nearest_990(self):
        """nearest_990 with uplift still terminates on X490 / X990."""
        config = TenantPricingConfig(
            currencies={
                "TST": CurrencyConfig(
                    fx_rate=Decimal("1.0"),
                    tier="usd_based",
                    vat_rate=None,
                    rounding_rule="nearest_990",
                ),
            }
        )
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("1000"))]
        results = engine.convert_batch(
            items, config, uplift_by_currency={"TST": Decimal("0.05")}
        )
        # 1000 * 1.05 = 1050 → offset 50, ≤ 490 → base_1000 + 490 = 1490.
        final = results[0].results["TST"].final_price
        assert final == Decimal("1490")
        # Charm pattern preserved: ends in 490 or 990.
        assert int(final) % 1000 in (490, 990)

    def test_uplift_negative_value(self):
        """Negative uplift = discount — also pre-rounding."""
        config = TenantPricingConfig(
            currencies={
                "TST": CurrencyConfig(
                    fx_rate=Decimal("1.0"),
                    tier="usd_based",
                    vat_rate=None,
                    rounding_rule="nearest_9",
                ),
            }
        )
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("100"))]
        results = engine.convert_batch(
            items, config, uplift_by_currency={"TST": Decimal("-0.10")}
        )
        # 100 * 0.90 = 90 → round_9(90) = 99.
        assert results[0].results["TST"].final_price == Decimal("99")

    def test_uplift_multi_currency_independent(self, accounting_config):
        """{GBP: 0.05, EUR: -0.10} affects GBP and EUR; CAD untouched."""
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("1659"))]
        baseline = engine.convert_batch(items, accounting_config)
        results = engine.convert_batch(
            items,
            accounting_config,
            uplift_by_currency={"GBP": Decimal("0.05"), "EUR": Decimal("-0.10")},
        )
        # GBP went up.
        assert results[0].results["GBP"].final_price > baseline[0].results["GBP"].final_price
        # EUR went down.
        assert results[0].results["EUR"].final_price < baseline[0].results["EUR"].final_price
        # CAD unchanged.
        assert results[0].results["CAD"].final_price == baseline[0].results["CAD"].final_price

    def test_uplift_unknown_currency_no_op(self, accounting_config):
        """Uplift for a currency not in the config does not crash and does not
        affect any other currency's output."""
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("1659"))]
        baseline = engine.convert_batch(items, accounting_config)
        results = engine.convert_batch(
            items, accounting_config, uplift_by_currency={"XYZ": Decimal("0.05")}
        )
        for code in baseline[0].results:
            assert results[0].results[code].final_price == baseline[0].results[code].final_price

    def test_uplift_empty_dict_is_noop(self, accounting_config):
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("1659"))]
        baseline = engine.convert_batch(items, accounting_config)
        results = engine.convert_batch(items, accounting_config, uplift_by_currency={})
        for code in baseline[0].results:
            assert results[0].results[code].final_price == baseline[0].results[code].final_price

    def test_uplift_eur_based_currency_pre_rounding(self, accounting_config):
        """Tier-2 (EUR-based) uplift lands on charm digits via local rounding."""
        engine = PricingEngine()
        items = [PricingInput(sku="X", usd_price=Decimal("1659"))]
        baseline = engine.convert_batch(items, accounting_config)
        results = engine.convert_batch(
            items, accounting_config, uplift_by_currency={"SEK": Decimal("0.05")}
        )
        # SEK rounds nearest_9 — the uplift output must still end in 9.
        assert int(results[0].results["SEK"].final_price) % 10 == 9
        # And it must be larger than baseline (positive uplift).
        assert results[0].results["SEK"].final_price > baseline[0].results["SEK"].final_price


class TestConvertSingle:
    def test_single_usd_to_gbp(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert result.currency == "GBP"
        assert result.fx_rate == Decimal("0.79")
        assert result.tier == "usd_based"
        assert result.rounding_rule == "nearest_9"

    def test_single_audit_trail(self, accounting_config):
        engine = PricingEngine()
        result = engine.convert_single(
            Decimal("1659"),
            "GBP",
            accounting_config.currencies["GBP"],
            accounting_config.eur_fx_rate,
        )
        assert result.converted_amount == Decimal("1659") * Decimal("0.79")
        assert result.vat_rate == Decimal("0.20")
        assert result.vat_amount is not None
        assert result.pre_round_amount > result.converted_amount  # VAT adds to it
        assert result.final_price == Decimal("1579")
