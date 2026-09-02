"""The side-effect log — making "we sent it and don't know" representable.

WHY. Today a NetSuite write has two recordable outcomes, and a third that
actually happens: the request was sent and no answer came back. On 2026-08-27
that third case created sandbox customer 5264348 while the app reported
`failed` and offered to run the identical payload again. PR #210 taught the
CARD to say "indeterminate"; it did not give the SYSTEM anywhere to record that
a call was in flight when the process died.

`.claude/rules/agent-graph.md` #10 is explicit: "Every external write needs a
work-derived idempotency key and a side-effect log written *before* the call,
so a crash between send and confirm is recoverable." None of it existed.

WHAT MAKES IT RECOVERABLE, settled live 2026-09-01 against sandbox
6738075-sb1 (see the design spec): the MCP surface has no idempotency header —
`ns_createRecord` takes only `recordType` and `data` — but NetSuite ENFORCES
uniqueness on `externalId` and refuses a duplicate with a distinguishable
"This entity already exists". So the key travels inside the payload, and the
guarantee is enforced by NetSuite rather than trusted from us.

The state machine has exactly one irreversible transition and it is the one
that matters: `attempted` is written BEFORE the call and is the state a crash
leaves behind. Nothing may set `written` without a definite answer.
"""

import json
import uuid

import pytest

from app.services.chat.write_side_effect import (
    IDEM_PREFIX,
    SideEffectStatus,
    build_idempotency_key,
    classify_retry_result,
    payload_with_idempotency_key,
)


class TestKeyIsWorkDerived:
    """A random key would make every retry a NEW write — the defect wearing a
    disguise. The key must be a pure function of the work."""

    def test_same_work_yields_same_key(self):
        a = build_idempotency_key(batch_id="b1", row_index=3, payload={"companyName": "Acme", "subsidiary": "5"})
        b = build_idempotency_key(batch_id="b1", row_index=3, payload={"companyName": "Acme", "subsidiary": "5"})
        assert a == b

    def test_key_ignores_dict_ordering(self):
        """Canonicalised, or two serialisations of one payload retry as two
        different writes."""
        a = build_idempotency_key(batch_id="b1", row_index=0, payload={"a": 1, "b": 2})
        b = build_idempotency_key(batch_id="b1", row_index=0, payload={"b": 2, "a": 1})
        assert a == b

    def test_different_work_yields_different_keys(self):
        base = {"companyName": "Acme"}
        assert build_idempotency_key(batch_id="b1", row_index=0, payload=base) != build_idempotency_key(
            batch_id="b1", row_index=1, payload=base
        )
        assert build_idempotency_key(batch_id="b1", row_index=0, payload=base) != build_idempotency_key(
            batch_id="b2", row_index=0, payload=base
        )
        assert build_idempotency_key(batch_id="b1", row_index=0, payload=base) != build_idempotency_key(
            batch_id="b1", row_index=0, payload={"companyName": "Acme Ltd"}
        )

    def test_key_is_namespaced_and_recognisable(self):
        """externalId is a real business field integrations own. A value we
        generated must be identifiable on sight and must not plausibly collide
        with a human-chosen key."""
        k = build_idempotency_key(batch_id="b1", row_index=0, payload={"x": 1})
        assert k.startswith(IDEM_PREFIX)
        assert len(k) <= 40, "NetSuite externalId is length-bounded; keep it short"

    def test_key_is_a_pure_function_of_its_inputs(self):
        """Deterministic and non-random: the SAME work must always produce the
        SAME key, or a retry becomes a new write.

        Deliberately NOT pinned to a hardcoded digest. The previous version
        rebuilt the hash material inline and compared — a second copy of the
        implementation that had to be edited in lockstep with it, and which
        could only ever restate what the code already said. Which inputs
        participate is pinned behaviourally instead, one property per test, in
        TestTheKeyIdentifiesTheWork.
        """
        payload = {"companyName": "Acme"}
        first = build_idempotency_key(batch_id="b1", row_index=0, payload=payload)
        second = build_idempotency_key(batch_id="b1", row_index=0, payload=dict(payload))

        assert first == second
        assert first.startswith("ss-idem-")
        # Key order in the dict must not change the key — two serialisations of
        # one payload must not retry as two writes.
        assert build_idempotency_key(batch_id="b1", row_index=0, payload={"b": 2, "a": 1}) == build_idempotency_key(
            batch_id="b1", row_index=0, payload={"a": 1, "b": 2}
        )


