"""The NetSuite OAuth state blob: one encoder, one decoder, no positional fields.

WHY THIS EXISTS. The state was a delimiter-joined string parsed by index --
`f"{verifier}:{account_id}:{tenant_id}:{user_id}|{restlet_url}|{client_id}"` read back
with `split(":", maxsplit=3)`. Every field was therefore at the mercy of every field
before it, and the endpoint that consumes the state (`/connections/netsuite/callback`)
has NO authentication: whatever the blob says the tenant is, is the tenant the callback
writes to.

Two live injections came out of that:

  * a colon in `account_id` shifted `tenant_id`, so an admin of their own tenant could
    make the callback write a connection -- carrying THEIR NetSuite credentials -- into
    a victim tenant, whose SuiteQL, pivots, reports and recon syncs then read the
    attacker's books. The supersede logic added on this branch made it worse: the
    victim's real rows are demoted to `superseded`, a status excluded from
    health-check, refresh and dispatch, so they stay permanently dead.
  * a colon in `label` forged the positional `reauth` marker (`parts[6] == "reauth"`),
    repointing an EXISTING connector in the victim tenant instead of adding one.

WHY ENCODING RATHER THAN VALIDATION. The first fix validated `account_id` at one of the
SIX producers (netsuite_auth, mcp_connectors x2, connections reconnect, onboarding x2).
Finding the other five is the same "fix applied to one of N call sites" that had already
happened twice on this branch, with the callback template and the escaping funnel. And
even a perfect six-site rollout would have missed `label`, which is a different field
with the same disease. JSON has no positional fields: no value of one key can change how
another key is read, whatever any producer passes.

LEGACY BLOBS ARE NOT ACCEPTED. Old-format states live at most 600s (the Redis TTL). They
no longer decode, so an OAuth flow in flight across the deploy fails with the callback's
existing "Invalid or expired state parameter. Please try again." -- a handled path that
tells the user to retry. Accepting the old format on read would keep the injection alive
for the whole TTL, which is precisely what is being removed.
"""

from __future__ import annotations

import json

# Present in every flow. `client_id`, `restlet_url`, `label` and `reauth_connector_id`
# are flow-specific and optional.
_REQUIRED = ("code_verifier", "account_id", "tenant_id", "user_id")


class InvalidOAuthStateError(ValueError):
    """The stored state is absent, unreadable, or missing a required field.

    Callers must treat this as "invalid or expired state" and abort the flow. There is
    no partial recovery: a state we cannot fully read is one we must not act on, since
    acting on it means writing credentials into whichever tenant it names.
    """


def encode_state(**fields: str | None) -> str:
    """Serialise the OAuth state for Redis.

    Values are stored verbatim -- hostile input is rendered inert by the encoding, not
    by sanitising it, so nothing legitimate is silently mangled on the way through.
    """
    missing = [k for k in _REQUIRED if not fields.get(k)]
    if missing:
        raise InvalidOAuthStateError(f"cannot encode state without {', '.join(missing)}")
    return json.dumps({k: v for k, v in fields.items() if v is not None}, separators=(",", ":"))


def decode_state(raw: str | None) -> dict[str, str]:
    """Parse a stored OAuth state, or raise InvalidOAuthStateError.

    Fails closed on anything that is not a JSON object carrying every required field --
    including the pre-2026-08-16 delimiter format, deliberately (see module docstring).
    """
    if not raw:
        raise InvalidOAuthStateError("state is empty")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise InvalidOAuthStateError("state is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise InvalidOAuthStateError(f"state must be a JSON object, got {type(parsed).__name__}")
    missing = [k for k in _REQUIRED if not parsed.get(k)]
    if missing:
        raise InvalidOAuthStateError(f"state is missing {', '.join(missing)}")
    return {k: v for k, v in parsed.items() if v is not None}
