import json
import time
import uuid
from dataclasses import dataclass
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_quoteattr

import structlog
from sqlalchemy import ARRAY, Text, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_learned_rule import TenantLearnedRule
from app.services.chat.llm_adapter import BaseLLMAdapter
from app.services.chat.llm_purpose import with_llm_purpose

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


@dataclass(frozen=True)
class EntityMatch:
    script_id: str
    entity_type: str
    description: str | None
    sim: float


# For every entity (kept in order by WITH ORDINALITY): the best pg_trgm match on
# natural_name and the best on script_id -- the two lookups that used to run per entity.
_BEST_MATCHES_SQL = text(
    """
    SELECT e.ord,
           n.script_id AS n_script_id, n.entity_type AS n_entity_type, n.description AS n_description, n.sim AS n_sim,
           s.script_id AS s_script_id, s.entity_type AS s_entity_type, s.description AS s_description, s.sim AS s_sim
    FROM unnest(CAST(:entities AS text[])) WITH ORDINALITY AS e(term, ord)
    LEFT JOIN LATERAL (
        SELECT m.script_id, m.entity_type, m.description, similarity(m.natural_name, e.term) AS sim
        FROM tenant_entity_mapping m
        WHERE m.tenant_id = :tenant_id AND m.natural_name % e.term
        ORDER BY similarity(m.natural_name, e.term) DESC
        LIMIT 1
    ) n ON true
    LEFT JOIN LATERAL (
        SELECT m.script_id, m.entity_type, m.description, similarity(m.script_id, e.term) AS sim
        FROM tenant_entity_mapping m
        WHERE m.tenant_id = :tenant_id AND m.script_id % e.term
        ORDER BY similarity(m.script_id, e.term) DESC
        LIMIT 1
    ) s ON true
    ORDER BY e.ord
    """
).bindparams(bindparam("entities", type_=ARRAY(Text)))


async def best_entity_matches(db: AsyncSession, tenant_id: uuid.UUID, entities: list[str]) -> list[EntityMatch | None]:
    """The best mapping for each entity, in one query (it was two per entity).

    Users can name a field by its display name ("FW Platform") or its script ID
    ("custbody_fw_platform"), so both columns are searched; the stronger match wins and
    a tie goes to the display name, exactly as the per-entity lookup chose.
    """
    if not entities:
        return []
    rows = (await db.execute(_BEST_MATCHES_SQL, {"entities": entities, "tenant_id": tenant_id})).all()
    matches: list[EntityMatch | None] = [None] * len(entities)
    for row in rows:
        name = (
            EntityMatch(row.n_script_id, row.n_entity_type, row.n_description, float(row.n_sim))
            if row.n_sim is not None
            else None
        )
        script = (
            EntityMatch(row.s_script_id, row.s_entity_type, row.s_description, float(row.s_sim))
            if row.s_sim is not None
            else None
        )
        if name and script:
            matches[row.ord - 1] = name if name.sim >= script.sim else script
        else:
            matches[row.ord - 1] = name or script
    return matches


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
    @with_llm_purpose("entity_extraction")
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
        entities = [str(entity) for entity in extracted_entities]
        for entity, match in zip(entities, await best_entity_matches(db, tenant_id, entities), strict=True):
            if match:
                score = match.sim
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
