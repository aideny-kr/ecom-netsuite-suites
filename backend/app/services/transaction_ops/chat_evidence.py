"""Shared agent context for streamed and non-streamed investigation results."""

import json


def condense_status(result):
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
            "Historical approvals never authorize a new change.",
        }
    )
