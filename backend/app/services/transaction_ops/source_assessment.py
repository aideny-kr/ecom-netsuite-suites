"""Recheck finalized source assessments at comparison and correction boundaries.

This proves recorded source amounts and identities, never a statutory rate.
"""

import re


def assessments_proven(source):
    seen = set()
    for tax in source.tax_details:
        if tax.calculation != "source_assessment":
            continue
        proof = tax.assessment
        if (
            source.system != "framework"
            or source.account_id != "frame.work"
            or proof is None
            or proof.authority != "framework_finalized_adjustment"
            or proof.finalized is not True
            or proof.adjustment_id in seen
            or source.updated_at is None
            or tax.basis is None
            or tax.basis < 0
            or tax.amount is None
            or tax.amount < 0
            or any(
                value is not None
                for value in (tax.rate, tax.rounding, tax.included_gross_basis, tax.included_rate_total)
            )
            or not re.fullmatch(r"(?:line|shipment):[1-9][0-9]{0,29}:tax:[1-9][0-9]{0,29}", tax.allocation_key or "")
            or tax.key != f"{tax.allocation_key}:source_rate:{proof.source_tax_id}:adjustment:{proof.adjustment_id}"
        ):
            return False
        try:
            if not proof.updated_at <= source.updated_at <= source.observed_at:
                return False
        except TypeError:
            return False
        seen.add(proof.adjustment_id)
    return True
