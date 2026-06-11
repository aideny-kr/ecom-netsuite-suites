"""v1 autonomy envelope (Bet 3 Rung 1): ONLY deterministic, zero-variance,
bucket='matches', non-terminal rows qualify. Everything else is excluded with
a tallied reason. Pure function — these tests use unsaved ORM objects."""

import uuid
from decimal import Decimal

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.autonomy_envelope import ENVELOPE_VERSION, evaluate


def _result(**overrides) -> ReconciliationResult:
    defaults = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        match_type="deterministic",
        status="suggested",
        bucket="matches",
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("100.00"),
        currency="USD",
    )
    defaults.update(overrides)
    return ReconciliationResult(**defaults)


def test_qualifying_row_is_candidate():
    row = _result()
    report = evaluate([row])
    assert report.candidate_count == 1
    assert report.candidate_ids == (str(row.id),)
    assert report.candidate_total_amount == Decimal("100.00")
    assert report.excluded == {}
    assert report.envelope_version == ENVELOPE_VERSION


def test_exclusion_reasons_are_tallied():
    rows = [
        _result(status="approved"),  # terminal_status
        _result(status="locked"),  # terminal_status
        _result(bucket="needs_review"),  # bucket_not_matches
        _result(match_type="fuzzy", bucket="rules"),  # bucket_not_matches (bucket checked first)
        _result(variance_amount=Decimal("0.01"), bucket="matches"),  # has_variance
        _result(variance_amount=None, bucket="matches"),  # has_variance (None = unknown = out)
    ]
    report = evaluate(rows)
    assert report.candidate_count == 0
    assert report.excluded == {
        "terminal_status": 2,
        "bucket_not_matches": 2,
        "has_variance": 2,
    }


def test_non_deterministic_in_matches_bucket_is_excluded():
    # Defensive: bucket says 'matches' but match_type disagrees — match_type wins.
    report = evaluate([_result(match_type="fuzzy", bucket="matches")])
    assert report.candidate_count == 0
    assert report.excluded == {"not_deterministic": 1}


def test_total_amount_sums_candidates_only_and_payload_is_json_safe():
    rows = [
        _result(stripe_amount=Decimal("10.50")),
        _result(stripe_amount=Decimal("2.25")),
        _result(status="approved", stripe_amount=Decimal("999.99")),
    ]
    report = evaluate(rows)
    assert report.candidate_total_amount == Decimal("12.75")
    payload = report.to_payload()
    assert payload["candidate_total_amount"] == "12.75"  # str, not Decimal
    assert payload["candidate_count"] == 2
    assert isinstance(payload["candidate_ids"], list)


def test_empty_input():
    report = evaluate([])
    assert report.candidate_count == 0
    assert report.candidate_ids == ()
    assert report.candidate_total_amount == Decimal("0")


def test_payload_caps_candidate_ids_at_200():
    """Framework-scale runs have tens of thousands of qualifying rows — the
    audit payload (also copied into jobs.result_summary) must stay bounded.
    Counts and totals remain exact; ids are a capped sample."""
    rows = [_result() for _ in range(201)]
    report = evaluate(rows)
    payload = report.to_payload()
    assert report.candidate_count == 201
    assert payload["candidate_count"] == 201
    assert len(payload["candidate_ids"]) == 200
    assert payload["candidate_ids_truncated"] is True


def test_payload_not_truncated_below_cap():
    payload = evaluate([_result()]).to_payload()
    assert payload["candidate_ids_truncated"] is False
    assert len(payload["candidate_ids"]) == 1


def test_amount_unknown_rows_are_excluded_not_zero_blessed():
    """A row with stripe_amount=None must not count as a $0 candidate — that
    would silently understate the dollar exposure the envelope caps will be
    calibrated against. It gets its own exclusion reason."""
    report = evaluate([_result(stripe_amount=None)])
    assert report.candidate_count == 0
    assert report.excluded == {"amount_unknown": 1}
