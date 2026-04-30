"""Clarify tool schema definition (Component 2)."""

CLARIFY_TOOL_SCHEMA: dict = {
    "name": "clarify",
    "description": (
        "Ask the user to choose between plausible interpretations of an "
        "ambiguous question before running data tools. Use when the user's "
        "question has multiple legitimate readings (e.g., 'revenue' could mean "
        "GAAP recognized revenue from NetSuite GL, or ecommerce checkout "
        "totals from BigQuery, or gross sales from Shopify). NEVER call any "
        "data tool in the same turn as clarify; the user's choice arrives on "
        "the next turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ["A", "B", "C"]},
                        "title": {"type": "string", "maxLength": 80},
                        "rationale": {"type": "string", "maxLength": 200},
                        "source": {
                            "type": "string",
                            "enum": ["netsuite", "bigquery", "shopify", "stripe", "drive"],
                        },
                        "is_default": {"type": "boolean"},
                    },
                    "required": ["id", "title", "rationale", "source", "is_default"],
                },
            },
            "ambiguity_summary": {"type": "string", "maxLength": 500},
        },
        "required": ["options", "ambiguity_summary"],
    },
}


def get_clarify_tool(plan_mode_enabled: bool) -> dict | None:
    """Return the clarify tool schema if Plan Mode is enabled, else None.

    Used by tool-inventory builder to gate tool registration. Returns the
    SAME dict object on every call so caller can compare by identity.
    """
    return CLARIFY_TOOL_SCHEMA if plan_mode_enabled else None
