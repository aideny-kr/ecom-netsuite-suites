from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata
from app.services.chat.write_payload import NormalizedPayload
from app.services.chat.write_validator import validate_write

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
