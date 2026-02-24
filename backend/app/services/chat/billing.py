"""Credit deduction tollbooth for metered billing.

Called after each chat turn to atomically deduct credits from the
tenant's wallet. Uses SELECT FOR UPDATE to prevent race conditions
during concurrent AI queries.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.tenant_wallet import TenantWallet

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Credit costs per model tier — ordered from most expensive to cheapest
# so that "opus" matches before "mini" (which could false-match "ge*mini*")
_MODEL_COSTS: list[tuple[str, int]] = [
    # Opus tier = 3 credits
    ("opus", 3),
    # Sonnet / Pro tier = 2 credits
    ("sonnet", 2),
    ("pro", 2),
    # Haiku / Flash tier = 1 credit
    ("haiku", 1),
    ("flash", 1),
    ("nano", 1),
    ("mini", 1),
    ("lite", 1),
]


def calculate_cost(model: str) -> int:
    """Determine credit cost based on model name.

    Uses hyphen-delimited token matching to avoid false positives
    like 'gemini' matching 'mini'.
    """
    # Split on common delimiters to get model name tokens
    model_lower = model.lower()
    tokens = set(model_lower.replace("_", "-").split("-"))
    for key, cost in _MODEL_COSTS:
        if key in tokens:
            return cost
    # Fallback: substring match for models without standard delimiters
    for key, cost in _MODEL_COSTS:
        if key in model_lower:
            return cost
    return 1  # Default to 1 credit for unknown models


async def deduct_chat_credits(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    model: str,
) -> dict | None:
    """Atomically deduct credits from the tenant wallet.

    Uses SELECT FOR UPDATE row lock to prevent concurrent race conditions.
    Returns the wallet balance after deduction, or None if no wallet exists.
    Must be called within an existing transaction (before db.commit()).
    """
    cost = calculate_cost(model)
    if cost == 0:
        return None

    # Row-level lock on the wallet
    result = await db.execute(
        select(TenantWallet)
        .where(TenantWallet.tenant_id == tenant_id)
        .with_for_update()
    )
    wallet = result.scalar_one_or_none()

    if not wallet:
        return None  # No wallet = no billing (free tier or not configured)

    # Deduction logic: base credits first, then overage
    if wallet.base_credits_remaining >= cost:
        wallet.base_credits_remaining -= cost
    else:
        remainder = cost - wallet.base_credits_remaining
        wallet.base_credits_remaining = 0
        wallet.metered_credits_used += remainder

    logger.info(
        "billing.credits_deducted",
        extra={
            "tenant_id": str(tenant_id),
            "cost": cost,
            "model": model,
            "base_remaining": wallet.base_credits_remaining,
            "metered_used": wallet.metered_credits_used,
        },
    )

    return {
        "base_remaining": wallet.base_credits_remaining,
        "metered_used": wallet.metered_credits_used,
        "cost": cost,
    }
