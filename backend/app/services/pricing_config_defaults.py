"""Default pricing configuration — 16 currencies with production FX rates."""

import copy

DEFAULT_PRICING_CONFIG = {
    "version": 1,
    "base_currency": "USD",
    "eur_fx_rate": "0.92",
    "currencies": {
        "GBP": {"fx_rate": "0.79", "tier": "usd_based", "vat_rate": "0.20", "rounding_rule": "nearest_9"},
        "CAD": {"fx_rate": "1.36", "tier": "usd_based", "vat_rate": None, "rounding_rule": "nearest_9"},
        "AUD": {"fx_rate": "1.53", "tier": "usd_based", "vat_rate": "0.10", "rounding_rule": "nearest_9"},
        "JPY": {"fx_rate": "149.50", "tier": "usd_based", "vat_rate": "0.10", "rounding_rule": "nearest_100"},
        "KRW": {"fx_rate": "1331.00", "tier": "usd_based", "vat_rate": "0.10", "rounding_rule": "nearest_990"},
        "INR": {"fx_rate": "83.10", "tier": "usd_based", "vat_rate": "0.18", "rounding_rule": "nearest_50"},
        "AED": {"fx_rate": "3.67", "tier": "usd_based", "vat_rate": "0.05", "rounding_rule": "nearest_9"},
        "EUR": {"fx_rate": "1.00", "tier": "eur_based", "vat_rate": "0.23", "rounding_rule": "nearest_9"},
        "SEK": {"fx_rate": "11.29", "tier": "eur_based", "vat_rate": "0.25", "rounding_rule": "nearest_9"},
        "NOK": {"fx_rate": "11.42", "tier": "eur_based", "vat_rate": "0.25", "rounding_rule": "nearest_9"},
        "DKK": {"fx_rate": "7.46", "tier": "eur_based", "vat_rate": "0.25", "rounding_rule": "nearest_9"},
        "PLN": {"fx_rate": "4.32", "tier": "eur_based", "vat_rate": "0.23", "rounding_rule": "nearest_9"},
        "CZK": {"fx_rate": "24.58", "tier": "eur_based", "vat_rate": "0.21", "rounding_rule": "nearest_9"},
        "CHF": {"fx_rate": "0.94", "tier": "eur_based", "vat_rate": "0.081", "rounding_rule": "nearest_9"},
        "HUF": {"fx_rate": "390.00", "tier": "eur_based", "vat_rate": "0.27", "rounding_rule": "nearest_990"},
        "RON": {"fx_rate": "4.97", "tier": "eur_based", "vat_rate": "0.19", "rounding_rule": "nearest_9"},
    },
}


def get_default_config() -> dict:
    """Return the default pricing config dict (safe to mutate — returns a copy)."""
    return copy.deepcopy(DEFAULT_PRICING_CONFIG)
