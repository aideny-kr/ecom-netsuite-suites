"""Shared agent context for streamed and non-streamed investigation results."""

import json


def condense_status(result):
    if result.get("source") == "stored_reconciliation_state":
        return json.dumps(
            {
                **{key: result.get(key) for key in ("success", "source", "observed_at", "truncated", "next_offset")},
                "entities": [
                    {
                        "config_id": entity["config_id"],
                        "name": entity["name"],
                        "coverage": entity["coverage"],
                        "next_action": entity["next_action"],
                        "continuation": entity["continuation"],
                        "active_runs_truncated": entity["active_runs_truncated"],
                        "active_runs": [
                            {
                                key: row.get(key)
                                for key in (
                                    "run_id",
                                    "origin",
                                    "execution_state",
                                    "phase",
                                    "collection_wait",
                                    "last_read_failure",
                                )
                            }
                            for row in entity["active_runs"]
                        ],
                    }
                    for entity in result.get("entities", [])
                ],
                "note": "The operational table displays exact dates and checkpoint counts. Explain its status without "
                "recomputing or restating figures. Scan coverage is not financial certification. "
                "A current cutoff does not prove every historical date was scanned. Financial counts belong only to "
                "the latest scheduled run checkpoint, not the entire requested period. Next-action times describe "
                "eligibility, not a guaranteed dispatch. Saved collection waits require scheduler revalidation. "
                "Worker/container health and last durable progress time are not measured by this tool. "
                "Use next_offset when truncated. This is a read-only snapshot; do not start or retry work merely "
                "to answer status, and do not infer that historical read failures are current blockers.",
            }
        )
    return json.dumps(
        {
            **{
                key: result.get(key)
                for key in (
                    "success",
                    "run_id",
                    "status",
                    "termination_reason",
                    "review_url",
                    "continuation_run_id",
                    "continuation_blocked",
                    "findings",
                    "investigation_guidance",
                    "accounting_review",
                    "proposals",
                    "settlement",
                    "case_id",
                    "last_observed_at",
                    "history",
                    "resolution_history",
                    "resolution_examples",
                    "accounting_resolution_history",
                    "accounting_resolution_examples",
                    "accounting_resolution_usage",
                    "resolution_usage",
                    "resolution_history_url",
                    "truncated",
                )
            },
            "note": "The evidence table displays exact amounts. Do not restate or recompute them. "
            "Use deterministic reconciliation.metrics to distinguish confirmed differences, matched metrics and "
            "unverified metrics. Generic repair_findings describe correction readiness, not missing refund amounts "
            "or unproven header differences. Do not relabel matched refunds as incomplete. "
            "Observation timestamps describe when evidence was read; a refresh requirement before execution "
            "does not mean the observation is stale. Separate observed facts, root-cause hypotheses and missing "
            "accounting evidence. Follow accounting_review to validate actual connected scope/configuration and "
            "continue authorized read-only investigation; do not stop after a status summary to ask permission. "
            "Never promise a journal, invoice edit or posted adjustment when no supported proposal exists. "
            "Pending proposals need exact-change human approval on the review page; "
            "approved does not mean executed or verified. Follow continuation_run_id when present. "
            "A succeeded settlement check verifies order total, tax and refunds, not payment/payout clearance. "
            "Historical approvals never authorize a new change. "
            "Use resolution_plan and completion in accounting history to continue the whole order, including "
            "its sales order and posting records. A prepared dependent correction needs its own exact approval. "
            "Do not repost an operation with recorded execution or ask the user to restart a completed investigation.",
        }
    )
