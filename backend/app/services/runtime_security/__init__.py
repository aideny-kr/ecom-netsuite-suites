"""Dedicated-company database provisioning; operator-only, never an HTTP API."""

import uuid


def bigquery_schema_prefix(company: uuid.UUID) -> str:
    """Immutable company namespace in the legacy shared knowledge table."""
    return f"bi/schema-docs/{uuid.UUID(str(company))}/"
