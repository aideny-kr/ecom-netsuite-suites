"""Pre-execution validation for SuiteQL queries.

Catches known pitfalls before they hit NetSuite.
Fail-open: if uncertain, let the query through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    suggested_fix: str | None = None


def validate_suiteql_query(query: str, context: dict | None = None) -> ValidationResult:
    """Validate a SuiteQL query for known syntax issues.

    Returns ValidationResult. Fail-open: returns is_valid=True if uncertain.
    """
    errors: list[str] = []
    suggestions: list[str] = []
    upper_query = query.upper().strip()

    # 1. LIMIT syntax (SuiteQL uses FETCH FIRST N ROWS ONLY)
    limit_match = re.search(r"\bLIMIT\s+(\d+)", query, re.IGNORECASE)
    if limit_match:
        n = limit_match.group(1)
        errors.append("SuiteQL does not support LIMIT. Use FETCH FIRST N ROWS ONLY.")
        suggestions.append(f"Replace 'LIMIT {n}' with 'FETCH FIRST {n} ROWS ONLY'")

    # 2. Compound status codes (e.g., 'SalesOrd:B' — should be just 'B')
    compound_match = re.findall(r"'([A-Za-z]+:[A-Z])'", query)
    if compound_match:
        codes = ", ".join(compound_match)
        errors.append(
            f"Compound status codes detected: {codes}. "
            "SuiteQL uses single-letter status codes (e.g., 'B' not 'SalesOrd:B')."
        )
        stripped = [c.split(":")[1] for c in compound_match]
        suggestions.append(f"Use single-letter codes: {', '.join(repr(s) for s in stripped)}")

    # 3. ROWNUM + ORDER BY anti-pattern
    if "ROWNUM" in upper_query and "ORDER BY" in upper_query:
        errors.append(
            "ROWNUM filters rows before ORDER BY is applied. "
            "Use FETCH FIRST N ROWS ONLY instead."
        )

    # 4. OFFSET syntax (not supported)
    if re.search(r"\bOFFSET\s+\d+", query, re.IGNORECASE):
        errors.append("SuiteQL does not support OFFSET. Use keyset pagination instead.")

    # 5. ILIKE (not supported — use LOWER() + LIKE)
    if re.search(r"\bILIKE\b", query, re.IGNORECASE):
        errors.append("SuiteQL does not support ILIKE. Use LOWER(field) LIKE LOWER('%value%').")

    # 6. Boolean literals (SuiteQL uses 'T'/'F', not true/false)
    if re.search(r"\b(true|false)\b", query, re.IGNORECASE) and not re.search(
        r"'(true|false)'", query, re.IGNORECASE
    ):
        errors.append("SuiteQL uses 'T'/'F' for boolean values, not true/false.")

    # 7. Double quotes for string literals
    if re.search(r'=\s*"[^"]+"|IN\s*\([^)]*"[^"]*"', query):
        errors.append("Use single quotes for string literals in SuiteQL, not double quotes.")

    # 8. Missing FROM clause
    if upper_query.startswith("SELECT") and "FROM" not in upper_query:
        errors.append("Query appears to be missing a FROM clause.")

    suggested_fix = "; ".join(suggestions) if suggestions else None
    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        suggested_fix=suggested_fix,
    )
