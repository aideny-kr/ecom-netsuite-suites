import type { CeligoFlowStep, CeligoJson } from "@/hooks/use-celigo-flows";

/** One affordance chip on a canvas bubble, in Celigo's own per-side
 * connector-line order (see `affordanceChips`). Three states only:
 * `"configured"` (the data is there), `"none"` (looked, and there is
 * genuinely nothing), `"unsynced"` (the sync does not carry this field at
 * all, so this surface cannot say either way -- never rendered as `"none"`,
 * which would claim more than is known). */
export type Chip = {
  slot: "transform" | "hooks" | "output_filter" | "input_filter" | "ns_mapping" | "response_mapping";
  state: "configured" | "none" | "unsynced";
  label: string;
  attachmentId?: string;
  functionName?: string;
  versionLetter?: string | null;
  versionsCount?: number | null;
  copiesCount?: number | null;
  diverged?: boolean;
};

/** Counts a Celigo filter/expression tree as a rule count for a chip label.
 * `["and"|"or", ...]` counts its list members (a compound expression IS that
 * many rules); any other non-empty expression (e.g. a single
 * `["notequals", ...]`) is one rule; `null`/`[]` is zero. A `{rules: [...]}`
 * wrapper (the shape `inputFilter`/`filter` sometimes carries) unwraps one
 * level first.
 *
 * Same counting rule as the backend's `count_rules` (topology.py), reproduced
 * here because the client renders straight off `filter_json`/`mapping_json`
 * without a round trip -- the ONE deliberate difference is the `{rules}`
 * unwrap, which the backend never sees because its callers hand it the inner
 * list already. The two used to differ by accident as well (this side matched
 * the combinator case-sensitively and counted non-list members); both are
 * pinned by `chips.test.ts`. */
export function countRules(value: CeligoJson): number {
  if (value && typeof value === "object" && !Array.isArray(value) && "rules" in (value as Record<string, unknown>)) {
    return countRules((value as { rules: CeligoJson }).rules);
  }
  if (!Array.isArray(value) || value.length === 0) return 0;
  const [head, ...rest] = value;
  // Matched exactly as `count_rules` matches it: case-insensitively, and
  // counting only the LIST members (each of those is an expression; a bare
  // string beside them is a label or flag, not a rule). These two details are
  // the only places the two implementations of this one rule used to
  // disagree.
  if (typeof head === "string" && (head.toLowerCase() === "and" || head.toLowerCase() === "or")) {
    return rest.filter((r) => Array.isArray(r)).length;
  }
  return 1;
}

/** `mapping_json`'s CONFIRMED live shape for a response/lookup mapping is
 * `{fields: [...]}` -- returns the field count, or `null` when the value
 * isn't that shape (so the caller can tell "no mapping" from "a shape this
 * client doesn't recognise yet"). */
function mappingFieldsCount(value: CeligoJson): number | null {
  if (value && typeof value === "object" && !Array.isArray(value) && Array.isArray((value as Record<string, unknown>).fields)) {
    return (value as { fields: unknown[] }).fields.length;
  }
  return null;
}

function filterChip(slot: "input_filter" | "output_filter", filterJson: CeligoJson, noneLabel: string): Chip {
  const n = countRules(filterJson);
  if (n === 0) return { slot, state: "none", label: noneLabel };
  return { slot, state: "configured", label: `filter · ${n} rule${n === 1 ? "" : "s"}` };
}

function responseMappingChip(mappingJson: CeligoJson): Chip {
  const n = mappingFieldsCount(mappingJson);
  if (n === null) return { slot: "response_mapping", state: "none", label: "no resp. map" };
  return { slot: "response_mapping", state: "configured", label: `⇄ response · ${n} field${n === 1 ? "" : "s"}` };
}

/** The import's NetSuite field mapping never comes through the sync (a
 * standing gap, not a bug) -- every destination shows this chip verbatim,
 * regardless of what `mapping_json` actually holds. */
const NS_MAPPING_CHIP: Chip = { slot: "ns_mapping", state: "unsynced", label: "⇄ NS field map · not synced" };

/** No transform is tracked by the sync at all (no field carries it) -- every
 * source/lookup shows this chip verbatim; it is a `"none"`, not an
 * `"unsynced"`, because Celigo's own Flow Builder shows the same thing for
 * these adaptor types (a genuine absence, not a sync gap). */
const TRANSFORM_CHIP: Chip = { slot: "transform", state: "none", label: "no transform" };

function hooksChip(step: Pick<CeligoFlowStep, "attachments">): Chip {
  const hook = step.attachments[0];
  if (!hook) return { slot: "hooks", state: "none", label: "no hooks" };
  return {
    slot: "hooks",
    state: "configured",
    label: `HK ${hook.function_name ?? "hook"}`,
    attachmentId: hook.id,
    functionName: hook.function_name ?? undefined,
    versionLetter: hook.script_version_letter,
    versionsCount: hook.script_versions_count,
    copiesCount: hook.script_copies_count,
    diverged: hook.script_content_diverged ?? undefined,
  };
}

/** Celigo hangs affordances on the connector line in a fixed order per side
 * (mockup legend): sources -- transform · hooks · output filter;
 * destinations -- input filter · NetSuite mapping · response mapping ·
 * hooks; lookups -- input filter · response mapping · hooks · transform.
 * Same order every time so an absent chip is visible and bubbles stay
 * comparable at a glance. `filter_json` backs whichever filter is the
 * step's OWN (a source's output filter, a lookup/destination's input
 * filter) -- one field, meaning read per `kind`. */
export function affordanceChips(step: CeligoFlowStep): Chip[] {
  const hooks = hooksChip(step);
  switch (step.kind) {
    case "source":
      return [TRANSFORM_CHIP, hooks, filterChip("output_filter", step.filter_json, "no output filter")];
    case "lookup":
      return [filterChip("input_filter", step.filter_json, "no input filter"), responseMappingChip(step.mapping_json), hooks, TRANSFORM_CHIP];
    case "destination":
    default:
      return [
        filterChip("input_filter", step.filter_json, "no input filter"),
        NS_MAPPING_CHIP,
        { slot: "response_mapping", state: "none", label: "no resp. map" },
        hooks,
      ];
  }
}