class TestPayloadCarriesTheKey:
    """There is no header channel — ns_createRecord accepts only recordType and
    data — so the key rides in the payload as externalId."""

    def test_key_is_injected_as_external_id(self):
        out, key = payload_with_idempotency_key({"companyName": "Acme"}, batch_id="b1", row_index=0)
        assert out["externalId"] == key
        assert out["companyName"] == "Acme", "must not disturb the rest of the payload"

    def test_a_user_supplied_external_id_is_never_overwritten(self):
        """Their key is a better natural identity than our hash, and silently
        replacing it would corrupt an integration we do not own."""
        out, key = payload_with_idempotency_key(
            {"companyName": "Acme", "externalId": "ERP-4417"}, batch_id="b1", row_index=0
        )
        assert out["externalId"] == "ERP-4417"
        assert key == "ERP-4417", "reconciliation must key on the value actually written"

    def test_a_user_supplied_external_id_is_honoured_case_insensitively(self):
        """The catalog spells it externalId, SuiteQL returns externalid — the
        same case split that once made required_field_registry miss every real
        write."""
        out, key = payload_with_idempotency_key(
            {"companyName": "Acme", "externalid": "ERP-9"}, batch_id="b1", row_index=0
        )
        assert key == "ERP-9"
        assert "ERP-9" in json.dumps(out)

    def test_the_input_payload_is_not_mutated(self):
        original = {"companyName": "Acme"}
        payload_with_idempotency_key(original, batch_id="b1", row_index=0)
        assert original == {"companyName": "Acme"}


class TestRetryClassification:
    """The decisive behaviour. A retry that hits NetSuite's uniqueness refusal
    proves the ORIGINAL landed — it is success, not failure. Measured live:
    HTTP 400 with o:errorDetails detail "Error while accessing a resource.
    This entity already exists." (o:errorCode USER_ERROR)."""

    ALREADY = json.dumps(
        {
            "success": False,
            "error": '{"status":400,"o:errorDetails":[{"detail":"Error while accessing a resource. '
            'This entity already exists.","o:errorCode":"USER_ERROR"}]}',
        }
    )

    def test_duplicate_refusal_means_the_original_landed(self):
        # Our own key: the refusal really does prove our original landed.
        assert classify_retry_result(self.ALREADY, idempotency_key="ss-idem-abc123") is SideEffectStatus.WRITTEN

    def test_an_ordinary_rejection_is_still_a_rejection(self):
        result = json.dumps(
            {
                "success": False,
                "error": '{"o:errorDetails":[{"detail":"Please enter value(s) for: Subsidiary.",'
                '"o:errorCode":"USER_ERROR"}]}',
            }
        )
        assert classify_retry_result(result) is SideEffectStatus.REJECTED

    def test_a_success_is_written(self):
        assert classify_retry_result(json.dumps({"success": True, "recordId": "5264548"})) is SideEffectStatus.WRITTEN

    def test_an_indeterminate_result_stays_attempted(self):
        """A timeout on the RETRY tells us nothing new — it must not collapse
        into either answer."""
        from app.services.chat.write_outcome import INDETERMINATE_KEY

        result = json.dumps({"error": "Tool execution exceeded 60-second timeout limit", INDETERMINATE_KEY: True})
        assert classify_retry_result(result) is SideEffectStatus.ATTEMPTED

    def test_unparseable_stays_attempted_never_written(self):
        assert classify_retry_result("<html>502 Bad Gateway</html>") is SideEffectStatus.ATTEMPTED


class TestStatusesAreDeliberate:
    def test_the_four_states_exist_and_attempted_is_the_crash_state(self):
        assert {s.value for s in SideEffectStatus} == {"attempted", "written", "rejected", "unknown"}

    def test_written_is_never_a_default(self):
        """Fail-closed: anything we cannot classify must not read as success on
        the money path."""
        for junk in ("", "null", "{}", "[]"):
            assert classify_retry_result(junk) is not SideEffectStatus.WRITTEN


