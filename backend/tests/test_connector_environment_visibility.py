"""The agent must be able to tell sandbox from production.

Today `build_external_tool_definitions` labels every tool
`[{connector.provider}]` — so two NetSuite connectors (a sandbox and a
production one, which is the whole point of the sandbox work) produce two
tools with IDENTICAL descriptions, distinguished only by an opaque 32-hex
connector UUID inside the tool NAME:

    ext__<uuid-A>__ns_createRecord   "[netsuite_mcp] Create a record…"
    ext__<uuid-B>__ns_createRecord   "[netsuite_mcp] Create a record…"

The model has no way to know which one is real money, so it picks arbitrarily.
Connecting a sandbox in that state makes writes MORE dangerous, not less: the
operator says "test in sandbox" and nobody — model, card, or log reader — can
tell where it went.

This is NOT the enforcement described in
docs/superpowers/specs/2026-08-27-sandbox-environment-binding-design.md. It is
the visibility half: it moves the model from "cannot possibly know" to "can
plainly see", and gives a human reading tool_calls_log the same. Enforcement
(refusing a write to the environment the session did not choose) still has to
be built at the dispatcher.
"""

from __future__ import annotations

import uuid

from app.services.chat.tools import build_external_tool_definitions


class _Conn:
    def __init__(self, account_id: str, label: str = ""):
        self.id = uuid.uuid4()
        self.provider = "netsuite_mcp"
        self.label = label
        self.metadata_json = {"account_id": account_id}
        self.discovered_tools = [
            {"name": "ns_createRecord", "description": "Create a record.", "input_schema": {"type": "object"}}
        ]


def _desc(conn) -> str:
    return build_external_tool_definitions([conn])[0]["description"]


def test_production_account_is_named_in_the_description():
    d = _desc(_Conn("6738075"))
    assert "PRODUCTION" in d
    assert "6738075" in d


def test_sandbox_account_is_named_in_the_description():
    """NetSuite sandbox account ids carry an _SB suffix."""
    d = _desc(_Conn("6738075_SB1"))
    assert "SANDBOX" in d
    assert "6738075_SB1" in d
    assert "PRODUCTION" not in d


def test_release_preview_counts_as_non_production():
    """_RP is a release-preview account — also not real money."""
    assert "SANDBOX" in _desc(_Conn("6738075_RP"))


def test_two_connectors_are_distinguishable():
    """The property that actually matters: same tool, two connectors, and the
    descriptions must DIFFER. This is what fails today."""
    prod, sand = _Conn("6738075"), _Conn("6738075_SB1")
    tools = build_external_tool_definitions([prod, sand])
    descs = [t["description"] for t in tools]
    assert len(descs) == 2
    assert descs[0] != descs[1], "the model cannot choose safely between identical descriptions"


def test_an_unknown_account_is_treated_as_production():
    """Fail toward caution: if the suffix is unrecognised, say PRODUCTION.
    Mislabelling production as sandbox invites a careless write; the reverse
    only invites care."""
    assert "PRODUCTION" in _desc(_Conn("weird-account-name"))


def test_a_connector_with_no_account_id_still_builds():
    """Never break tool building over a display concern."""
    c = _Conn("")
    c.metadata_json = {}
    d = _desc(c)
    assert "netsuite_mcp" in d
    assert "Create a record." in d


def test_the_original_description_is_preserved():
    """Oracle bakes SuiteQL dialect rules into these descriptions; the vs-MCP
    benchmark depends on passing them through unchanged."""
    assert "Create a record." in _desc(_Conn("6738075"))


# ---------------------------------------------------------------------------
# The human's half: the confirmation card must name where the write is going
# ---------------------------------------------------------------------------
#
# Making the MODEL able to see the difference is not enough. The human is the
# last gate, and the card they approve said nothing about which NetSuite
# account it targets — so with a sandbox and a production connector both
# connected, an operator could approve a write with no way to tell which books
# it lands in. Same defect as the blank-cheque card, one level up: the decision
# is real, the information needed to make it is absent.


def _card(**kw):
    from app.services.chat.write_confirmation_service import build_confirmation_payload

    base = dict(
        mutation_type="create",
        record_type="customer",
        tool_name="ext__" + "a" * 32 + "__ns_createRecord",
        tool_input={"recordType": "customer", "data": '{"companyName": "Acme"}'},
        session_id="s",
        validation=None,
    )
    base.update(kw)
    return build_confirmation_payload(**base)


def test_card_names_the_target_environment_and_account():
    p = _card(target_account="6738075", target_environment="PRODUCTION")
    assert p is not None
    assert p.target_account == "6738075"
    assert p.target_environment == "PRODUCTION"


def test_card_marks_a_sandbox_target():
    p = _card(target_account="6738075_SB1", target_environment="SANDBOX")
    assert p.target_environment == "SANDBOX"


def test_card_omits_the_target_when_unknown():
    """Never guess. An unknown target renders nothing rather than asserting
    PRODUCTION on a card a human is about to approve."""
    p = _card()
    assert p.target_account is None
    assert p.target_environment is None
