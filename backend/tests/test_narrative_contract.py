from app.services.reconciliation.narrative_contract import (
    narrative_respects_evidence,
    numeric_tokens,
)


def test_numeric_tokens_normalizes():
    assert numeric_tokens("Fee of $1,284.55 across 3 payouts") == {"1284.55", "3"}
    assert numeric_tokens("no numbers here") == set()
    # Digit runs glued to letters (ids, currency-prefixed amounts) are now
    # extracted as candidate tokens too — narrative_respects_evidence decides
    # whether they're ALLOWED (a verbatim evidence number, or the whole word
    # appearing in evidence), numeric_tokens itself no longer exempts them.
    assert numeric_tokens("Charge ch_123abc for R628489275") == {"123", "628489275"}


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


def test_number_glued_to_letters_without_evidence_fails():
    """T2 gate fix: the old regex exempted any digit run glued to letters
    (e.g. "USD1284.55") from being a numeric token at all, so a fabricated
    figure could hide from the contract just by sitting next to a currency
    code or unit. It must now be extracted and rejected like any other
    invented number."""
    assert narrative_respects_evidence("Fee of USD1284.55 booked.", ["variance_amount=1.00"]) is False


def test_id_glued_number_passes_via_word_in_evidence():
    """An id like R628489275 is a digit run glued to letters, but it isn't a
    fabricated figure — it's allowed because the whole word appears verbatim
    in the evidence (the order reference), not because it normalizes to a
    number that matches."""
    ev = ["R628489275"]
    assert narrative_respects_evidence("See order R628489275 for detail.", ev) is True
