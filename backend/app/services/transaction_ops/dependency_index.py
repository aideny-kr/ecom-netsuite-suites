"""Map changed NetSuite identities to affected orders, without provider reads.

This is an additive inventory of observed dependencies, not evidence that a
change feed is complete or that a cached comparison is current. Old dependencies
may over-nominate an order; dropping one can miss a deletion or removed link.
"""

from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.core.database import set_tenant_context
from app.models.transaction_netsuite_dependency import TransactionNetSuiteDependency as Dependency
from app.models.transaction_ops import TransactionRun
from app.services.transaction_ops.netsuite_reader import _account, _id

_RECORD_TYPES = {"transaction", "customrecord_fw_refund_requests"}
MAX_DEPENDENCIES = 600  # At most two endpoints per graph edge, plus custom requests.
_INVENTORY_KIND = "observed_refund_dependencies_v1"


def _key(kind, identifier):
    if not isinstance(kind, str) or kind not in _RECORD_TYPES or not _id(identifier):
        raise ValueError("invalid_dependency_identity")
    return kind, str(identifier)


def compact_dependency_evidence(report):
    """Keep bounded identities when detailed financial evidence is omitted."""
    try:
        target = (report.get("refund_evidence") or {}).get("target") or {}
        if not target:
            inventory = report.get("refund_dependency_evidence") or {}
            target = {**inventory, "complete": inventory.get("kind") == _INVENTORY_KIND}
        manifest = target["dependency_manifest"]
        transactions, requests = manifest["transaction_ids"], manifest["refund_requests"]
        if (
            target.get("complete") is not True
            or target.get("provider") != "netsuite"
            or type(manifest.get("version")) is not int
            or manifest["version"] != 1
            or type(manifest.get("truncated")) is not bool
            or not isinstance(transactions, list)
            or not isinstance(requests, list)
            or len(transactions) + len(requests) > MAX_DEPENDENCIES
            or not _id(manifest.get("order_id"))
            or not _id(target.get("subsidiary_id"))
        ):
            return None
        scope = {
            key: target[key] for key in ("provider", "account_id", "connection_id", "subsidiary_id", "order_reference")
        }
        if any(not isinstance(value, str) or len(value) > 255 for value in scope.values()):
            return None
        _account(scope["account_id"])
        UUID(scope["connection_id"])
        return {
            **scope,
            "kind": _INVENTORY_KIND,
            "dependency_manifest": {
                "version": 1,
                "order_id": str(manifest["order_id"]),
                "transaction_ids": [_key("transaction", value)[1] for value in transactions],
                "refund_requests": [_key("customrecord_fw_refund_requests", value)[1] for value in requests],
                "truncated": manifest["truncated"],
            },
        }
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def observed_dependencies(report, snapshot, reference):
    """Only scoped identities; never infer an absent document from an empty list."""
    try:
        account = _account(snapshot["netsuite_account_id"])
        connection = str(UUID(snapshot["netsuite_connection_id"]))
        targets = report.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 2:
            return set()
        roots = {
            _key("transaction", target["record_id"])
            for target in targets
            if target.get("system") == "netsuite"
            and target.get("record_type") == "salesorder"
            and _account(target.get("account_id")) == account
            and target.get("subsidiary_id") == snapshot.get("subsidiary_id")
            and target.get("order_reference") == reference
        }
        refund = (report.get("refund_evidence") or {}).get("target") or {}
        if not refund:
            inventory = report.get("refund_dependency_evidence") or {}
            refund = {**inventory, "complete": inventory.get("kind") == _INVENTORY_KIND}
        if (
            refund.get("complete") is not True
            or refund.get("provider") != "netsuite"
            or refund.get("connection_id") != connection
            or refund.get("account_id") != account
            or refund.get("subsidiary_id") != snapshot.get("subsidiary_id")
            or refund.get("order_reference") != reference
        ):
            return roots
        manifest = refund.get("dependency_manifest")
        if manifest is None and len(roots) == 1:
            # Pre-manifest reports retained positive refund/custom-link IDs.
            # They can nominate old owners after deletion, without pretending
            # the old report recorded every node of the native graph.
            records, links = refund.get("record_ids", []), refund.get("request_links", [])
            if (
                not isinstance(records, list)
                or not isinstance(links, list)
                or len(roots) + len(records) + 4 * len(links) > MAX_DEPENDENCIES
            ):
                return roots
            try:
                legacy = {_key("transaction", identifier) for identifier in records}
                for link in links:
                    legacy.add(_key("customrecord_fw_refund_requests", link["request_id"]))
                    legacy.update(
                        _key("transaction", link[field])
                        for field in ("credit_memo_id", "refund_id", "deposit_id")
                        if link.get(field) is not None
                    )
                return roots | legacy
            except (KeyError, TypeError, ValueError, AttributeError):
                return roots
        if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
            return roots  # Legacy evidence has no full graph inventory.
        transactions, requests = manifest.get("transaction_ids"), manifest.get("refund_requests")
        if (
            ("transaction", manifest.get("order_id")) not in roots
            or not isinstance(transactions, list)
            or not isinstance(requests, list)
            or len(transactions) + len(requests) > MAX_DEPENDENCIES
        ):
            return roots
        dependencies = {_key("transaction", identifier) for identifier in transactions}
        dependencies.update(_key("customrecord_fw_refund_requests", identifier) for identifier in requests)
        return roots | dependencies
    except (KeyError, ValueError, TypeError, AttributeError):
        # A legacy diagnostic finding may not have typed financial snapshots.
        # It cannot establish dependency scope and must not poison other orders.
        return set()


