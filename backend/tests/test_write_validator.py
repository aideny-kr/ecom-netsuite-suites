import pytest
from pydantic import ValidationError

from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata
from app.services.chat.write_payload import NormalizedPayload
from app.services.chat.write_validator import ValidationResult, validate_write

META = RecordMetadata(
    record_type="customer",
    fields=[
        FieldSpec(name="companyname", label="Company Name", required=False),
        FieldSpec(
            name="subsidiary",
            label="Primary Subsidiary",
            required=True,
            type="select",
            options=[{"value": "1", "label": "Framework Inc"}],
        ),
    ],
)


def test_missing_required_header_field_is_flagged():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "test ai customer"}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is False
    assert result.missing_required == ["subsidiary"]
    assert result.editable_slots[0].name == "subsidiary"
    assert result.editable_slots[0].label == "Primary Subsidiary"
    assert result.editable_slots[0].allowed == [{"value": "1", "label": "Framework Inc"}]


def test_complete_payload_passes():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x", "subsidiary": "1"}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is True
    assert result.editable_slots == []


def test_missing_line_required_field_is_flagged():
    meta = RecordMetadata(
        record_type="journalEntry",
        fields=[],
        line_fields=[FieldSpec(name="account", label="Account", required=True)],
    )
    result = validate_write(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 5}]),
        metadata=meta,
        record_type="journalEntry",
        mutation_type="create",
    )
    assert result.ok is False
    assert "line[0].account" in result.missing_line_required


def test_no_metadata_marks_unvalidated_but_not_invalid():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x"}, lines=[]),
        metadata=None,
        record_type="customer",
        mutation_type="create",
    )
    assert result.unvalidated is True
    assert result.ok is True


def test_update_does_not_require_untouched_fields():
    """A partial update must not demand every required field be resent."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "renamed"}, lines=[], record_id="7"),
        metadata=META,
        record_type="customer",
        mutation_type="update",
    )
    assert result.ok is True


def test_fingerprint_is_stable_and_distinguishing():
    a = validate_write(
        payload=NormalizedPayload(fields={}, lines=[]), metadata=META, record_type="customer", mutation_type="create"
    )
    b = validate_write(
        payload=NormalizedPayload(fields={}, lines=[]), metadata=META, record_type="customer", mutation_type="create"
    )
    c = validate_write(
        payload=NormalizedPayload(fields={"subsidiary": "1"}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_ok_true_with_missing_required_is_rejected():
    """A future caller must not be able to hand-construct a lying result."""
    with pytest.raises(ValidationError):
        ValidationResult(ok=True, missing_required=["subsidiary"])


def test_ok_consistent_with_empty_lists_still_constructs():
    result = ValidationResult(ok=True)
    assert result.ok is True
    assert result.missing_required == []


def test_no_metadata_with_invariant_errors_is_not_ok():
    """Unknown field requirements PLUS a known invariant violation: still unvalidated, but not ok."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x"}, lines=[]),
        metadata=None,
        record_type="customer",
        mutation_type="create",
        invariant_errors=["debits must equal credits"],
    )
    assert result.unvalidated is True
    assert result.ok is False


@pytest.mark.parametrize("empty_value", ["", "   ", None])
def test_missing_required_header_field_flagged_when_value_is_empty(empty_value):
    """Key presence alone must not satisfy a required field — the agent can
    otherwise send `{"subsidiary": ""}` and clear the missing-field check
    without ever supplying a usable value."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "test ai customer", "subsidiary": empty_value}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is False
    assert result.missing_required == ["subsidiary"]


@pytest.mark.parametrize("falsy_but_valid", [0, 0.0, False])
def test_missing_required_header_field_not_flagged_for_falsy_values(falsy_but_valid):
    """0, 0.0 and False are legitimate NetSuite field values and must not be
    treated as missing — regression guard against bare `if not value`."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "test ai customer", "subsidiary": falsy_but_valid}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is True
    assert result.missing_required == []


def test_missing_required_header_field_not_flagged_for_real_value():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x", "subsidiary": "1"}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is True
    assert result.missing_required == []


@pytest.mark.parametrize("empty_value", ["", "   ", None])
def test_missing_line_required_field_flagged_when_value_is_empty(empty_value):
    meta = RecordMetadata(
        record_type="journalEntry",
        fields=[],
        line_fields=[FieldSpec(name="account", label="Account", required=True)],
    )
    result = validate_write(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 5, "account": empty_value}]),
        metadata=meta,
        record_type="journalEntry",
        mutation_type="create",
    )
    assert result.ok is False
    assert "line[0].account" in result.missing_line_required


@pytest.mark.parametrize("falsy_but_valid", [0, 0.0, False])
def test_missing_line_required_field_not_flagged_for_falsy_values(falsy_but_valid):
    meta = RecordMetadata(
        record_type="journalEntry",
        fields=[],
        line_fields=[FieldSpec(name="account", label="Account", required=True)],
    )
    result = validate_write(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 5, "account": falsy_but_valid}]),
        metadata=meta,
        record_type="journalEntry",
        mutation_type="create",
    )
    assert result.ok is True
    assert result.missing_line_required == []


def test_update_with_empty_required_field_still_skips_sweep_entirely():
    """An update legitimately sends a partial payload — even an explicitly
    empty value on a required field must not trigger the sweep, because
    demanding the full set would break a simple rename."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "renamed", "subsidiary": ""}, lines=[], record_id="7"),
        metadata=META,
        record_type="customer",
        mutation_type="update",
    )
    assert result.ok is True
    assert result.missing_required == []


def test_missing_line_required_fields_never_become_editable_slots():
    """Finding B6: line-level required fields (debit/credit) must remain
    structurally unreachable via slot_values — editable_slots is built ONLY
    from top-level `missing`, never from `missing_line_required`. This is
    what keeps the merge-then-execute path (Finding B/D) from becoming a
    balance-bypass channel: a human can never fill a missing line field
    through the slot mechanism, only through the agent re-composing the
    write with a complete payload."""
    meta = RecordMetadata(
        record_type="journalEntry",
        fields=[FieldSpec(name="subsidiary", label="Subsidiary", required=True)],
        line_fields=[FieldSpec(name="account", label="Account", required=True)],
    )
    result = validate_write(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 5}]),
        metadata=meta,
        record_type="journalEntry",
        mutation_type="create",
    )
    assert result.missing_required == ["subsidiary"]
    assert "line[0].account" in result.missing_line_required
    # The line-level miss is terminal (unfillable_line_fields on the card),
    # NOT a slot — only "subsidiary" (top-level) may ever appear here.
    assert [s.name for s in result.editable_slots] == ["subsidiary"]
