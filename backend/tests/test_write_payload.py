import pytest

from app.services.chat.write_payload import (
    PayloadParseError,
    normalize_write_payload,
)


def test_data_as_json_string_is_parsed():
    """The live NetSuite MCP shape — `data` holding a JSON *string*."""
    result = normalize_write_payload({"recordType": "customer", "data": '{"companyname": "test ai customer"}'})
    assert result.fields == {"companyname": "test ai customer"}
    assert result.lines == []
    assert result.record_id is None


def test_body_as_dict_is_parsed():
    """The legacy shape the old code assumed."""
    result = normalize_write_payload({"recordType": "invoice", "body": {"memo": "x"}})
    assert result.fields == {"memo": "x"}


def test_data_as_dict_is_parsed():
    result = normalize_write_payload({"recordType": "invoice", "data": {"memo": "x"}})
    assert result.fields == {"memo": "x"}


def test_record_id_from_top_level_then_body():
    assert normalize_write_payload({"id": "42", "data": "{}"}).record_id == "42"
    assert normalize_write_payload({"data": '{"id": 7}'}).record_id == "7"


def test_lines_are_extracted_from_sublists():
    """Transaction records carry lines; validation must see them."""
    result = normalize_write_payload(
        {
            "recordType": "journalEntry",
            "data": '{"subsidiary": "1", "line": [{"account": "10", "debit": 5}, {"account": "20", "credit": 5}]}',
        }
    )
    assert result.fields["subsidiary"] == "1"
    assert len(result.lines) == 2
    assert result.lines[0]["account"] == "10"


def test_unparseable_payload_raises():
    """Fail closed — never silently yield empty fields."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"recordType": "customer", "data": "{not json"})


def test_no_payload_key_at_all_raises():
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"recordType": "customer"})
