"""The one registry of accounting correction treatments.

Every place that used to keep its own set of correction kinds, grouping, rules
fingerprints, lock keys, recheck targets, verification dispatch and recovery
queries, reads this table instead. A proposal's transport is a field on the
proposal (``execution_transport``), never inferred from its kind: the 54-case
group crash in PR #262 was an MCP proposal meeting a branch that assumed native.

Adding a treatment means adding one row here and the adapter that implements it.
``tests/test_treatment_registry.py`` fails if any other module grows a second
copy of these kinds.
"""

from dataclasses import dataclass


class TreatmentError(ValueError, KeyError):
    """A proposal the registry cannot place: an unregistered kind, or a proposal missing
    the field its treatment needs (its lock document, its profile).

    It is a ValueError because every caller of this module treats a ValueError as the
    refusal to show a person, and a KeyError because the old inline lookups raised one
    and their guards still catch it. Being both is what keeps a registry lookup from
    turning into an unhandled crash at whichever call site forgot the guard.
    """

    def __str__(self):
        return self.args[0] if self.args else ""


def _required_field(proposal, key):
    try:
        return proposal[key]
    except (KeyError, TypeError):
        raise TreatmentError(f"the correction is missing its {key}") from None


@dataclass(frozen=True)
class Treatment:
    kind: str
    label: str  # the plan's step label
    batch_label: str  # the group card's treatment batch label
    record_type: str  # the document the correction writes
    family: str  # invoice_tax | commercial | amendment
    lock: str  # record | invoice | invoice_record: the document two corrections must not share
    reconciliation_target: str  # created_from | record | sales_order
    verification: str  # invoice | discount | credit | order | amendment
    dependent_step: bool = False  # the plan's sales-order step awaits approval for this kind
    prefetch_metadata: bool = False  # scoped invoice metadata is prefetched before the card


_ROWS = (
    Treatment(
        kind="invoice_tax",
        label="Correct invoice tax",
        batch_label="Invoice tax correction",
        record_type="invoice",
        family="invoice_tax",
        lock="record",
        reconciliation_target="created_from",
        verification="invoice",
    ),
    Treatment(
        kind="invoice_sales_adjustment",
        label="Apply invoice sales adjustment",
        batch_label="Sales Adjustment on unpaid invoice",
        record_type="invoice",
        family="commercial",
        lock="record",
        reconciliation_target="created_from",
        verification="discount",
        prefetch_metadata=True,
    ),
    Treatment(
        kind="sales_adjustment_credit",
        label="Create and apply Sales Adjustments credit",
        batch_label="Sales Adjustments credit and invoice application",
        record_type="creditmemo",
        family="commercial",
        lock="invoice_record",  # the credit is created against the invoice named by record_id
        reconciliation_target="created_from",
        verification="credit",
    ),
    Treatment(
        kind="sales_order_source_alignment",
        label="Align sales order with source",
        batch_label="Sales order source alignment",
        record_type="salesorder",
        family="commercial",
        lock="invoice",
        reconciliation_target="record",
        verification="order",
        dependent_step=True,
        prefetch_metadata=True,
    ),
    Treatment(
        kind="credit_tax_reallocation",
        label="Correct existing credit tax allocation",
        batch_label="Existing credit tax allocation",
        record_type="creditmemo",
        family="amendment",
        lock="invoice",
        reconciliation_target="sales_order",
        verification="amendment",
    ),
    Treatment(
        kind="sales_order_line_alignment",
        label="Align sales-order lines and tax with source",
        batch_label="Sales order source alignment",
        record_type="salesorder",
        family="amendment",
        lock="invoice",
        reconciliation_target="record",
        verification="amendment",
        dependent_step=True,
    ),
)

REGISTRY = {row.kind: row for row in _ROWS}
KINDS = {row.kind: row.label for row in _ROWS}
DEFAULT_KIND = "invoice_tax"
MCP_TRANSPORT = "mcp_record_api"

# Kinds with a native verification contract. The legacy invoice-tax proposal is
# supported only in its exact taxRate-only shape (see ``supports``).
VERIFIED_KINDS = tuple(row.kind for row in _ROWS if row.family != "invoice_tax")
AMENDMENT_RECORD_TYPES = {row.kind: row.record_type for row in _ROWS if row.family == "amendment"}
DEPENDENT_KINDS = frozenset(row.kind for row in _ROWS if row.dependent_step)


