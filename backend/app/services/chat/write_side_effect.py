"""Idempotency key + outcome classification for NetSuite writes.

THE PROBLEM. A write has two outcomes the system can record and a third that
actually happens: the request was sent and no answer came back. On 2026-08-27
that third case created sandbox customer 5264348 while the app reported
``failed`` and offered to run the identical payload again. PR #210 taught the
CARD to say "indeterminate"; it gave the system nowhere to record that a call
was in flight when the process died, and no way to find out afterwards.

``.claude/rules/agent-graph.md`` #10: "Every external write needs a work-derived
idempotency key and a side-effect log written *before* the call, so a crash
between send and confirm is recoverable."

HOW IT WORKS HERE, settled against live sandbox 6738075-sb1 on 2026-09-01 (see
``docs/superpowers/specs/2026-08-31-batch-write-idempotency-design.md``):

* There is **no header channel**. ``ns_createRecord`` accepts exactly
  ``recordType`` and ``data`` — read from the live MCP server's own schema. So
  the key travels *inside the payload*, as ``externalId``.
* NetSuite **enforces uniqueness on externalId itself**. Two identical creates
  produced ``recordId 5264548`` then ``HTTP 400 … "This entity already exists."``
  and a row count of 1.

That is stronger than a client-supplied header would be: the guarantee is
enforced server-side rather than trusted from us. A blind retry *cannot* create
a second record, and the refusal is distinguishable — so a retry that hits it
proves the original landed, which is exactly the fact the unknown state needs.

FAIL-CLOSED IS THE RULE. ``WRITTEN`` is never a default. Anything unparseable,
empty, or merely indeterminate stays ``ATTEMPTED`` — on the money path, "we
could not tell" must never render as "it worked". That inversion is the defect
this module exists to end, and it has already shipped once.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

__all__ = [
    "IDEM_PREFIX",
    "SideEffectStatus",
    "build_idempotency_key",
    "classify_retry_result",
    "payload_with_idempotency_key",
]

# Namespace so a value WE generated is identifiable on sight. externalId is a
# real business field that integrations commonly own (Celigo among them); an
# unprefixed hash sitting in it would be indistinguishable from a customer's
# own key, and nobody could later tell which system wrote it.
IDEM_PREFIX = "ss-idem-"

# NetSuite bounds externalId length; 24 hex chars of SHA-256 leaves ~10^-14
# collision probability across a million writes while keeping the value short
# enough to read in a UI and in a SuiteQL WHERE clause.
_DIGEST_CHARS = 24


class SideEffectStatus(str, Enum):
    """The four states a write can be in.

    ``ATTEMPTED`` is the important one and the reason this module exists: it is
    written BEFORE the call and is what a crash between send and confirm leaves
    behind. It is the true state, and the one the system previously could not
    represent — so a killed process left a write that had either happened or
    not, with nothing recording which.
    """

    ATTEMPTED = "attempted"
    WRITTEN = "written"
    REJECTED = "rejected"
    # Reserved for a row an operator has been told to resolve by hand — an
    # ATTEMPTED row that reconciliation could not settle (NetSuite unreachable,
    # or a record type with no queryable natural identity).
    UNKNOWN = "unknown"


def _external_id_in(payload: dict[str, Any]) -> tuple[str | None, Any]:
    """Find a caller-supplied externalId, ignoring case.

    The catalog spells it ``externalId`` and SuiteQL returns ``externalid`` —
    the identical split documented in ``required_field_registry.field_value``,
    where lowercase rule names silently missed every real customer create.
    """
    for key, value in payload.items():
        if isinstance(key, str) and key.lower() == "externalid":
            if isinstance(value, str) and value.strip():
                return key, value.strip()
            return key, None
    return None, None


def build_idempotency_key(*, batch_id: str | None, row_index: int | None, payload: Any) -> str:
    """A key derived from the WORK, never random.

    A random key would make every retry a new write — the defect wearing a
    disguise. Same work in, same key out; different work, different key.

    ``row_index`` participates because two rows of one file may legitimately be
    the same company (different contacts, say). That is different work and must
    not collapse onto a single key, which would silently drop the second.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")
    material = json.dumps(
        {"batch": batch_id, "row": row_index, "payload": payload},
        sort_keys=True,  # canonical: two serialisations of one payload must not retry as two writes
        separators=(",", ":"),
        default=str,
    )
    return IDEM_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def payload_with_idempotency_key(
    payload: dict[str, Any], *, batch_id: str | None, row_index: int | None
) -> tuple[dict[str, Any], str]:
    """Return ``(payload_with_external_id, key_actually_used)``.

    A caller-supplied ``externalId`` is **never** overwritten. Their key is a
    better natural identity than our hash — it is the one their other systems
    already reconcile against — and silently replacing it would corrupt an
    integration we do not own. In that case the returned key is theirs, because
    reconciliation must query the value that was actually written.

    Non-mutating: the caller's dict is left alone, so a failed write cannot
    leave a half-decorated payload behind.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")

    existing_key, existing_value = _external_id_in(payload)
    if existing_value:
        return dict(payload), existing_value

    key = build_idempotency_key(batch_id=batch_id, row_index=row_index, payload=payload)
    out = dict(payload)
    # Reuse the caller's spelling if they supplied an empty one, so we do not
    # end up with both externalId and externalid in the same payload.
    out[existing_key or "externalId"] = key
    return out, key


# NetSuite's wording for the uniqueness refusal, observed live 2026-09-01:
#   "Error while accessing a resource. This entity already exists."
# Matched on the o:errorDetails detail text rather than the HTTP status, because
# 400/USER_ERROR is also what an ordinary validation rejection returns.
_ALREADY_EXISTS = "this entity already exists"


def classify_retry_result(raw_result: str) -> SideEffectStatus:
    """Classify one write result into a side-effect status.

    The decisive case: NetSuite refusing a duplicate externalId means the
    ORIGINAL write landed. That is ``WRITTEN``, not ``REJECTED`` — treating it
    as failure would send the repair loop chasing a write that already
    succeeded, which is the exact duplicate-invitation shape PR #210 closed.

    Everything unclassifiable stays ``ATTEMPTED``. ``WRITTEN`` is never a
    default: on a path that mutates a customer's ledger, "we could not tell"
    rendering as "it worked" is the failure mode this whole module exists to
    prevent, and it has already shipped once.
    """
    from app.services.chat.write_outcome import classify_write_outcome
    from app.services.chat.write_repair_bound import extract_netsuite_error_details

    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        return SideEffectStatus.ATTEMPTED

    if not isinstance(parsed, dict) or not parsed:
        return SideEffectStatus.ATTEMPTED

    # The uniqueness refusal is checked FIRST: it arrives dressed as a failure
    # (success=false, HTTP 400, USER_ERROR) and every other branch would
    # misread it. Reuses the canonical o:errorDetails walker rather than a
    # second parser — one owner for that shape.
    haystack = " ".join(extract_netsuite_error_details(raw_result)).lower()
    if not haystack:
        haystack = json.dumps(parsed).lower()
    if _ALREADY_EXISTS in haystack:
        return SideEffectStatus.WRITTEN

    outcome = classify_write_outcome(parsed)
    if outcome == "success":
        return SideEffectStatus.WRITTEN
    if outcome == "failed":
        return SideEffectStatus.REJECTED
    return SideEffectStatus.ATTEMPTED
