"""The curated required-field registry, and its merge onto live metadata.

Why this module exists at all: ``ns_getRecordTypeMetadata`` returns a JSON
Schema that carries field NAMES and nothing about requiredness — no
``required`` array, and ``nullable`` is ``false`` on zero of 177 customer
fields (controller-verified 2026-08-25). ``ns_getSuiteQLMetadata`` returns the
identical shape, and the one queryable mandatoriness source (SuiteQL
``CustomField.ismandatory``) covers CUSTOM fields only — never ``subsidiary``
or ``companyname``. So "ask NetSuite what's required" is unsatisfiable, and a
human-curated registry is the only path to a card that can ask for what's
missing.

The registry's central risk runs OPPOSITE to the usual one: a field wrongly
marked required BLOCKS a write NetSuite would have accepted, and that failure
looks to an operator like the product being broken. Several tests below exist
purely to hold that line.
"""

from __future__ import annotations

import pytest

from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata
from app.services.chat.required_field_registry import (
    Provenance,
    apply_curated_requirements,
    requirements_for,
)
from app.services.chat.write_payload import NormalizedPayload
from app.services.chat.write_validator import validate_write

# ── The registry's contents ────────────────────────────────────────────────


def test_customer_requires_subsidiary():
    """The one entry proven against this account: two recorded rejections
    naming "Primary Subsidiary", plus a same-session fail->succeed pair that
    established the API name."""
    rules = requirements_for("customer")
    assert rules is not None
    by_name = {r.name: r for r in rules}
    assert "subsidiary" in by_name
    assert by_name["subsidiary"].provenance is Provenance.ACCOUNT_EVIDENCED


def test_uncurated_record_type_returns_none():
    """None means "not curated" — distinct from "curated, requires nothing".
    Callers must leave such a record type unvalidated rather than claim it is
    complete."""
    assert requirements_for("expensereport") is None
    assert requirements_for("somethingmadeup") is None


def test_lookup_is_case_insensitive():
    """record_type reaches us from model-composed tool input, so its casing is
    not ours to assume."""
    assert requirements_for("Customer") == requirements_for("customer")
    assert requirements_for("SalesOrder") == requirements_for("salesorder")


def test_transactions_with_an_entity_do_not_require_subsidiary():
    """The trap this registry must not fall into. On a transaction that
    carries an entity, NetSuite DERIVES subsidiary from that entity — it is
    required=false. Marking it required here would block writes NetSuite
    accepts."""
    for record_type in ("invoice", "salesorder", "vendorbill"):
        rules = requirements_for(record_type)
        assert rules is not None, record_type
        assert "subsidiary" not in {r.name for r in rules}, record_type


def test_no_rule_for_a_field_netsuite_defaults_itself():
    """trandate defaults to today when omitted, so requiring it would be a
    false block. Named explicitly because it is the most tempting wrong
    entry — it appears required on every transaction form."""
    for record_type in ("journalentry", "invoice", "salesorder", "vendorbill"):
        rules = requirements_for(record_type) or ()
        assert "trandate" not in {r.name for r in rules}, record_type


def test_every_rule_carries_provenance_and_evidence():
    """A registry entry with no stated basis is indistinguishable from a
    guess, and a guess here blocks real writes."""
    from app.services.chat.required_field_registry import _REGISTRY

    assert _REGISTRY, "registry must not be empty"
    for record_type, rules in _REGISTRY.items():
        assert record_type == record_type.lower(), f"{record_type} key must be lowercase"
        assert rules, f"{record_type} has an empty rule list"
        for rule in rules:
            assert rule.name, f"{record_type}: rule with empty name"
            assert rule.label, f"{record_type}.{rule.name}: empty label"
            assert isinstance(rule.provenance, Provenance), f"{record_type}.{rule.name}"
            assert rule.evidence.strip(), f"{record_type}.{rule.name}: empty evidence"


# ── Conditional rules ──────────────────────────────────────────────────────


def test_company_customer_needs_companyname_not_lastname():
    """isperson absent means a company (NetSuite's own default)."""
    names = {r.name for r in requirements_for("customer") if r.applies_to({})}
    assert "companyname" in names
    assert "lastname" not in names


def test_individual_customer_needs_lastname_not_companyname():
    names = {r.name for r in requirements_for("customer") if r.applies_to({"isperson": True})}
    assert "lastname" in names
    assert "companyname" not in names


