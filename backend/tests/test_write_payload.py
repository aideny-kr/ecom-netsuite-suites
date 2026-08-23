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


# ---------------------------------------------------------------------------
# `.record` / `.payload_key` — the raw, unsplit record and which tool_input
# key it came from. A lossless merge target: `.fields`/`.lines` alone lose
# which original sublist name (`line`/`item`/`expense`/...) a line group
# came from, so merging into `.fields` and reassembling `.lines` back under
# a hardcoded key would silently drop every line item on write-back
# (write_confirmation_service.py's Task 7 review, CRITICAL finding).
# ---------------------------------------------------------------------------


def test_record_and_payload_key_exposed_for_data_shape():
    result = normalize_write_payload(
        {
            "recordType": "journalEntry",
            "data": '{"subsidiary": "1", "line": [{"account": "10", "debit": 5}]}',
        }
    )
    assert result.payload_key == "data"
    assert result.record == {"subsidiary": "1", "line": [{"account": "10", "debit": 5}]}


def test_record_and_payload_key_exposed_for_body_shape():
    result = normalize_write_payload(
        {
            "recordType": "journalEntry",
            "body": {"subsidiary": "1", "item": [{"item": "5", "quantity": 1}]},
        }
    )
    assert result.payload_key == "body"
    assert result.record == {"subsidiary": "1", "item": [{"item": "5", "quantity": 1}]}


def test_payload_key_resolves_to_the_key_that_actually_coerced():
    """`data` is present but null; `body` is what actually parsed.

    `_PAYLOAD_KEYS` precedence walks `data` first, but a present-and-null
    `data` doesn't coerce — `payload_key` must name `body`, the key that
    actually produced the record, not just the first key that exists.
    """
    result = normalize_write_payload({"data": None, "body": {"companyname": "x"}})
    assert result.payload_key == "body"
    assert result.record == {"companyname": "x"}


# ---------------------------------------------------------------------------
# MF-2 — a `_LINE_KEYS` sublist entry that isn't an object must never be
# silently dropped. The old behaviour filtered non-dict entries out of
# `.lines` and never returned them to `.fields`, so they vanished from the
# confirmation card while `tool_input` — what actually executes — still
# carried them. Fail closed instead: raise, exactly like an unparseable
# payload, so the card is never built from a payload it can't fully show.
# ---------------------------------------------------------------------------


def test_line_list_of_bare_scalars_raises():
    """`{"item": ["1", "2"]}` — a scalar array is not renderable as lines."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"recordType": "salesOrder", "data": '{"entity": "5", "item": ["1", "2"]}'})


def test_line_list_with_one_non_dict_entry_among_dicts_raises():
    """A mix of good and bad entries must still fail closed, not just drop the bad one."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload(
            {
                "recordType": "salesOrder",
                "body": {
                    "entity": "5",
                    "item": [{"item": "1", "quantity": 1}, {"item": "2", "quantity": 1}, "ghost"],
                },
            }
        )
