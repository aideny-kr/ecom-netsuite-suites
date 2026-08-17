"""The OAuth state blob must not be positionally parseable.

Gate round 8 found a cross-tenant takeover: /authorize packed
`f"{verifier}:{account_id}:{tenant_id}:{user_id}|..."` into Redis and the
unauthenticated callback pulled it apart with `split(":")`, so a colon inside
account_id shifted every following field and the callback read an attacker-chosen
tenant_id. The fix at the time validated account_id -- at ONE of the SIX producers.

The final round found the other five, and a second injectable field: `label` lands in
position 5 while `is_reauth` keys off `parts[6] == "reauth"`, so a label ending in
":reauth:<uuid>" repoints an EXISTING connector in the victim tenant. Validating
account_id everywhere would still have left that open.

So the encoding changes rather than the fields being policed. A JSON object has no
positional fields to shift: no value of any field can change how any other field is
read. That is one encoder and one decoder, instead of six call sites that each have to
remember -- which is the same shape as the render_callback funnel, adopted for the same
reason (three consecutive half-applied fixes).

Deploy note: states written by the old format live at most 600s. They no longer decode,
so an OAuth flow in flight across the deploy fails with the existing "Invalid or expired
state parameter. Please try again." Accepting the legacy format on read would keep the
vulnerability alive for the whole TTL, which is the thing being removed.
"""

from __future__ import annotations

import pytest

from app.api.v1.oauth_state import InvalidOAuthStateError, decode_state, encode_state

VICTIM = "bf92d059-4f45-492d-b920-d0390f6fb77a"
ATTACKER = "ce3dfaad-626f-4992-84e9-500c8291ca0a"
USER = "11111111-1111-1111-1111-111111111111"


def test_round_trips_every_field():
    payload = {
        "code_verifier": "v",
        "account_id": "1234567",
        "tenant_id": ATTACKER,
        "user_id": USER,
        "restlet_url": "https://x.restlets.api.netsuite.com/app/site/hosting/restlet.nl?script=1&deploy=1",
        "client_id": "abc123",
    }
    assert decode_state(encode_state(**payload)) == payload


@pytest.mark.parametrize(
    "hostile",
    [
        f"1234567:{VICTIM}:{USER}",  # the round-8 takeover
        f"1234567:CID:{VICTIM}:{USER}",  # its MCP variant
        "1234567|evil",  # the pipe half of the old scheme
        '1234567", "tenant_id": "' + VICTIM,  # trying to break out of JSON itself
    ],
)
def test_a_hostile_account_id_cannot_move_any_other_field(hostile: str):
    """The whole class: no value of one field may change how another is read."""
    raw = encode_state(
        code_verifier="v",
        account_id=hostile,
        tenant_id=ATTACKER,
        user_id=USER,
    )
    decoded = decode_state(raw)

    assert decoded["tenant_id"] == ATTACKER, "account_id smuggled a different tenant"
    assert decoded["user_id"] == USER
    assert decoded["account_id"] == hostile, "the value must survive intact, just inert"


def test_a_hostile_label_cannot_forge_the_reauth_flag():
    """`label` is user-supplied and sat next to the positional reauth marker.

    In the old encoding `is_reauth` was `parts[6] == "reauth"`, so a label ending in
    ":reauth:<connector uuid>" repointed an existing connector in whatever tenant the
    same trick selected.
    """
    victim_connector = "22222222-2222-2222-2222-222222222222"
    raw = encode_state(
        code_verifier="v",
        account_id="1234567",
        client_id="cid",
        tenant_id=ATTACKER,
        user_id=USER,
        label=f"my label:reauth:{victim_connector}",
    )
    decoded = decode_state(raw)

    assert decoded.get("reauth_connector_id") is None, "label forged a reauth target"
    assert decoded["label"] == f"my label:reauth:{victim_connector}"


def test_a_genuine_reauth_is_carried_as_its_own_field():
    connector_id = "33333333-3333-3333-3333-333333333333"
    decoded = decode_state(
        encode_state(
            code_verifier="v",
            account_id="1234567",
            tenant_id=ATTACKER,
            user_id=USER,
            reauth_connector_id=connector_id,
        )
    )
    assert decoded["reauth_connector_id"] == connector_id


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "VERIFIER:1234567:tenant:user|restlet|client",  # the legacy colon format
        "[]",  # valid JSON, wrong shape
        '"a string"',
    ],
)
def test_undecodable_state_is_rejected_not_guessed(raw: str):
    """Fail closed. A state we cannot read is not a state we may act on.

    The legacy colon format is deliberately in this list: accepting it on read would
    keep the injection alive for the full 600s TTL of every pre-deploy blob.
    """
    with pytest.raises(InvalidOAuthStateError):
        decode_state(raw)


def test_missing_required_fields_are_rejected():
    import json

    with pytest.raises(InvalidOAuthStateError):
        decode_state(json.dumps({"account_id": "1234567"}))


# ---------------------------------------------------------------------------
# No producer may hand-roll the blob again
# ---------------------------------------------------------------------------


def test_every_producer_uses_the_encoder():
    """Six producers wrote this blob; a fix applied to one of them is not a fix.

    That was the actual failure: `account_id` was validated at netsuite_auth's
    /authorize while mcp_connectors (x2), connections' reconnect and onboarding (x2)
    kept writing raw delimiter-joined strings straight into the same Redis keys the
    unauthenticated callback trusts.

    Walks the AST rather than the text: a regex over source also matches the module
    docstrings that DESCRIBE the old format, which is prose, not a producer.
    """
    import ast
    from pathlib import Path

    api_v1 = Path(__file__).resolve().parents[1] / "app" / "api" / "v1"
    offenders = []
    for path in api_v1.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            # An f-string whose literal parts are only delimiters, joining 3+ values:
            # the shape of the old positional encoding.
            values = [v for v in node.values if isinstance(v, ast.FormattedValue)]
            literals = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if len(values) >= 3 and literals and set(literals) <= {":", "|"}:
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], (
        f"{offenders} build an OAuth state by string-joining — use encode_state(), or the "
        "positional-injection class comes straight back"
    )


def test_state_keys_are_only_written_via_encode_state():
    """The `setex` calls themselves must pass encode_state(...), not an f-string."""
    import re
    from pathlib import Path

    api_v1 = Path(__file__).resolve().parents[1] / "app" / "api" / "v1"
    offenders = []
    for path in api_v1.glob("*.py"):
        text = path.read_text()
        for m in re.finditer(r'f"netsuite(?:_mcp)?_oauth:\{state\}",\s*\n\s*\d+,\s*\n(\s*.+)', text):
            payload = m.group(1).strip()
            if not payload.startswith("encode_state("):
                offenders.append(f"{path.name}: {payload[:60]}")

    assert offenders == [], f"{offenders} write an OAuth state without encode_state()"
