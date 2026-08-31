"""Three outcomes for an approved write, not two.

The shipped code recognises success and failure. A timeout is neither: it says
what our patience ran out on, never what NetSuite committed. Proven live on
2026-08-27 against sandbox 6738075-sb1 — ns_createRecord exceeded the ceiling,
NetSuite created customer 5264348 anyway, and the app recorded `failed` and
offered to run the identical payload again. One approval from a duplicate.

`write_repair_bound.py` says in its own docstring that it decides what to do
when a write is "rejected BY NETSUITE". A transport failure is outside that
contract, and feeding one to it asks the model to fix a payload that was
never the problem — while the record may already exist.

So the classifier has to distinguish "NetSuite said no" (repairable, the
payload is wrong) from "we do not know" (NOT repairable; reconcile first,
never re-propose). The signal is structural, set where the timeout is caught,
not sniffed out of an error string downstream.
"""

import pytest

from app.services.chat.write_outcome import INDETERMINATE_KEY, classify_write_outcome


class TestIndeterminate:
    def test_the_timeout_marker_is_indeterminate(self):
        result = {"error": "Tool execution exceeded 60-second timeout limit", INDETERMINATE_KEY: True}
        assert classify_write_outcome(result) == "indeterminate"

    def test_an_unparseable_result_is_indeterminate_not_success(self):
        """Previously this branch set `_exec_succeeded = True` and told the
        operator the write had executed — asserting an outcome from a response
        we could not read at all."""
        assert classify_write_outcome(None, unparseable=True) == "indeterminate"

    def test_a_non_dict_result_is_indeterminate(self):
        assert classify_write_outcome("some bare string") == "indeterminate"

    def test_the_marker_wins_over_an_error_message(self):
        """A timeout carries an `error` string too. If the error branch were
        checked first, every timeout would be misfiled as a NetSuite rejection
        — which is the bug this module exists to prevent."""
        result = {"error": "Tool execution exceeded 60-second timeout limit", INDETERMINATE_KEY: True}
        assert classify_write_outcome(result) == "indeterminate"


class TestDeterminateFailure:
    def test_a_netsuite_error_is_a_failure(self):
        result = {"error": "Invalid subsidiary reference"}
        assert classify_write_outcome(result) == "failed"

    def test_success_false_without_an_error_key_is_a_failure(self):
        """The external MCP write tools share ns_runReport's transport, which
        is CONFIRMED to drop `error` while still sending `success: false`."""
        assert classify_write_outcome({"success": False}) == "failed"

    def test_o_error_details_shape_is_a_failure(self):
        result = {"error": '{"o:errorDetails":[{"detail":"subsidiary is required"}]}'}
        assert classify_write_outcome(result) == "failed"


class TestSuccess:
    def test_a_bare_id_response_is_success(self):
        """ns_createRecord's ordinary response has no `success` key at all."""
        assert classify_write_outcome({"id": "5264348"}) == "success"

    def test_success_true_is_success(self):
        assert classify_write_outcome({"success": True, "recordId": "123"}) == "success"

    def test_an_empty_dict_is_success_not_indeterminate(self):
        """Deliberate: an empty dict parsed cleanly. Only an UNPARSEABLE or
        explicitly-marked result is unknown — widening this would turn
        ordinary writes into reconciliation prompts."""
        assert classify_write_outcome({}) == "success"


class TestRepairEligibility:
    """The consequence that matters: only a determinate failure may re-enter
    the repair loop. Re-proposing an indeterminate write is how a duplicate
    gets created."""

    @pytest.mark.parametrize(
        "outcome,eligible",
        [("failed", True), ("indeterminate", False), ("success", False)],
    )
    def test_only_a_determinate_failure_may_repair(self, outcome, eligible):
        from app.services.chat.write_outcome import may_enter_repair_loop

        assert may_enter_repair_loop(outcome) is eligible
