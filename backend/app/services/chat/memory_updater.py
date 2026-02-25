"""Background correction extractor — learns from user corrections in chat.

Regex-gated: costs zero tokens for ~95% of messages. Only triggers an LLM call
when the user's message contains explicit correction signals like "no, it's...",
"actually...", "remember that...", etc.

When triggered, uses a fast model to extract the correction as a structured rule
and persists it via TenantLearnedRule for future sessions.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.chat.llm_adapter import BaseLLMAdapter

logger = logging.getLogger(__name__)

# Regex patterns that signal the user is correcting the bot
_CORRECTION_PATTERNS = re.compile(
    r"""(?xi)
    (?:
        \b(?:no|nope|wrong|incorrect|not\s+right),?\s |
        \bthat(?:'s|\s+is)\s+(?:wrong|incorrect|not\s+right) |
        \bactually[,\s] |
        \bremember\s+that\b |
        \balways\s+(?:use|show|include|add)\b |
        \bnever\s+(?:use|show|include|add)\b |
        \bit\s+should\s+be\b |
        \bnot\s+\w+[,\s]+it(?:'s|\s+is)\b |
        \bplease\s+(?:always|never)\b |
        \bfrom\s+now\s+on\b |
        \bin\s+the\s+future\b |
        \bdon(?:'t|t)\s+(?:use|show|include)\b |
        \bwhen\s+i\s+say\b |
        \bis\s+stored\s+in\b |
        \bthe\s+(?:field|column|table)\s+(?:is|for)\b |
        \buse\s+(?:customrecord|custbody|custcol|custitem)\w*\b
    )
    """,
)

_EXTRACTION_PROMPT = """\
Analyze this user message for corrections or persistent preferences about an AI data assistant.

Extract TWO types of corrections if present:

Type 1 — Entity/Field Mapping (NetSuite-specific):
If the user maps a natural name to a script ID (e.g., "inventory processor is customrecord_foo",
"the platform field is custitem_fw_platform"):
{
  "entity_correction": {
    "natural_name": "the natural language term",
    "script_id": "the exact NetSuite script/field ID",
    "entity_type": "customrecord | customlist | transaction_body_field | item_field | entity_field"
  }
}

Type 2 — General Rule/Preference:
If the user states a general rule (e.g., "always show currency", "never round amounts",
"when I say today I mean PST"):
{
  "rule": {
    "description": "Clear 1-2 sentence description of the rule",
    "category": "output_preference | query_logic | status_mapping | field_mapping | currency | general"
  }
}

Return a JSON object with both fields (set to null if not applicable):
{
  "entity_correction": null,
  "rule": null
}

User message: {{USER_MESSAGE}}
Previous assistant response: {{ASSISTANT_PREVIEW}}
"""


def has_correction_signal(user_message: str) -> bool:
    """Fast regex check — returns True if the message looks like a correction."""
    return bool(_CORRECTION_PATTERNS.search(user_message))


async def maybe_extract_correction(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message: str,
    assistant_message: str,
    adapter: BaseLLMAdapter,
    model: str,
) -> bool:
    """Check for corrections in the user message and persist as learned rules.

    Returns True if a correction was saved, False otherwise.
    """
    # Fast gate: skip 95% of messages with zero cost
    if not has_correction_signal(user_message):
        return False

    logger.info(
        "memory_updater.correction_signal",
        extra={"tenant_id": str(tenant_id), "preview": user_message[:80]},
    )

    assistant_preview = assistant_message[:500] if assistant_message else ""
    prompt = _EXTRACTION_PROMPT.replace("{{USER_MESSAGE}}", user_message[:1000])
    prompt = prompt.replace("{{ASSISTANT_PREVIEW}}", assistant_preview)

    try:
        response = await adapter.create_message(
            model=model,
            max_tokens=256,
            system="You extract corrections from chat messages. Return only JSON.",
            messages=[{"role": "user", "content": prompt}],
        )

        text = "\n".join(response.text_blocks) if response.text_blocks else ""
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            return False

        data = json.loads(json_match.group())
        saved = False

        # Handle entity mapping correction
        entity = data.get("entity_correction")
        if entity and entity.get("natural_name") and entity.get("script_id"):
            saved = await _save_entity_mapping(
                db, tenant_id, entity["natural_name"], entity["script_id"],
                entity.get("entity_type", "general"),
            )

        # Handle general learned rule
        rule = data.get("rule")
        if rule and rule.get("description"):
            saved = await _save_learned_rule(
                db, tenant_id, user_id, rule["description"],
                rule.get("category", "general"),
            ) or saved

        if saved:
            from app.services.audit_service import log_event

            await log_event(
                db=db,
                tenant_id=tenant_id,
                category="memory",
                action="correction.auto_saved",
                actor_id=user_id,
                resource_type="chat_correction",
                resource_id=str(tenant_id),
                payload={"user_message_preview": user_message[:200]},
            )

        return saved

    except Exception:
        logger.warning("memory_updater.extraction_failed", exc_info=True)
        return False


async def _save_entity_mapping(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    natural_name: str,
    script_id: str,
    entity_type: str,
) -> bool:
    """Upsert an entity mapping from a user correction."""
    from app.models.tenant_entity_mapping import TenantEntityMapping

    stmt = insert(TenantEntityMapping).values(
        tenant_id=tenant_id,
        entity_type=entity_type,
        natural_name=natural_name,
        script_id=script_id,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_tenant_entity_type_script",
        set_={"natural_name": natural_name},
    )
    await db.execute(stmt)

    logger.info(
        "memory_updater.entity_mapping_saved",
        extra={
            "tenant_id": str(tenant_id),
            "natural_name": natural_name,
            "script_id": script_id,
        },
    )
    return True


async def _save_learned_rule(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    description: str,
    category: str,
) -> bool:
    """Save a general learned rule from a user correction."""
    from app.models.tenant_learned_rule import TenantLearnedRule

    rule = TenantLearnedRule(
        tenant_id=tenant_id,
        rule_category=category,
        rule_description=description,
        is_active=True,
        created_by=user_id,
    )
    db.add(rule)
    await db.flush()

    logger.info(
        "memory_updater.learned_rule_saved",
        extra={
            "tenant_id": str(tenant_id),
            "rule_id": str(rule.id),
            "category": category,
        },
    )
    return True
