"""Shared, tenant-configurable order-reference extraction.

R3 Part 1 (de-Framework the order key). Consolidates the two duplicate
``R\\d{9}`` extractors (``order_matching_engine`` + ``ingestion.netsuite_deposit_sync``)
into ONE module backed by a tenant-configurable pattern.

Invariants:
  * ``DEFAULT_ORDER_REF_PATTERN`` is byte-identical to the prior hardcoded pattern
    (``R`` followed by exactly 9 digits, one capture group). A tenant whose
    ``order_ref_pattern`` is NULL/absent therefore extracts identically to before.
  * Compilation is memoized (``lru_cache``) because extraction runs over 400K+
    payout lines.
  * A malformed/invalid pattern NEVER raises — it falls back to the default and
    logs a warning, so a bad tenant config can't take down the matching pipeline.
"""

from __future__ import annotations

import re
from functools import lru_cache

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import TenantConfig

logger = structlog.get_logger()

# Prior hardcoded pattern: "R" then exactly 9 digits, one capture group. NULL
# TenantConfig.order_ref_pattern => this default => byte-identical to pre-R3.
DEFAULT_ORDER_REF_PATTERN = r"(R\d{9})"


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    """Compile (and memoize) ``pattern``.

    On ANY bad pattern — invalid regex syntax (``re.error``), a non-string value
    (``TypeError``, e.g. an int leaked from a misconfigured TenantConfig), or a
    pathological but syntactically valid pattern that blows up the compiler
    (``OverflowError``/``RecursionError`` from e.g. ``a{99999999999}``) — log a
    warning and fall back to the default pattern so extraction NEVER raises and a
    bad tenant config can't take down the recon/deposit run. The default itself is
    always valid.
    """
    try:
        return re.compile(pattern)
    except (re.error, TypeError, OverflowError, RecursionError) as exc:
        logger.warning(
            "order_ref.invalid_pattern",
            pattern=pattern,
            error=str(exc),
        )
        return re.compile(DEFAULT_ORDER_REF_PATTERN)


def extract_order_ref(text: str | None, pattern: str | None = None) -> str | None:
    """Extract an order reference from ``text`` using ``pattern``.

    ``pattern=None`` uses :data:`DEFAULT_ORDER_REF_PATTERN` (``R`` + 9 digits).
    Uses ``search`` (the ref need not be anchored). Returns ``group(1)`` when the
    pattern has a capture group, otherwise ``group(0)``. ``None``/empty ``text``
    returns ``None``. A malformed ``pattern`` falls back to the default (never
    raises).

    Examples:
        "Framework Marketplace Order ID: R628489275-XU9EPZPD" -> "R628489275"
        "Sales Order #R577684612" -> "R577684612"
        "R123456789" -> "R123456789"
        "STRIPE PAYOUT" -> None
    """
    if not text:
        return None
    compiled = _compiled(pattern if pattern is not None else DEFAULT_ORDER_REF_PATTERN)
    m = compiled.search(text)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


async def load_order_ref_pattern(db: AsyncSession, tenant_id: str) -> str:
    """Load a tenant's order-reference extraction pattern.

    Returns ``TenantConfig.order_ref_pattern`` when set, otherwise
    :data:`DEFAULT_ORDER_REF_PATTERN` (NULL/no-config => engine default). ONE
    shared helper — call it once per tenant per run, not per line.
    """
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))).scalar_one_or_none()
    if cfg is None or cfg.order_ref_pattern is None:
        return DEFAULT_ORDER_REF_PATTERN
    return cfg.order_ref_pattern
