import json
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_entity_mapping import TenantEntityMapping
from app.models.tenant_learned_rule import TenantLearnedRule
from app.services.chat.llm_adapter import BaseLLMAdapter

logger = structlog.get_logger(__name__)

EXTRACTOR_SYSTEM_PROMPT = """\
You are a fast named entity extractor for NetSuite business context.
Read the user prompt and output a strict JSON array of potential entities. Extract:
1. Custom record names (e.g., "Inventory Processor", "Integration Log")
2. Custom field names (e.g., "Rush flag", "External Order Number")
3. Status values or list option names that sound tenant-specific (e.g., "Failed", "Completed", "Pending", "In Progress", "Ordoro")
4. Saved search names or report names
Do NOT extract generic NetSuite terms like "sales order", "customer", "invoice", or "transaction".
Output ONLY valid JSON, e.g., ["Inventory Processor", "Failed", "Ordoro"]\
"""


class TenantEntityResolver:
    """
    Interceptor layer that runs before the main reasoning agent.
    Extracts potential NetSuite entities using a fast LLM call (e.g. Haiku)
    and maps them against the tenant's high-speed Postgres pg_trgm index.
    """

    @staticmethod
    async def resolve_entities(
        user_message: str,
        tenant_id: uuid.UUID,
        db: AsyncSession,
        adapter: BaseLLMAdapter,
        model: str,
    ) -> str:
        prompt = f"User prompt: {user_message}"
        response = await adapter.create_message(
            model=model,
            max_tokens=256,
            system=EXTRACTOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            content = response.text_blocks[0] if response.text_blocks else "[]"
            logger.info(
                "tenant_resolver.raw_extraction",
                raw_content=content[:500],
            )
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            extracted_entities = json.loads(content)
            if not isinstance(extracted_entities, list):
                extracted_entities = []
        except Exception as e:
            logger.warning("tenant_resolver.extraction_failed", exc_info=e)
            return ""

        logger.info(
            "tenant_resolver.extracted_entities",
            entities=extracted_entities,
            count=len(extracted_entities),
        )
        print(f"[TENANT_RESOLVER] Extracted entities: {extracted_entities}", flush=True)

        if not extracted_entities:
            return ""

        resolved = []
        for entity in extracted_entities:
            # High-speed pg_trgm lookup combining tenant_id strict equality with fuzzy matching
            query = (
                select(TenantEntityMapping, func.similarity(TenantEntityMapping.natural_name, entity).label("sim"))
                .where(TenantEntityMapping.tenant_id == tenant_id)
                .where(TenantEntityMapping.natural_name.op("%")(entity))
                .order_by(func.similarity(TenantEntityMapping.natural_name, entity).desc())
                .limit(1)
            )
            result = await db.execute(query)
            row = result.first()
            if row:
                match = row.TenantEntityMapping
                score = row.sim
                logger.info(
                    "tenant_resolver.match_found",
                    user_term=entity,
                    script_id=match.script_id,
                    entity_type=match.entity_type,
                    similarity=round(score, 3),
                )
                print(
                    f"[TENANT_RESOLVER] MATCH: '{entity}' → {match.script_id} ({match.entity_type}, sim={score:.3f})",
                    flush=True,
                )
                resolved.append(
                    {
                        "user_term": entity,
                        "internal_script_id": match.script_id,
                        "entity_type": match.entity_type,
                        "metadata": match.description or "",
                        "confidence_score": round(score, 2),
                    }
                )
            else:
                logger.info(
                    "tenant_resolver.no_match",
                    user_term=entity,
                )

        # Extract Tenant Learned Rules (Semantic Memory)
        learned_rules = []
        try:
            rule_query = (
                select(TenantLearnedRule)
                .where(TenantLearnedRule.tenant_id == tenant_id)
                .where(TenantLearnedRule.is_active == True)  # noqa: E712
            )
            rule_result = await db.execute(rule_query)
            learned_rules = list(rule_result.scalars().all())
        except Exception as e:
            logger.warning("tenant_resolver.learned_rules_extraction_failed", exc_info=e)

        if not resolved and not learned_rules:
            logger.info("tenant_resolver.no_resolved_entities_or_rules")
            return ""

        # Construct the XML block to attach to the context
        xml_parts = [
            "<tenant_vernacular>",
            "    <instruction_context>",
            "        The following entities and rules have been mapped to their specific internal NetSuite constraints for this particular tenant. ",
            "        You MUST use these exact inner script IDs and rules when constructing your SuiteQL FROM and WHERE clauses.",
            "    </instruction_context>",
        ]

        if resolved:
            xml_parts.append("    <resolved_entities>")
            for r in resolved:
                xml_parts.append("        <entity>")
                xml_parts.append(f"            <user_term>{r['user_term']}</user_term>")
                xml_parts.append(f"            <internal_script_id>{r['internal_script_id']}</internal_script_id>")
                xml_parts.append(f"            <entity_type>{r['entity_type']}</entity_type>")
                xml_parts.append(f"            <metadata>{r['metadata']}</metadata>")
                xml_parts.append(f"            <confidence_score>{r['confidence_score']}</confidence_score>")
                xml_parts.append("        </entity>")
            xml_parts.append("    </resolved_entities>")

        if learned_rules:
            xml_parts.append("    <learned_rules>")
            xml_parts.append(
                "        <!-- Explicit business logic / schema rules learned for this tenant. FOLLOW THESE STRICTLY. -->"
            )
            for rule in learned_rules:
                xml_parts.append(f'        <rule category="{rule.rule_category or "general"}">')
                xml_parts.append(f"            {rule.rule_description}")
                xml_parts.append("        </rule>")
            xml_parts.append("    </learned_rules>")

        xml_parts.append("</tenant_vernacular>")

        xml_output = "\n".join(xml_parts)
        logger.info(
            "tenant_resolver.xml_output",
            resolved_count=len(resolved),
            xml_preview=xml_output[:1000],
        )
        # Also print to stdout for docker log visibility
        print(f"[TENANT_RESOLVER] Resolved {len(resolved)} entities. XML:\n{xml_output[:1500]}", flush=True)
        return xml_output
