"""``ns_selector_app`` is redirected to the slot mechanism that actually works.

WHY THIS EXISTS. NetSuite's MCP surface offers ``ns_selector_app``, which opens
a record picker inside NetSuite's own UI. To a model deciding "the user must
choose a subsidiary", it is the obviously right tool — and in this product it
is a dead end: nothing in our frontend or backend renders it, so the model
tells the operator "I've opened the subsidiary selector for you" and there is
no selector anywhere. Worse than prose, because it asserts an affordance that
does not exist.

Measured on staging 2026-08-26 across five attempts at "create a customer"
without naming a subsidiary: THREE ended in ``ns_selector_app`` (including two
where the prompt explicitly said to use ``ask_user`` and not to answer in
chat), and two ended in prose. No wording reached the write-confirmation
card's slot form, which has been built and shipped the whole time.

So the fix works WITH the model rather than against it. The model's intent is
already correct — it wants a human to pick from a list. It is calling the
wrong door. Intercepting the call and naming the right one turns a dead end
into the exact mechanism the design intended, and does it as code at a choke
point rather than a fourth attempt at prompt wording (three have been ignored).
"""

from __future__ import annotations

import json

import pytest

from app.services.chat.selector_app_redirect import (
    SELECTOR_TOOL,
    build_selector_redirect,
    is_selector_app_call,
)


def _ext(name: str) -> str:
    return "ext__" + "a" * 32 + "__" + name


def test_recognises_the_selector_tool_on_any_connector():
    assert is_selector_app_call(_ext(SELECTOR_TOOL)) is True
    assert is_selector_app_call("ext__" + "b" * 32 + "__ns_selector_app") is True


def test_does_not_touch_other_tools():
    """A narrow intercept. Anything else — including the metadata and
    subsidiary tools the write loop depends on — must dispatch normally."""
    for other in ("ns_getRecordTypeMetadata", "ns_getSubsidiaries", "ns_createRecord", "ns_runCustomSuiteQL"):
        assert is_selector_app_call(_ext(other)) is False
    assert is_selector_app_call("bigquery_sql") is False
    assert is_selector_app_call("") is False


def test_redirect_names_the_field_and_the_tool_to_call():
    """The message must be actionable: which field, and which call to make.
    A bare "unsupported" would leave the model to guess again."""
    out = json.loads(build_selector_redirect({"recordType": "subsidiary"}, mutation_record_type="customer"))
    assert out["selector_unavailable"] is True
    assert out["field"] == "subsidiary"
    instruction = out["instruction"]
    assert "ask_user" in instruction
    assert "subsidiary" in instruction
    assert "ns_createRecord" in instruction


def test_redirect_says_plainly_that_no_picker_opened():
    """The failure mode being fixed is the model TELLING the user a selector
    opened. The result must contradict that belief outright, or it will
    narrate the same falsehood anyway."""
    out = json.loads(build_selector_redirect({"recordType": "subsidiary"}, mutation_record_type="customer"))
    blob = json.dumps(out).lower()
    assert "not" in blob and "shown" in blob or "cannot" in blob


def test_unknown_record_type_still_redirects():
    """Even for a picker we have no option source for, opening a selector the
    UI cannot render is never the right outcome."""
    out = json.loads(build_selector_redirect({"recordType": "department"}, mutation_record_type="customer"))
    assert out["selector_unavailable"] is True
    assert out["field"] == "department"


@pytest.mark.parametrize("bad", [{}, {"recordType": ""}, {"recordType": None}, {"other": "x"}])
def test_malformed_input_never_raises(bad):
    """Malformed model output must degrade, not crash the turn."""
    out = json.loads(build_selector_redirect(bad, mutation_record_type=None))
    assert out["selector_unavailable"] is True
    assert isinstance(out["instruction"], str) and out["instruction"]


def test_redirect_is_pure_and_makes_no_network_call():
    """Cheap by construction — this runs inline in the streaming loop, so it
    must not add a round trip."""
    out = build_selector_redirect({"recordType": "subsidiary"}, mutation_record_type="customer")
    assert isinstance(out, str)
    json.loads(out)
