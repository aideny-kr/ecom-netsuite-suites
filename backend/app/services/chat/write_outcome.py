"""Classify what happened to an approved NetSuite write — in three outcomes.

The shipped code recognised two, success and failure, and folded everything
else into failure. A timeout is neither. It is a statement about how long we
were willing to wait, and says nothing about what NetSuite committed.

WHAT THIS COST, measured. Sandbox 6738075-sb1, 2026-08-27: ns_createRecord
exceeded the ceiling, NetSuite created customer 5264348 anyway, and the app
recorded the write as ``failed``, told the operator so, and opened a repair
card carrying a byte-identical payload. Approving it would have created a
second customer. The record existed the whole time; only our knowledge of it
was missing.

WHY A THIRD STATE RATHER THAN A BETTER GUESS. ``write_repair_bound`` states
its own precondition: it decides what to do when a write is "rejected BY
NETSUITE". That contract is sound — a rejected payload is worth recomposing,
because the payload is what was wrong. A transport failure is outside it: the
payload may have been perfect, and recomposing it risks a duplicate rather
than a fix. The two need different handling, so they need different names.

The signal is STRUCTURAL, set by the code that catches the timeout (see
``mcp_client_service``), never sniffed out of an error string here. Matching
on message text would make the guarantee depend on wording nobody owns.

Deliberately narrow: only an explicitly-marked result or one we could not
parse at all is indeterminate. An empty dict that parsed cleanly is success.
Widening "unknown" would turn ordinary writes into reconciliation prompts,
and a reconciliation prompt the operator learns to dismiss is worse than none.
"""

from __future__ import annotations

from typing import Any, Literal

__all__ = ["INDETERMINATE_KEY", "WriteOutcome", "classify_write_outcome", "may_enter_repair_loop"]

# Set by mcp_client_service at the point a call times out or returns nothing.
# The name is deliberately not a NetSuite field shape: it is ours, and no
# NetSuite response can collide with it.
INDETERMINATE_KEY = "outcome_indeterminate"

WriteOutcome = Literal["success", "failed", "indeterminate"]


def classify_write_outcome(result: Any, *, unparseable: bool = False) -> WriteOutcome:
    """Return the outcome of an executed write.

    *unparseable* is for the caller's ``json.JSONDecodeError`` branch — a
    response we could not read is the definition of an unknown outcome. That
    branch previously reported SUCCESS, telling the operator a write had
    executed on the strength of a response nobody could parse.

    The indeterminate checks come FIRST and on purpose: a timeout result
    carries an ``error`` string too, so testing the error branch first would
    misfile every timeout as a NetSuite rejection and hand it to the repair
    loop — the exact duplicate-inviting path this exists to close.
    """
    if unparseable:
        return "indeterminate"
    if not isinstance(result, dict):
        return "indeterminate"
    if result.get(INDETERMINATE_KEY) is True:
        return "indeterminate"

    # Reuse the repo's existing predicate rather than invent a fourth variant
    # — identical to extract_result_payload and the orchestrator's sibling SSE
    # guard. `is False`, not `is not True`, so ns_createRecord's ordinary
    # `{"id": ...}` response (no `success` key) still counts as success.
    from app.services.chat.tool_call_results import _extract_error_message

    if _extract_error_message(result) or result.get("success") is False:
        return "failed"
    return "success"


def may_enter_repair_loop(outcome: WriteOutcome) -> bool:
    """Only a determinate NetSuite rejection may be recomposed and re-proposed.

    An indeterminate write must never re-enter: the record may already exist,
    and a repair card is an invitation to write it twice. Reconcile against
    NetSuite first; a human decides from there.
    """
    return outcome == "failed"
