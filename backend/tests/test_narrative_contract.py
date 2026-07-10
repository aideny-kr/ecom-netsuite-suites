from app.services.reconciliation.narrative_contract import (
    narrative_respects_evidence,
    numeric_tokens,
)


def test_numeric_tokens_normalizes():
    assert numeric_tokens("Fee of $1,284.55 across 3 payouts") == {"1284.55", "3"}
    assert numeric_tokens("no numbers here") == set()
    assert numeric_tokens("Charge ch_123abc for R628489275") == set()  # ids are not numbers


def test_respects_when_all_numbers_from_evidence():
    ev = ["variance_amount=77.10", "stripe_amount=500.00", "order R628489275"]
    assert (
        narrative_respects_evidence("Variance of $77.10 against a $500.00 charge — unexplained residual.", ev) is True
    )


def test_violates_on_invented_number():
    ev = ["variance_amount=77.10"]
    assert narrative_respects_evidence("Roughly $80 of unexplained variance.", ev) is False


def test_empty_narrative_ok():
    assert narrative_respects_evidence("", ["x=1"]) is True