def test_individual_flag_accepts_netsuite_string_booleans():
    """NetSuite serialises booleans as "T"/"F" strings. bool("F") is True in
    Python, so a bare truthiness test here would invert the rule."""
    person = {r.name for r in requirements_for("customer") if r.applies_to({"isperson": "T"})}
    company = {r.name for r in requirements_for("customer") if r.applies_to({"isperson": "F"})}
    assert "lastname" in person and "companyname" not in person
    assert "companyname" in company and "lastname" not in company


# ── The merge onto live metadata ───────────────────────────────────────────


def test_merge_leaves_none_metadata_as_none():
    """A failed metadata fetch stays a failed fetch. The registry adds
    requirements to metadata we HAVE; it does not manufacture metadata we
    could not read."""
    assert apply_curated_requirements(None, record_type="customer", fields={}) is None


def test_merge_leaves_uncurated_record_type_untouched():
    meta = RecordMetadata(
        record_type="expensereport",
        fields=[FieldSpec(name="memo", label="Memo")],
        requirements_known=False,
    )
    merged = apply_curated_requirements(meta, record_type="expensereport", fields={})
    assert merged.requirements_known is False
    assert merged.required_field_names() == []
    assert merged.requirements_source is None


def test_merge_marks_curated_fields_required_and_flips_requirements_known():
    """This is the whole point: the live shape yields requirements_known=False,
    which makes the validator return early with an empty missing_required. The
    merge is what gives the card something to ask for."""
    meta = RecordMetadata(
        record_type="customer",
        fields=[
            FieldSpec(name="subsidiary", label="Primary Subsidiary", type="integer"),
            FieldSpec(name="companyname", label="Company Name"),
            FieldSpec(name="memo", label="Memo"),
        ],
        requirements_known=False,
    )
    merged = apply_curated_requirements(meta, record_type="customer", fields={})

    assert merged.requirements_known is True
    assert merged.requirements_source == "curated_registry"
    assert set(merged.required_field_names()) == {"subsidiary", "companyname"}
    assert merged.spec_for("memo").required is False


def test_merge_preserves_live_label_and_type():
    """NetSuite's own label beats ours — it is what the operator sees on the
    NetSuite form."""
    meta = RecordMetadata(
        record_type="customer",
        fields=[FieldSpec(name="subsidiary", label="Subsidiary (Primary)", type="integer")],
        requirements_known=False,
    )
    merged = apply_curated_requirements(meta, record_type="customer", fields={})
    spec = merged.spec_for("subsidiary")
    assert spec.label == "Subsidiary (Primary)"
    assert spec.type == "integer"


def test_a_field_the_account_does_not_expose_is_not_required():
    """T2 gate finding (major): `subsidiary` exists on customer/vendor/journal
    entry only when the account has OneWorld enabled. A single-entity account's
    NetSuite has no such field at all, so requiring it there would block every
    customer create with a question the operator cannot answer — the exact
    "wrong entry is worse than a missing one" failure this registry is supposed
    to avoid. Presence in the account's own field catalog IS the OneWorld
    gate, and it needs no extra round trip: we already hold the catalog.
    """
    single_entity = RecordMetadata(
        record_type="customer",
        fields=[
            FieldSpec(name="companyname", label="Company Name"),
            FieldSpec(name="email", label="Email"),
        ],
        requirements_known=False,
    )
    merged = apply_curated_requirements(single_entity, record_type="customer", fields={})

    assert merged.required_field_names() == ["companyname"]
    assert merged.spec_for("subsidiary") is None
    # Still a completed check — we consulted the catalog and it settled the
    # question. Reporting "unknown" here would drop back to an unvalidated
    # card for an account we CAN validate.
    assert merged.requirements_known is True


def test_an_empty_field_catalog_is_unknown_not_permissive():
    """Zero fields means the catalog told us nothing, not that the record type
    requires nothing. Treating it as "checked, nothing required" would let an
    empty payload through claiming it had been validated."""
    meta = RecordMetadata(record_type="customer", fields=[], requirements_known=False)
    merged = apply_curated_requirements(meta, record_type="customer", fields={})
    assert merged.requirements_known is False
    assert merged.requirements_source is None
    assert merged.required_field_names() == []


def test_merge_does_not_mutate_the_cached_input():
    """get_record_metadata caches its RecordMetadata. Mutating it in place
    would let one call's conditional rules leak into the next call's
    metadata — a customer create with isperson=True would poison the cached
    entry for every later company create."""
    meta = RecordMetadata(
        record_type="customer",
        fields=[FieldSpec(name="subsidiary", label="Primary Subsidiary")],
        requirements_known=False,
    )
    apply_curated_requirements(meta, record_type="customer", fields={})
    assert meta.requirements_known is False
    assert meta.spec_for("subsidiary").required is False


