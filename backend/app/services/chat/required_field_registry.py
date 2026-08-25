"""Human-curated required fields per NetSuite record type.

WHY THIS EXISTS — and why it is a deliberate reversal of a stated plan rule.
``docs/superpowers/plans/2026-08-19-agentic-netsuite-write-loop.md`` says
"Never hardcode NetSuite field names in Python. Every required-field fact
comes from ns_getRecordTypeMetadata at runtime." That constraint is
UNSATISFIABLE, established three independent ways on 2026-08-25:

* ``ns_getRecordTypeMetadata`` returns a JSON-Schema-shaped catalog whose per
  field keys are only ``{description, format, nullable, properties, title,
  type, x-ns-custom-field}``. There is no ``required`` array, and ``nullable``
  is ``false`` on zero of the 177 customer fields. The shape structurally
  cannot carry requiredness.
* ``ns_getSuiteQLMetadata`` returns that identical shape.
* The only queryable mandatoriness source, SuiteQL ``CustomField.ismandatory``
  (see ``prompt_template_service.py``), covers CUSTOM fields only — it never
  describes ``subsidiary``, ``companyname`` or ``entityid``.

So requirements must be curated. That is a decision to record, not a detail to
slip in: it moves a class of fact from runtime discovery to code review.

THE RISK RUNS OPPOSITE TO THE USUAL ONE. A missing entry costs little — the
write reaches NetSuite, NetSuite rejects it, and the bounded repair loop in
``agents/base_agent.py`` recovers. A WRONG entry blocks a write NetSuite would
have accepted, and to an operator that is indistinguishable from the product
being broken. Every entry therefore has to clear two bars: NetSuite really
rejects the create without it, AND it does not auto-derive from another field.

Entries deliberately EXCLUDED, each for a reason worth keeping written down:

* ``subsidiary`` on any transaction carrying an ``entity`` (invoice,
  salesOrder, vendorBill, creditMemo). NetSuite derives it from the entity's
  own subsidiary; it is required=false there. This is the single most tempting
  wrong entry, because it is required on the entity records right above.
* ``trandate`` anywhere. NetSuite defaults it to today when omitted.
* ``location`` / ``department`` / ``class``. Requiredness is per-account
  configuration (a preference, and multi-location inventory), not a property
  of the record type — unknowable from here.
* Everything on ``expenseReport``. Never observed on this account and absent
  from the record scrape; a placeholder would be a guess wearing a fact's
  clothes.
* Line-level and cross-line rules (a JE needs >=2 balancing lines; an invoice
  needs >=1 item line). Those are structural invariants, not field presence —
  JE balance already lives in ``posting_invariants.py`` and must not be
  re-implemented here.

Adding a record type is one reviewed block below. Nothing at runtime — and no
model — can add one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.services.chat.record_metadata_service import (
    FieldSpec,
    RecordMetadata,
    coerce_netsuite_bool,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Provenance",
    "RequiredFieldRule",
    "REQUIREMENTS_SOURCE",
    "apply_curated_requirements",
    "requirements_for",
]

# Stamped onto RecordMetadata.requirements_source when this registry supplied
# the requirements, so a log line (or a future card caption) can say WHICH
# check ran instead of implying NetSuite asserted it.
REQUIREMENTS_SOURCE = "curated_registry"


class Provenance(str, Enum):
    """How we know. The distinction is not bookkeeping — it is how a reviewer
    judges whether an entry is safe to trust when it starts blocking writes."""

    #: This account's own NetSuite rejected a create naming this field.
    ACCOUNT_EVIDENCED = "account_evidenced"
    #: Established NetSuite behaviour, not verified against this account.
    DOMAIN = "domain"


@dataclass(frozen=True)
class RequiredFieldRule:
    name: str
    label: str
    provenance: Provenance
    evidence: str
    #: Optional predicate over the write payload's header fields. ``None``
    #: means unconditional. A rule whose predicate returns False is not
    #: required for THIS payload — it is not "missing", it does not apply.
    condition: Callable[[Mapping[str, Any]], bool] | None = None
    condition_note: str = ""

    def applies_to(self, fields: Mapping[str, Any]) -> bool:
        if self.condition is None:
            return True
        return self.condition(fields)


def _is_individual(fields: Mapping[str, Any]) -> bool:
    """True when the entity is a person rather than a company.

    ``isperson`` arrives as a real bool or as NetSuite's ``"T"``/``"F"``
    strings, so this must go through ``coerce_netsuite_bool`` — ``bool("F")``
    is ``True`` in Python and would invert the rule. Absent means company,
    matching NetSuite's own default.
    """
    return coerce_netsuite_bool(fields.get("isperson"))


def _is_company(fields: Mapping[str, Any]) -> bool:
    return not _is_individual(fields)


_SUBSIDIARY_ON_ENTITY = (
    "A OneWorld entity record has no parent to inherit a subsidiary from, so "
    "NetSuite cannot derive it. Same class of fact as customer.subsidiary, "
    "which this account has rejected writes over twice."
)

_ENTITY_ON_TRANSACTION = (
    "A transaction cannot exist without the party it is with; NetSuite has no "
    "way to derive it. Note the deliberate absence of `subsidiary` here — it "
    "DOES derive, from this entity."
)

_REGISTRY: dict[str, tuple[RequiredFieldRule, ...]] = {
    "customer": (
        RequiredFieldRule(
            name="subsidiary",
            label="Primary Subsidiary",
            provenance=Provenance.ACCOUNT_EVIDENCED,
            evidence=(
                "Two recorded rejections from this account's NetSuite, both "
                "'Please enter value(s) for: Primary Subsidiary', plus a "
                "same-session fail->succeed pair on 2026-08-25 that "
                "established the API name is `subsidiary` (record 5789707)."
            ),
        ),
        RequiredFieldRule(
            name="companyname",
            label="Company Name",
            provenance=Provenance.DOMAIN,
            evidence="A company customer has no name without it; entityid derives FROM it.",
            condition=_is_company,
            condition_note="isperson is not true",
        ),
        RequiredFieldRule(
            name="lastname",
            label="Last Name",
            provenance=Provenance.DOMAIN,
            evidence="An individual customer's name anchor; NetSuite rejects a person with no last name.",
            condition=_is_individual,
            condition_note="isperson is true",
        ),
    ),
    "vendor": (
        RequiredFieldRule(
            name="subsidiary",
            label="Primary Subsidiary",
            provenance=Provenance.DOMAIN,
            evidence=_SUBSIDIARY_ON_ENTITY,
        ),
        RequiredFieldRule(
            name="companyname",
            label="Company Name",
            provenance=Provenance.DOMAIN,
            evidence="A company vendor has no name without it; entityid derives FROM it.",
            condition=_is_company,
            condition_note="isperson is not true",
        ),
        RequiredFieldRule(
            name="lastname",
            label="Last Name",
            provenance=Provenance.DOMAIN,
            evidence="An individual vendor's name anchor.",
            condition=_is_individual,
            condition_note="isperson is true",
        ),
    ),
    "journalentry": (
        RequiredFieldRule(
            name="subsidiary",
            label="Subsidiary",
            provenance=Provenance.DOMAIN,
            evidence=(
                "A journal entry carries no entity, so there is nothing to "
                "derive a subsidiary from — an unconditional OneWorld gate. "
                "Line accounts and the debit=credit balance are cross-line "
                "invariants and live in posting_invariants.py, not here."
            ),
        ),
    ),
    "invoice": (
        RequiredFieldRule(
            name="entity",
            label="Customer",
            provenance=Provenance.DOMAIN,
            evidence=_ENTITY_ON_TRANSACTION,
        ),
    ),
    "salesorder": (
        RequiredFieldRule(
            name="entity",
            label="Customer",
            provenance=Provenance.DOMAIN,
            evidence=_ENTITY_ON_TRANSACTION,
        ),
    ),
    "creditmemo": (
        RequiredFieldRule(
            name="entity",
            label="Customer",
            provenance=Provenance.DOMAIN,
            evidence=_ENTITY_ON_TRANSACTION,
        ),
    ),
    "vendorbill": (
        RequiredFieldRule(
            name="entity",
            label="Vendor",
            provenance=Provenance.DOMAIN,
            evidence=_ENTITY_ON_TRANSACTION,
        ),
    ),
}


def requirements_for(record_type: str) -> tuple[RequiredFieldRule, ...] | None:
    """Rules for *record_type*, or ``None`` if it is not curated.

    ``None`` means "we do not know this record type's requirements" — it is
    NOT "this record type requires nothing". Callers must keep such a write
    ``unvalidated`` rather than presenting it as checked.
    """
    if not isinstance(record_type, str):
        return None
    return _REGISTRY.get(record_type.strip().lower())


def apply_curated_requirements(
    metadata: RecordMetadata | None,
    *,
    record_type: str,
    fields: Mapping[str, Any],
) -> RecordMetadata | None:
    """Overlay curated requirements onto live *metadata*, returning a NEW
    ``RecordMetadata``.

    Never mutates *metadata*: ``get_record_metadata`` caches the object it
    returns, so an in-place edit would let one payload's conditional rules
    leak into every later call — a customer create with ``isperson=True``
    would leave the cached entry demanding ``lastname`` from every subsequent
    company create.

    Returns *metadata* unchanged (including ``None``) when the record type is
    not curated. A ``None`` metadata stays ``None`` deliberately: this
    function adds requirements to metadata we HAVE, and does not manufacture
    metadata we could not read — a failed fetch must keep its existing
    fail-open behaviour of showing the human an ``unvalidated`` card.
    """
    if metadata is None:
        return None

    rules = requirements_for(record_type)
    if not rules:
        return metadata

    applicable = {rule.name: rule for rule in rules if rule.applies_to(fields)}
    if not applicable:
        # A curated record type whose every rule is conditioned out is still a
        # completed check — say so, rather than falling back to "unknown".
        return metadata.model_copy(
            update={"requirements_known": True, "requirements_source": REQUIREMENTS_SOURCE},
            deep=True,
        )

    merged_fields: list[FieldSpec] = []
    seen: set[str] = set()
    for spec in metadata.fields:
        seen.add(spec.name)
        if spec.name in applicable:
            # Keep NetSuite's own label and type — that is what the operator
            # sees on the NetSuite form. Only requiredness comes from us.
            merged_fields.append(spec.model_copy(update={"required": True}, deep=True))
        else:
            merged_fields.append(spec.model_copy(deep=True))

    for name, rule in applicable.items():
        if name in seen:
            continue
        # The catalog omitted a field we know is required. Dropping it would
        # turn a known requirement back into an unasked question, so
        # synthesise a spec from the rule instead.
        merged_fields.append(FieldSpec(name=name, label=rule.label, required=True))

    logger.info(
        "required_field_registry: applied curated requirements record_type=%s required=%s",
        record_type,
        sorted(applicable),
    )

    return metadata.model_copy(
        update={
            "fields": merged_fields,
            "requirements_known": True,
            "requirements_source": REQUIREMENTS_SOURCE,
        },
        deep=True,
    )