@pytest.mark.parametrize("bad", [None, 123, [], "not-a-dict"])
def test_key_derivation_refuses_a_non_dict_payload(bad):
    with pytest.raises((TypeError, ValueError)):
        build_idempotency_key(batch_id="b", row_index=0, payload=bad)


def test_row_index_is_part_of_the_key_so_identical_rows_stay_distinct():
    """Two rows in one file may legitimately be the same company (different
    contacts, say). They are different WORK and must not collapse onto one key."""
    same = {"companyName": "Acme"}
    keys = {build_idempotency_key(batch_id="b1", row_index=i, payload=same) for i in range(5)}
    assert len(keys) == 5


def test_single_record_writes_get_a_key_too():
    """Phase 1 ships alone and must fix the single-record timeout case — a
    batchless write still needs a key to be answerable."""
    out, key = payload_with_idempotency_key({"companyName": "Acme"}, batch_id=None, row_index=None)
    assert out["externalId"] == key
    assert key.startswith(IDEM_PREFIX)
    assert uuid.UUID  # sanity: module imported cleanly


# ---------------------------------------------------------------------------
# Stamping must not destroy the payload it stamps.
#
# T2 gate round 1, BLOCKER. The first implementation stamped
# `normalize_write_payload(...).fields` and wrote that back as the whole
# payload. `.fields` deliberately EXCLUDES line sublists, so every transaction
# line vanished from both the HMAC-signed card and the payload NetSuite
# received. write_payload.NormalizedPayload.record exists precisely for this
# and says so: "a merge that wants to add a field without losing line items has
# to merge into `record` and write back under `payload_key`".
#
# 134 write-path tests missed it because every one of them used a header-only
# customer create. Not one had a line item.
# ---------------------------------------------------------------------------


class TestStampToolInput:
    def test_line_items_survive_stamping(self):
        """The blocker. A salesOrder keeps its lines and gains an externalId."""
        from app.services.chat.write_side_effect import stamp_tool_input

        raw = {
            "recordType": "salesOrder",
            "data": json.dumps(
                {
                    "entity": 123,
                    "item": [
                        {"item": 101, "quantity": 2, "rate": 50.0},
                        {"item": 202, "quantity": 1, "rate": 19.99},
                    ],
                }
            ),
        }
        out, key = stamp_tool_input(raw, batch_id=None, row_index=None)
        sent = json.loads(out["data"])

        assert sent["item"] == json.loads(raw["data"])["item"], "lines must be untouched"
        assert sent["externalId"] == key
        assert sent["entity"] == 123

    def test_the_key_covers_the_lines_not_just_the_header(self):
        """Two orders with identical headers but DIFFERENT lines are different
        work and must get different keys.

        Deriving from header fields alone gave them the same key — NetSuite
        would refuse the second as a duplicate externalId, and a duplicate
        refusal is classified WRITTEN, so we would report an order as created
        that never existed. Worse than the dropped lines it hid behind.
        """
        from app.services.chat.write_side_effect import stamp_tool_input

        def _order(qty):
            return {
                "recordType": "salesOrder",
                "data": json.dumps({"entity": 123, "item": [{"item": 101, "quantity": qty}]}),
            }

        _, key_a = stamp_tool_input(_order(2), batch_id=None, row_index=None)
        _, key_b = stamp_tool_input(_order(99), batch_id=None, row_index=None)
        assert key_a != key_b

    def test_a_dict_payload_stays_a_dict(self):
        """`body` arrives as a dict, `data` as a JSON string. Writing a string
        back where a dict was read would change the tool_input's shape under
        the schema the MCP server declared."""
        from app.services.chat.write_side_effect import stamp_tool_input

        raw = {"recordType": "customer", "body": {"companyName": "Acme"}}
        out, key = stamp_tool_input(raw, batch_id=None, row_index=None)

        assert isinstance(out["body"], dict), "a dict payload must not become a string"
        assert out["body"]["externalId"] == key

    def test_a_caller_supplied_externalid_is_returned_untouched(self):
        from app.services.chat.write_side_effect import stamp_tool_input

        raw = {"recordType": "customer", "data": json.dumps({"companyName": "A", "externalId": "THEIRS"})}
        out, key = stamp_tool_input(raw, batch_id=None, row_index=None)

        assert key == "THEIRS"
        assert json.loads(out["data"])["externalId"] == "THEIRS"

    def test_an_input_with_no_payload_is_left_alone(self):
        """A delete-shaped call carries no data/body. It must no-op EXPLICITLY,
        not by falling through a broad except."""
        from app.services.chat.write_side_effect import stamp_tool_input

        raw = {"recordType": "customer", "recordId": "5795008"}
        out, key = stamp_tool_input(raw, batch_id=None, row_index=None)

        assert out == raw
        assert key is None