def treatment_of(proposal) -> Treatment:
    """The registry row for a proposal; a missing kind is the legacy invoice-tax treatment.

    An unregistered kind is refused (TreatmentError), never routed to a default: the
    54-case crash was a proposal meeting a branch written for a different kind.
    """
    kind = (proposal or {}).get("kind") or DEFAULT_KIND
    try:
        return REGISTRY[kind]
    except KeyError:
        raise TreatmentError(f"unsupported correction kind: {kind}") from None


def treatment_or_none(proposal):
    """The registry row, or None for a proposal whose kind is not a registered treatment.

    For readers that must tolerate a foreign kind (history, links, display); anything
    that decides a write uses treatment_of and refuses.
    """
    try:
        return treatment_of(proposal)
    except TreatmentError:
        return None


def family_of(proposal):
    """The treatment family, or None for a proposal whose kind is not a registered treatment."""
    row = treatment_or_none(proposal)
    return row.family if row else None


def is_mcp(proposal) -> bool:
    return (proposal or {}).get("execution_transport") == MCP_TRANSPORT


def supports(proposal) -> bool:
    """Whether a proposal has a verification contract this platform can recheck."""
    if not proposal:
        return False
    kind = proposal.get("kind")
    if kind in VERIFIED_KINDS:
        return True
    return (
        kind in {None, DEFAULT_KIND}
        and proposal.get("record_type") == "invoice"
        and set(proposal.get("proposed_fields") or {}) == {"taxRate"}
    )


def treatment_profile(proposal) -> dict:
    """The accounting profile that makes two proposals the same treatment.

    Transport first: an MCP amendment is bound to the connector schema it was
    typed against, a native amendment to its native profile. Only then by family.
    """
    treatment = treatment_of(proposal)
    if treatment.family == "amendment":
        if is_mcp(proposal):
            return {"connector_schema": _required_field(proposal, "connector_schema")}
        return _required_field(proposal, "native_profile")
    if treatment.family == "commercial":
        return _required_field(proposal, "profile")
    return {"tax_item_id": _required_field(proposal, "tax_item").get("id")}


def collision_key(proposal) -> tuple[str, str]:
    """The document that two approved corrections must never touch concurrently."""
    treatment = treatment_of(proposal)
    if treatment.lock == "invoice":
        # Fail closed: a lock keyed on the wrong document is worse than no lock.
        return "invoice", str(_required_field(proposal, "invoice_id"))
    if treatment.lock == "invoice_record":
        return "invoice", str(_required_field(proposal, "record_id"))
    return _required_field(proposal, "record_type"), str(_required_field(proposal, "record_id"))


def reconciliation_target_id(proposal):
    """The sales order a recheck report must describe, or None when it cannot be established.

    A declared ``reconciliation_target`` is used, but never trusted over evidence: the
    existing-credit rule still requires the independently collected invoice -> sales-order
    edge, and when that edge or the proposal's own derivation names a different order the
    answer is None and the recheck refuses rather than guesses.
    Without a declaration the family rule applies: sales-order corrections are their own
    target, invoice corrections were created from the order, and an existing-credit
    correction binds through the collected edge.
    """
    try:
        declared = (proposal.get("reconciliation_target") or {}).get("record_id")
        declared = None if declared in (None, "") else str(declared)  # ids compare as strings; 0 is a value
        rule = treatment_of(proposal).reconciliation_target
        if rule == "record":
            derived = str(proposal["record_id"])
        elif rule == "sales_order":
            # An existing-credit correction binds through the independently collected
            # invoice -> sales-order edge; neither the proposal's own derivation nor a
            # declaration can stand in for it. The edge becomes the derived answer and
            # then meets the same declared-vs-derived check as every other rule.
            order = proposal.get("sales_order_id")
            candidate = declared if order in (None, "") else str(order)
            edge = (((proposal.get("support") or {}).get("invoice") or {}).get("createdFrom") or {}).get("id")
            if not candidate or edge is None or str(edge) != candidate:
                return None
            derived = candidate
        else:
            derived = str(proposal["before"]["createdFrom"]["id"])
        if declared and declared != derived:
            return None
        return declared or derived
    except (KeyError, TypeError, AttributeError):
        return None