async def record_dependencies(db, tenant_id, run, finding):
    if run.tenant_id != tenant_id or finding.tenant_id != tenant_id or finding.run_id != run.id:
        raise ValueError("dependency_scope_mismatch")
    keys = observed_dependencies(finding.report_json, run.config_snapshot, finding.order_reference)
    if not keys:
        return
    await set_tenant_context(db, tenant_id)
    values = [
        {
            "tenant_id": tenant_id,
            "run_id": run.id,
            "order_reference": finding.order_reference,
            "connection_id": UUID(run.config_snapshot["netsuite_connection_id"]),
            "account_id": _account(run.config_snapshot["netsuite_account_id"]),
            "record_type": kind,
            "record_id": identifier,
        }
        for kind, identifier in sorted(keys)
    ]
    await db.execute(insert(Dependency).values(values).on_conflict_do_nothing(constraint="uq_tx_ns_dependency"))
    # The caller commits finding + inventory together under its existing lease.


async def affected_order_references(db, tenant_id, config_id, record_keys, *, after_reference="", page_size=100):
    """Keys use SuiteQL table names, not transaction subtype/display strings.

    Bounded keyset pagination. An empty page is not a feed-completeness claim.
    """
    from app.services.transaction_ops import state_service as state

    config = await state.get_config(db, tenant_id, config_id)
    if (
        not isinstance(record_keys, (list, tuple))
        or not 1 <= len(record_keys) <= 500
        or not isinstance(after_reference, str)
        or len(after_reference) > 100
        or type(page_size) is not int
        or not 1 <= page_size <= 500
    ):
        raise ValueError("invalid_dependency_page")
    if any(not isinstance(key, (tuple, list)) or len(key) != 2 for key in record_keys):
        raise ValueError("invalid_dependency_identity")
    keys = sorted({_key(*key) for key in record_keys})
    query = (
        select(Dependency.order_reference)
        .join(
            TransactionRun,
            (TransactionRun.id == Dependency.run_id) & (TransactionRun.tenant_id == tenant_id),
        )
        .where(
            Dependency.tenant_id == tenant_id,
            Dependency.connection_id == config.netsuite_connection_id,
            Dependency.account_id == _account(config.netsuite_account_id),
            TransactionRun.config_id == config.id,
            tuple_(Dependency.record_type, Dependency.record_id).in_(keys),
            Dependency.order_reference > after_reference,
        )
        .distinct()
        .order_by(Dependency.order_reference)
        .limit(page_size + 1)
    )
    references = list(await db.scalars(query))
    more = len(references) > page_size
    return {
        "order_references": references[:page_size],
        "has_more": more,
        "next_after_reference": references[page_size - 1] if more else None,
    }
