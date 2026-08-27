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


# ---------------------------------------------------------------------------
# T2 gate Finding B — two coerced payload keys must raise, not silently pick
# one (the reported bug: `{"data": {}, "body": {pop}}` picked "data" and
# rendered an empty confirmation card while `tool_input` still carried the
# populated `body`, which executes verbatim against the remote MCP). Ruling:
# raise whenever MORE THAN ONE key coerces to a dict — no emptiness or
# equality inspection anywhere. See the architect ruling this branch's T2
# gate delivered (orchestrator.py write_confirm block, write_payload.py
# module docstring).
# ---------------------------------------------------------------------------


def test_both_payload_keys_populated_raises():
    with pytest.raises(PayloadParseError) as exc_info:
        normalize_write_payload({"recordType": "customer", "data": {"memo": "A"}, "body": {"memo": "B"}})
    assert "data" in str(exc_info.value)
    assert "body" in str(exc_info.value)


def test_empty_data_plus_populated_body_dict_raises():
    """The reported bug, dict-empty variant — today picks 'data' with
    fields={}, silently dropping the populated 'body' from the card while
    tool_input (what actually executes) still carries it."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"data": {}, "body": {"account": "123", "amount": 500}})


def test_empty_data_plus_populated_body_string_raises():
    """The reported bug, string-'{}' variant."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"data": "{}", "body": {"account": "123", "amount": 500}})


def test_both_keys_identical_still_raises():
    """No equality carve-out: a dual-populated input is refused even when
    both keys happen to hold the same value — determinism over cleverness;
    no real producer emits this shape, and the write-repair loop absorbs it
    either way."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"data": {"memo": "A"}, "body": {"memo": "A"}})


def test_both_keys_empty_dicts_raises():
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"data": {}, "body": {}})


def test_sole_empty_dict_payload_still_normalizes_not_raises():
    """Case 3 guard — the row a naive 'skip empty keys, prefer non-empty'
    fix would break. Exactly one key present and it coerces (to an empty
    dict) — no ambiguity, so this must NOT raise. This is also the
    regression the task explicitly calls out: a genuinely empty payload
    must still yield an empty NormalizedPayload."""
    result = normalize_write_payload({"data": {}})
    assert result.fields == {}
    assert result.lines == []
    assert result.record == {}
    assert result.payload_key == "data"


@pytest.mark.parametrize("uncoercible_data", [None, "", "   ", 42, [1]])
def test_present_but_uncoercible_data_still_falls_through_to_body(uncoercible_data):
    """Present-but-uncoercible sibling keys keep falling through — this is
    what `_PAYLOAD_KEYS` precedence legitimately means; only the SECOND
    coerced key is new territory. Extends
    test_payload_key_resolves_to_the_key_that_actually_coerced."""
    result = normalize_write_payload({"data": uncoercible_data, "body": {"companyname": "x"}})
    assert result.payload_key == "body"
    assert result.record == {"companyname": "x"}


def test_populated_data_with_malformed_body_string_now_raises():
    """Intended behavior delta: under the old break-on-first-match scan,
    'data' won and a malformed 'body' string was never even examined — it
    would ride along unexamined all the way to tool_input, still sent to
    NetSuite verbatim. The new scan coerces every present key, so a
    malformed second key now fails closed instead."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"data": {"memo": "A"}, "body": "{not json"})


# ---------------------------------------------------------------------------
# ask_user must never survive into the payload, at ANY nesting level
# ---------------------------------------------------------------------------
#
# `ask_user` is an out-of-band hint: the model names a field it wants a human
# to choose, the server resolves the options, and the key is popped before
# anything else touches the tool input. That pop handles the TOP level of
# tool_input — but observed live on staging 2026-08-27 the model nested it
# INSIDE the data payload instead:
#
#   {"recordType": "customer",
#    "data": "{\"companyName\": \"Acme\", \"ask_user\": [\"subsidiary\"]}"}
#
# Two real consequences. It rendered as a bogus field row on the confirmation
# card — the operator saw a field named `ask_user` and reasonably rejected the
# write. And on approve it would have been posted to NetSuite as a record
# field, which NetSuite rejects, turning an approved write into a 400.


def test_ask_user_nested_in_data_is_stripped_from_fields():
    payload = normalize_write_payload(
        {"recordType": "customer", "data": '{"companyName": "Acme", "ask_user": ["subsidiary"]}'}
    )
    assert payload.fields == {"companyName": "Acme"}


def test_ask_user_nested_in_body_is_stripped_too():
    payload = normalize_write_payload(
        {"recordType": "customer", "body": {"companyName": "Acme", "ask_user": ["subsidiary"]}}
    )
    assert payload.fields == {"companyName": "Acme"}


def test_stripping_ask_user_does_not_break_an_otherwise_empty_payload():
    """A payload whose only key was the hint still parses. It is genuinely
    empty of real fields, and the validator's missing-required check is what
    should speak to that — not a parse error."""
    payload = normalize_write_payload({"recordType": "customer", "data": '{"ask_user": ["subsidiary"]}'})
    assert payload.fields == {}


def test_a_field_merely_named_like_the_hint_is_untouched():
    """Only the exact key is removed. NetSuite has no `ask_user` field, but a
    custom field could be named something adjacent — do not over-match."""
    payload = normalize_write_payload(
        {"recordType": "customer", "data": '{"custentity_ask_user_note": "x", "companyName": "Acme"}'}
    )
    assert payload.fields == {"custentity_ask_user_note": "x", "companyName": "Acme"}
