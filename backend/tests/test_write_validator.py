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


def test_requirements_known_false_skips_required_sweep_but_stays_unvalidated():
    """The properties-only live shape gives us field NAMES, never requirements.
    A naive fix that flips `unvalidated` to False the moment metadata exists
    would claim a validation that was never performed — this must not happen."""
    meta = RecordMetadata(
        record_type="customer",
        fields=[FieldSpec(name="subsidiary", label="Primary Subsidiary", required=False)],
        requirements_known=False,
    )
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x"}, lines=[]),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert result.unvalidated is True
    assert result.missing_required == []
    assert result.ok is True


def test_requirements_known_false_with_invariant_errors_still_not_ok():
    """Unknown requirements PLUS a known invariant violation: still unvalidated,
    but not ok — mirrors test_no_metadata_with_invariant_errors_is_not_ok for
    the requirements_known=False case."""
    meta = RecordMetadata(record_type="customer", fields=[], requirements_known=False)
    result = validate_write(
        payload=NormalizedPayload(fields={}, lines=[]),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
        invariant_errors=["debits must equal credits"],
    )
    assert result.unvalidated is True
    assert result.ok is False


def test_requirements_known_true_default_preserves_existing_validation():
    """Regression guard: RecordMetadata constructed without requirements_known
    (every other fixture in this file) must keep validating exactly as before."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "test ai customer"}, lines=[]),
        metadata=META,
        record_type="customer",
        mutation_type="create",
    )
    assert result.unvalidated is False
    assert result.missing_required == ["subsidiary"]


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


# ---------------------------------------------------------------------------
# Delegating a missing field to the human instead of back to the model
# ---------------------------------------------------------------------------


def _missing_subsidiary_result():
    meta = RecordMetadata(
        record_type="customer",
        fields=[
            FieldSpec(name="subsidiary", label="Primary Subsidiary", required=True),
            FieldSpec(name="companyname", label="Company Name", required=True),
        ],
    )
    return validate_write(
        payload=NormalizedPayload(fields={"companyname": "Acme"}, lines=[]),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )


def test_delegated_slot_clears_that_field_from_missing_required():
    """When the model says "a human must choose this" and the server actually
    resolved options for it, the field is no longer the model's problem to
    solve — it is a question for the card. Leaving it in missing_required
    would bounce the proposal back to the model forever."""
    from app.services.chat.write_validator import EditableSlot

    base = _missing_subsidiary_result()
    assert base.ok is False and base.missing_required == ["subsidiary"]

    delegated = base.with_delegated_slots(
        [EditableSlot(name="subsidiary", label="Primary Subsidiary", allowed=[{"value": "1", "label": "Framework"}])]
    )

    assert delegated.ok is True
    assert delegated.missing_required == []
    assert [s.name for s in delegated.editable_slots] == ["subsidiary"]
    assert delegated.editable_slots[0].allowed == [{"value": "1", "label": "Framework"}]


def test_delegating_an_unrelated_field_leaves_the_real_gap_open():
    """A slot for something else does not make the missing field any less
    missing — otherwise a stray hint would wave an incomplete write through."""
    from app.services.chat.write_validator import EditableSlot

    delegated = _missing_subsidiary_result().with_delegated_slots(
        [EditableSlot(name="department", label="Department", allowed=[{"value": "2", "label": "Ops"}])]
    )
    assert delegated.ok is False
    assert delegated.missing_required == ["subsidiary"]


def test_delegation_never_clears_invariant_errors():
    """Invariant failures (closed period, unbalanced journal) are terminal —
    no human slot can answer them, so they must survive delegation."""
    from app.services.chat.write_validator import EditableSlot

    meta = RecordMetadata(record_type="journalEntry", fields=[], requirements_known=True)
    base = validate_write(
        payload=NormalizedPayload(fields={}, lines=[]),
        metadata=meta,
        record_type="journalEntry",
        mutation_type="create",
        invariant_errors=["Accounting period 2026-01 is closed."],
    )
    delegated = base.with_delegated_slots([EditableSlot(name="subsidiary", label="Subsidiary")])
    assert delegated.ok is False
    assert delegated.invariant_errors == ["Accounting period 2026-01 is closed."]


def test_delegation_never_clears_line_level_gaps():
    """Line fields have no slot mechanism in v1 — a line gap stays terminal."""
    from app.services.chat.write_validator import EditableSlot

    meta = RecordMetadata(
        record_type="journalEntry",
        fields=[],
        line_fields=[FieldSpec(name="account", label="Account", required=True)],
    )
    base = validate_write(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 5}]),
        metadata=meta,
        record_type="journalEntry",
        mutation_type="create",
    )
    delegated = base.with_delegated_slots([EditableSlot(name="account", label="Account")])
    assert delegated.ok is False
    assert delegated.missing_line_required == ["line[0].account"]


def test_delegation_with_no_slots_returns_an_equal_result():
    base = _missing_subsidiary_result()
    assert base.with_delegated_slots([]) == base


def test_delegation_does_not_duplicate_an_already_declared_slot():
    """validate_write already declares a slot for every missing required field.
    A hint naming the same field must refine that slot (it carries real
    options), not stack a second one with the same name onto the card."""
    from app.services.chat.write_validator import EditableSlot

    base = _missing_subsidiary_result()
    assert [s.name for s in base.editable_slots] == ["subsidiary"]

    delegated = base.with_delegated_slots(
        [EditableSlot(name="subsidiary", label="Primary Subsidiary", allowed=[{"value": "1", "label": "Framework"}])]
    )
    assert [s.name for s in delegated.editable_slots] == ["subsidiary"]
    assert delegated.editable_slots[0].allowed == [{"value": "1", "label": "Framework"}]
