import json
import time
import uuid
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_quoteattr

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_entity_mapping import TenantEntityMapping
from app.models.tenant_learned_rule import TenantLearnedRule
from app.services.chat.llm_adapter import BaseLLMAdapter

logger = structlog.get_logger(__name__)

# Entity types that are NOT safe to inject as authoritative WHERE-clause filters.
# A match here is a list *value* (customlistvalue → "list_name.internal_id"), a
# list *definition* (customlist — queryable as a FROM target, but not a WHERE
# filter), or an operational reference (saved search / script / workflow). A list
# value especially can't be filtered on without knowing which field references it
# — injecting these as authoritative "use this script_id" filters caused confident-
# wrong answers (e.g. "Laptop 13" resolved to customlist_fw_cpu_platform.14, a value
# carried only by 12 spare-part SKUs). They are surfaced as advisory hints instead.
_NON_QUERYABLE_ENTITY_TYPES = frozenset(
    {"customlistvalue", "customlist", "savedsearch", "script", "scriptdeployment", "workflow"}
)


def _esc(value: object) -> str:
    """XML-escape a tenant/LLM-controlled value before interpolating it into the
    vernacular XML that is injected into the system prompt (prevents element
    break-out / prompt injection via a rule description or extracted entity)."""
    return _xml_escape(str(value))