def test_merge_applies_conditions_against_the_payload():
    meta = RecordMetadata(
        record_type="customer",
        fields=[
            FieldSpec(name="subsidiary", label="Primary Subsidiary"),
            FieldSpec(name="companyname", label="Company Name"),
            FieldSpec(name="lastname", label="Last Name"),
        ],
        requirements_known=False,
    )
    company = apply_curated_requirements(meta, record_type="customer", fields={"companyname": "Acme"})
    person = apply_curated_requirements(meta, record_type="customer", fields={"isperson": "T"})

    assert "companyname" in company.required_field_names()
    assert "lastname" not in company.required_field_names()
    assert "lastname" in person.required_field_names()
    assert "companyname" not in person.required_field_names()


# ── End to end through the validator ───────────────────────────────────────


def test_customer_create_missing_subsidiary_now_declares_a_slot():
    """The operator-visible outcome: before the registry this produced
    unvalidated=True with no slots, so the card had nothing to ask for and the
    agent picked a subsidiary by reasoning alone."""
    meta = apply_curated_requirements(
        RecordMetadata(
            record_type="customer",
            fields=[
                FieldSpec(name="subsidiary", label="Primary Subsidiary"),
                FieldSpec(name="companyname", label="Company Name"),
            ],
            requirements_known=False,
        ),
        record_type="customer",
        fields={"companyname": "Acme Corp"},
    )
    result = validate_write(
        payload=NormalizedPayload(
            fields={"companyname": "Acme Corp"},
            lines=[],
            record_id=None,
            record={"companyname": "Acme Corp"},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )

    assert result.ok is False
    assert result.unvalidated is False
    assert result.missing_required == ["subsidiary"]
    assert [s.name for s in result.editable_slots] == ["subsidiary"]


def test_complete_customer_create_passes():
    meta = apply_curated_requirements(
        RecordMetadata(
            record_type="customer",
            fields=[
                FieldSpec(name="subsidiary", label="Primary Subsidiary"),
                FieldSpec(name="companyname", label="Company Name"),
            ],
            requirements_known=False,
        ),
        record_type="customer",
        fields={"companyname": "Acme Corp", "subsidiary": "1"},
    )
    result = validate_write(
        payload=NormalizedPayload(
            fields={"companyname": "Acme Corp", "subsidiary": "1"},
            lines=[],
            record_id=None,
            record={"companyname": "Acme Corp", "subsidiary": "1"},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is True
    assert result.unvalidated is False
    assert result.missing_required == []


def test_update_still_accepts_a_partial_payload():
    """Regression guard: requiring the full set on an update would break every
    rename. validate_write already scopes the sweep to create/upsert — the
    registry must not change that."""
    meta = apply_curated_requirements(
        RecordMetadata(
            record_type="customer",
            fields=[
                FieldSpec(name="subsidiary", label="Primary Subsidiary"),
                FieldSpec(name="companyname", label="Company Name"),
                FieldSpec(name="phone", label="Phone"),
            ],
            requirements_known=False,
        ),
        record_type="customer",
        fields={"phone": "555-0100"},
    )
    result = validate_write(
        payload=NormalizedPayload(
            fields={"phone": "555-0100"},
            lines=[],
            record_id="123",
            record={"phone": "555-0100"},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="update",
    )
    assert result.ok is True
    assert result.missing_required == []


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_a_required_field_present_but_blank_still_counts_as_missing(empty):
    """ "Professional" means a real value, not a placeholder that satisfies a
    presence check."""
    meta = apply_curated_requirements(
        RecordMetadata(
            record_type="customer",
            fields=[FieldSpec(name="subsidiary", label="Primary Subsidiary")],
            requirements_known=False,
        ),
        record_type="customer",
        fields={"companyname": "Acme", "subsidiary": empty},
    )
    result = validate_write(
        payload=NormalizedPayload(
            fields={"companyname": "Acme", "subsidiary": empty},
            lines=[],
            record_id=None,
            record={},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert "subsidiary" in result.missing_required


def test_curated_label_fills_in_when_the_catalog_had_no_title():
    """`_parse_properties_shape` falls back to the raw API name when a field
    carries no `title`. Asking an operator to fill in "subsidiary" is worse
    than asking for "Primary Subsidiary", so the curated label wins there —
    and only there."""
    meta = RecordMetadata(
        record_type="customer",
        fields=[FieldSpec(name="subsidiary", label="subsidiary")],
        requirements_known=False,
    )
    merged = apply_curated_requirements(meta, record_type="customer", fields={})
    assert merged.spec_for("subsidiary").label == "Primary Subsidiary"


# ---------------------------------------------------------------------------
# Against the REAL catalog shape, not hand-written field names
# ---------------------------------------------------------------------------
#
# Every test above this line invented its own field names, and invented them in
# lowercase. That shared assumption is why none of them could catch the bug
# this section exists for: NetSuite's REST metadata catalog spells entity
# fields in camelCase (`companyName`, `lastName`, `isPerson`) while SuiteQL
# spells them lowercase. With the registry keyed lowercase, the presence gate
# silently skipped `companyname`/`lastname` on every real customer create —
# failing in the SAFE direction (a skipped requirement, not a false block),
# which is precisely why it looked fine on staging.
#
# Fixture is the verbatim live response for account 6738075, captured
# 2026-08-25. It also pins the premise this whole registry rests on: nullable
# is false on ZERO of the 177 fields.

import json
import pathlib

from app.services.chat.record_metadata_service import _parse_properties_shape

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "netsuite_metadata" / "customer_properties_shape.json"


def _live_customer_metadata():
    return _parse_properties_shape(json.loads(_FIXTURE.read_text()), "customer")


def test_fixture_really_is_the_camelcase_shape():
    """Guards the premise of the tests below — if NetSuite ever switches to
    lowercase, these tests must be re-derived, not quietly re-passed."""
    meta = _live_customer_metadata()
    names = {f.name for f in meta.fields}
    assert {"companyName", "lastName", "isPerson", "subsidiary"} <= names
    assert "companyname" not in names
    assert meta.requirements_known is False


def test_company_name_is_required_against_the_real_catalog():
    meta = apply_curated_requirements(_live_customer_metadata(), record_type="customer", fields={})
    required = set(meta.required_field_names())
    assert "subsidiary" in required
    assert "companyName" in required, "the curated companyname rule must match the catalog's companyName"


def test_required_names_use_the_catalog_spelling_so_the_payload_lookup_matches():
    """validate_write does payload.fields.get(name) with the name straight off
    the FieldSpec. If the registry reported its own lowercase spelling, every
    lookup would miss and a populated field would read as missing."""
    meta = apply_curated_requirements(_live_customer_metadata(), record_type="customer", fields={})
    result = validate_write(
        payload=NormalizedPayload(
            fields={"companyName": "Acme Test Corp", "subsidiary": {"id": "5"}},
            lines=[],
            record_id=None,
            record={},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert result.missing_required == []
    assert result.ok is True


def test_a_nameless_company_create_is_caught_against_the_real_catalog():
    """The whole point: before the casing fix this passed validation, because
    the companyname rule was skipped as 'not exposed by this account'."""
    meta = apply_curated_requirements(_live_customer_metadata(), record_type="customer", fields={})
    result = validate_write(
        payload=NormalizedPayload(
            fields={"subsidiary": {"id": "5"}}, lines=[], record_id=None, record={}, payload_key="data"
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is False
    assert "companyName" in result.missing_required


def test_isperson_condition_reads_the_camelcase_payload_key():
    """The payload carries isPerson; the rule predicate looked for isperson, so
    every individual create was graded as a company."""
    meta = apply_curated_requirements(_live_customer_metadata(), record_type="customer", fields={"isPerson": True})
    required = set(meta.required_field_names())
    assert "lastName" in required
    assert "companyName" not in required


def test_individual_create_with_only_a_last_name_validates():
    meta = apply_curated_requirements(
        _live_customer_metadata(),
        record_type="customer",
        fields={"isPerson": True, "lastName": "Tester", "subsidiary": {"id": "1"}},
    )
    result = validate_write(
        payload=NormalizedPayload(
            fields={"isPerson": True, "firstName": "Ada", "lastName": "Tester", "subsidiary": {"id": "1"}},
            lines=[],
            record_id=None,
            record={},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert result.ok is True, result.missing_required


def test_a_subsidiary_reference_object_counts_as_present():
    """NetSuite sends a reference field as {"id": "5"}, not a scalar. _is_empty
    must not treat a dict as missing."""
    meta = apply_curated_requirements(_live_customer_metadata(), record_type="customer", fields={})
    result = validate_write(
        payload=NormalizedPayload(
            fields={"companyName": "Acme", "subsidiary": {"id": "5"}},
            lines=[],
            record_id=None,
            record={},
            payload_key="data",
        ),
        metadata=meta,
        record_type="customer",
        mutation_type="create",
    )
    assert "subsidiary" not in result.missing_required
