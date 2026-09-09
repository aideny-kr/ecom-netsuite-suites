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
                    "proposals",
                    "settlement",
                    "case_id",
                    "last_observed_at",
                    "history",
                    "resolution_history",
                    "resolution_examples",
                    "resolution_usage",
                    "resolution_history_url",
                    "truncated",
                )
            },
            "note": "The evidence table displays exact amounts. Do not restate or recompute them. "
            "Use the findings to explain missing evidence and supported next steps. "
            "Pending proposals need exact-change human approval on the review page; "
            "approved does not mean executed or verified. Follow continuation_run_id when present. "
            "A succeeded settlement check verifies order total, tax and refunds, not payment/payout clearance. "
            "Historical approvals never authorize a new change.",
        }
    )