class TestClassifyIsFailClosed:
    """T2 gate round 1. `classify_retry_result`'s docstring promised "WRITTEN is
    never a default" while delivering exactly that: the underlying outcome
    classifier reads ABSENCE OF AN ERROR as success, so the MCP layer's own
    indeterminate envelopes classified as a definite write.

    mcp_client_service.py:344 returns `{"result": "No content returned"}` when
    the server sends nothing back, and :350 returns `{"result": <raw text>}`
    for anything unparseable. Both mean "I cannot tell you what happened" —
    the one state this table exists to represent — and both were being
    recorded as WRITTEN and dropped off the resume worklist.

    The rule: WRITTEN requires POSITIVE evidence (an explicit success flag or a
    record id), never the mere absence of an error.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            '{"result": "No content returned"}',
            '{"result": "some unparseable prose from the server"}',
            '{"result": ""}',
            '{"result": null}',
        ],
    )
    def test_mcp_indeterminate_envelopes_stay_attempted(self, raw):
        from app.services.chat.write_side_effect import classify_retry_result

        assert classify_retry_result(raw) is SideEffectStatus.ATTEMPTED

    @pytest.mark.parametrize(
        "raw",
        [
            '{"success": true, "recordId": "5264548"}',
            '{"recordId": "5264548"}',
            '{"id": 5264548}',
            '{"success": true}',
        ],
    )
    def test_positive_evidence_still_settles_written(self, raw):
        """The guard must not make real successes unsettleable."""
        from app.services.chat.write_side_effect import classify_retry_result

        assert classify_retry_result(raw) is SideEffectStatus.WRITTEN

    def test_a_duplicate_refusal_is_still_written(self):
        """Unchanged: NetSuite refusing our externalId proves the first landed."""
        from app.services.chat.write_side_effect import classify_retry_result

        raw = json.dumps(
            {
                "success": False,
                "error": '{"o:errorDetails":[{"detail":"Error while accessing a resource. '
                'This entity already exists.","o:errorCode":"USER_ERROR"}]}',
            }
        )
        assert classify_retry_result(raw, idempotency_key="ss-idem-abc") is SideEffectStatus.WRITTEN

    def test_an_explicit_error_is_still_rejected(self):
        from app.services.chat.write_side_effect import classify_retry_result

        raw = '{"error": "Please enter value(s) for: Subsidiary."}'
        assert classify_retry_result(raw) is SideEffectStatus.REJECTED


class TestTheKeyIdentifiesTheWork:
    """T2 gate round 2 — and the SHAPE shared with round 1's blocker.

    Round 1: the key was derived from `.fields`, so line items did not
    participate. Round 2: three more collisions, all the same defect wearing
    different clothes — the key does not identify the work.

    An idempotency key IS an identity claim. When two different writes share
    one, NetSuite refuses the second as a duplicate externalId, and a duplicate
    refusal classifies as WRITTEN — so we report an order created that never
    existed. Every case below was verified colliding before this change.

    The mechanism, rather than three patches: the key is a function of the
    COMPLETE work identity — connector, record type, mutation type, record id,
    batch/row, and the full payload — and is recomputed whenever the payload
    changes, replacing OUR key (the `ss-idem-` namespace exists for exactly
    this) while never touching a caller's.
    """

    def _key(self, **kw):
        from app.services.chat.write_side_effect import build_idempotency_key

        base = dict(batch_id=None, row_index=None, payload={"companyName": "Acme"})
        base.update(kw)
        return build_idempotency_key(**base)

    def test_record_type_participates(self):
        """A customer and a vendor with identical payloads are different work."""
        assert self._key(record_type="customer") != self._key(record_type="vendor")

    def test_mutation_type_participates(self):
        assert self._key(mutation_type="create") != self._key(mutation_type="update")

    def test_record_id_participates(self):
        """Deleting record 5 and record 99 are different work — and both carry
        NO payload, which is how they collided onto one ledger row."""
        assert self._key(payload={}, record_id="5") != self._key(payload={}, record_id="99")

    def test_connector_participates(self):
        """The same write to sandbox and to production is not the same write."""
        assert self._key(connector_id="conn-sandbox") != self._key(connector_id="conn-prod")

    def test_payload_less_deletes_do_not_all_collide(self):
        """The worst case: with no payload, every delete in a tenant hashed to
        one constant key, so the second delete silently no-op'd on the first's
        row and then overwrote its record id."""
        a = self._key(payload={}, record_type="customer", mutation_type="delete", record_id="5")
        b = self._key(payload={}, record_type="salesOrder", mutation_type="delete", record_id="99")
        assert a != b

    def test_a_changed_payload_changes_our_key(self):
        """THE dangerous one. The key is stamped at card build; the human then
        fills required-field slots and the payload changes. Our key must follow
        it. Two drafts completed differently are two different writes, and
        giving them one externalId makes NetSuite refuse the second — which we
        would classify as WRITTEN."""
        from app.services.chat.write_side_effect import payload_with_idempotency_key

        draft, _ = payload_with_idempotency_key(
            {"companyName": "Acme"}, batch_id=None, row_index=None, record_type="customer"
        )
        a, ka = payload_with_idempotency_key(
            {**draft, "subsidiary": 1}, batch_id=None, row_index=None, record_type="customer"
        )
        b, kb = payload_with_idempotency_key(
            {**draft, "subsidiary": 2}, batch_id=None, row_index=None, record_type="customer"
        )
        assert ka != kb, "different completed writes must not share an externalId"
        assert a["externalId"] == ka and b["externalId"] == kb

    def test_recomputing_is_stable_for_unchanged_work(self):
        """Idempotent: stamping an already-stamped, unchanged payload must not
        churn the key, or a retry of the SAME work would become a new write."""
        from app.services.chat.write_side_effect import payload_with_idempotency_key

        once, k1 = payload_with_idempotency_key(
            {"companyName": "Acme"}, batch_id=None, row_index=None, record_type="customer"
        )
        twice, k2 = payload_with_idempotency_key(once, batch_id=None, row_index=None, record_type="customer")
        assert k1 == k2
        assert once == twice

    def test_a_caller_supplied_key_is_still_never_touched(self):
        """Unchanged contract: their key is the better natural identity."""
        from app.services.chat.write_side_effect import payload_with_idempotency_key

        out, key = payload_with_idempotency_key(
            {"companyName": "Acme", "externalId": "THEIRS-123"},
            batch_id=None,
            row_index=None,
            record_type="customer",
        )
        assert key == "THEIRS-123"
        assert out["externalId"] == "THEIRS-123"


class TestDuplicateRefusalOnlyProvesOurOwnWrite:
    """T2 gate round 2. 'This entity already exists' proves OUR write landed
    only if the externalId was OURS. For a CALLER-supplied externalId the same
    refusal equally means "your own integration already used that id" — an
    everyday collision that has nothing to do with our attempt.

    Reading it as WRITTEN there marks a write successful that never happened.
    The `ss-idem-` namespace is what makes the distinction decidable.
    """

    _DUP = json.dumps(
        {
            "success": False,
            "error": '{"o:errorDetails":[{"detail":"Error while accessing a resource. '
            'This entity already exists.","o:errorCode":"USER_ERROR"}]}',
        }
    )

    def test_our_own_key_still_proves_the_write_landed(self):
        from app.services.chat.write_side_effect import classify_retry_result

        got = classify_retry_result(self._DUP, idempotency_key="ss-idem-abc123")
        assert got is SideEffectStatus.WRITTEN

    def test_a_caller_key_is_not_proof_and_stays_attempted(self):
        from app.services.chat.write_side_effect import classify_retry_result

        got = classify_retry_result(self._DUP, idempotency_key="THEIRS-123")
        assert got is SideEffectStatus.ATTEMPTED

    def test_an_unknown_key_fails_closed(self):
        from app.services.chat.write_side_effect import classify_retry_result

        assert classify_retry_result(self._DUP, idempotency_key=None) is SideEffectStatus.ATTEMPTED