EXTRACTOR_SYSTEM_PROMPT = """\
You are a fast named entity extractor for NetSuite business context.
Read the user prompt and output a strict JSON array of potential entities. Extract:
1. Custom record names (e.g., "Inventory Processor", "Integration Log")
2. Custom field names or business dimensions (e.g., "Rush flag", "External Order Number", "platform", "channel", "warehouse", "brand", "region")
3. Status values or list option names that sound tenant-specific (e.g., "Failed", "Completed", "Pending", "In Progress", "Ordoro")
4. Script or SuiteScript names (e.g., "Order Processor", "Fulfillment Scheduler")
5. Workflow names (e.g., "Approve Purchase Order", "Sales Order Routing")
6. Saved search names or report names
7. Any term that could be a reporting dimension, grouping field, or segment (e.g., "platform", "source", "category", "type", "location")
Do NOT extract generic NetSuite record types like "sales order", "customer", "invoice", or "transaction".
DO extract short business terms that could be custom fields (e.g., "platform", "channel", "source") — these are often custom body/item fields.
Output ONLY valid JSON, e.g., ["Inventory Processor", "Failed", "platform"]\
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
        print(f"[TENANT_RESOLVER] start | msg_len={len(user_message)}", flush=True)
        _t0 = time.time()
        response = await adapter.create_message(
            model=model,
            max_tokens=256,
            system=EXTRACTOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        print(
            f"[TENANT_RESOLVER] llm_call_complete in {time.time() - _t0:.2f}s",
            flush=True,
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
        advisory = []  # non-queryable matches (list values, scripts) — surfaced as caution, not filters
        for entity in extracted_entities:
            # High-speed pg_trgm lookup — search BOTH natural_name and script_id,
            # take the best match. This allows users to reference fields by either
            # display name ("FW Platform") or script ID ("custbody_fw_platform").
            name_query = (
                select(TenantEntityMapping, func.similarity(TenantEntityMapping.natural_name, entity).label("sim"))
                .where(TenantEntityMapping.tenant_id == tenant_id)
                .where(TenantEntityMapping.natural_name.op("%")(entity))
                .order_by(func.similarity(TenantEntityMapping.natural_name, entity).desc())
                .limit(1)
            )
            name_result = await db.execute(name_query)
            name_row = name_result.first()

            # Also search script_id (e.g., "custbody_fw_platform")
            script_query = (
                select(TenantEntityMapping, func.similarity(TenantEntityMapping.script_id, entity).label("sim"))
                .where(TenantEntityMapping.tenant_id == tenant_id)
                .where(TenantEntityMapping.script_id.op("%")(entity))
                .order_by(func.similarity(TenantEntityMapping.script_id, entity).desc())
                .limit(1)
            )
            script_result = await db.execute(script_query)
            script_row = script_result.first()

            # Pick the best match
            row = None
            if name_row and script_row:
                row = name_row if name_row.sim >= script_row.sim else script_row
            elif name_row:
                row = name_row
            elif script_row:
                row = script_row
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
                # Filter low-confidence matches to prevent wrong field injection
                from app.services.chat.agents.base_agent import _MIN_ENTITY_CONFIDENCE

                if score < _MIN_ENTITY_CONFIDENCE:
                    print(
                        f"[TENANT_RESOLVER] SKIPPED (low confidence {score:.3f} < {_MIN_ENTITY_CONFIDENCE}): '{entity}' → {match.script_id}",
                        flush=True,
                    )
                    continue
                entry = {
                    "user_term": entity,
                    "internal_script_id": match.script_id,
                    "entity_type": match.entity_type,
                    "metadata": match.description or "",
                    "confidence_score": round(score, 2),
                }
                # A list value / non-column reference can't be a WHERE-clause filter on
                # its own — route it to the advisory block instead of resolved_entities.
                if match.entity_type in _NON_QUERYABLE_ENTITY_TYPES:
                    advisory.append(entry)
                else:
                    resolved.append(entry)
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

        if not resolved and not advisory and not learned_rules:
            logger.info("tenant_resolver.no_resolved_entities_or_rules")
            return ""

        # Construct the XML block to attach to the context
        xml_parts = [
            "<tenant_vernacular>",
            "    <instruction_context>",
            "        The following have been mapped to this tenant's internal NetSuite constraints. ",
            "        Prefer the resolved entity script IDs and learned rules when constructing SuiteQL FROM and WHERE clauses. ",
            "        Any ambiguous entries below are ADVISORY ONLY — verify the field and value before using; never filter on them blindly.",
            "    </instruction_context>",
        ]

        if resolved:
            xml_parts.append("    <resolved_entities>")
            for r in resolved:
                xml_parts.append("        <entity>")
                xml_parts.append(f"            <user_term>{_esc(r['user_term'])}</user_term>")
                xml_parts.append(
                    f"            <internal_script_id>{_esc(r['internal_script_id'])}</internal_script_id>"
                )
                xml_parts.append(f"            <entity_type>{_esc(r['entity_type'])}</entity_type>")
                xml_parts.append(f"            <metadata>{_esc(r['metadata'])}</metadata>")
                xml_parts.append(f"            <confidence_score>{r['confidence_score']}</confidence_score>")
                xml_parts.append("        </entity>")
            xml_parts.append("    </resolved_entities>")

        if advisory:
            xml_parts.append("    <ambiguous_entities>")
            xml_parts.append(
                "        <!-- ADVISORY ONLY. Each term below matched a list VALUE or a "
                "non-column reference (script / saved-search / workflow), NOT a queryable column. "
                "Do NOT filter on matched_value directly. Identify the item/transaction field whose "
                "source list matches, confirm the value reflects the user's intent (it may tag only "
                "parts/variants, not the product), and prefer the tenant's documented class/category rules. -->"
            )
            for a in advisory:
                xml_parts.append("        <ambiguous_term>")
                xml_parts.append(f"            <user_term>{_esc(a['user_term'])}</user_term>")
                xml_parts.append(f"            <matched_value>{_esc(a['internal_script_id'])}</matched_value>")
                xml_parts.append(f"            <entity_type>{_esc(a['entity_type'])}</entity_type>")
                xml_parts.append("        </ambiguous_term>")
            xml_parts.append("    </ambiguous_entities>")

        if learned_rules:
            xml_parts.append("    <learned_rules>")
            xml_parts.append(
                "        <!-- Explicit business logic / schema rules learned for this tenant. FOLLOW THESE STRICTLY. -->"
            )
            for rule in learned_rules:
                xml_parts.append(f"        <rule category={_xml_quoteattr(rule.rule_category or 'general')}>")
                xml_parts.append(f"            {_esc(rule.rule_description)}")
                xml_parts.append("        </rule>")
            xml_parts.append("    </learned_rules>")

        xml_parts.append("</tenant_vernacular>")

        xml_output = "\n".join(xml_parts)
        logger.info(
            "tenant_resolver.xml_output",
            resolved_count=len(resolved),
            advisory_count=len(advisory),
            xml_preview=xml_output[:1000],
        )
        # Also print to stdout for docker log visibility
        print(
            f"[TENANT_RESOLVER] Resolved {len(resolved)} entities, {len(advisory)} advisory. XML:\n{xml_output[:1500]}",
            flush=True,
        )
        return xml_output
