import { describe, expect, it } from "vitest";
import { affordanceChips, countRules } from "../chips";
import type { CeligoFlowStep } from "@/hooks/use-celigo-flows";

const base: CeligoFlowStep = { id: "s", celigo_id: "c", role: "processor", kind: "destination", router_id: null, branch_id: null, branch_key: "$root", sequence: 0, adaptor_type: "NetSuiteDistributedImport", connection_celigo_id: null, filter_json: null, mapping_json: null, proceed_on_failure: null, skip_retries: null, attachments: [], reference_name: null, record_type: "salesorder", operation: "add", search_id: null, error_count: 0 };
const hook = { id: "a1", flow_id: "f", flow_step_id: "s", script_id: "scr", script_celigo_id: "scr", function_name: "preMap", json_path: "x.hooks.preMap", site_type: "hook", script_name: "ns_sales_order_premap", script_size_chars: 2443, script_copies_count: 7, script_versions_count: 3, script_version_letter: "C", script_content_diverged: true };

describe("affordanceChips — Celigo's per-side order, three states", () => {
  it("destination: input filter · NetSuite mapping (always unsynced) · response mapping · hooks", () => {
    const chips = affordanceChips({ ...base, attachments: [hook] });
    expect(chips.map((c) => `${c.slot}:${c.state}`)).toEqual(["input_filter:none", "ns_mapping:unsynced", "response_mapping:none", "hooks:configured"]);
    expect(chips[3]).toMatchObject({ label: "HK preMap", versionLetter: "C", versionsCount: 3, diverged: true });
  });
  it("source: transform · hooks · output filter; a filter_json counts as configured with its rule count", () => {
    const chips = affordanceChips({ ...base, role: "generator", kind: "source", adaptor_type: "HTTPExport", filter_json: ["and", ["equals", "a", "b"], ["equals", "c", "d"]] });
    expect(chips.map((c) => c.slot)).toEqual(["transform", "hooks", "output_filter"]);
    expect(chips[2]).toMatchObject({ state: "configured", label: "filter · 2 rules" });
  });
  it("lookup: input filter · response mapping · hooks · transform; mapping fields are counted", () => {
    const chips = affordanceChips({ ...base, kind: "lookup", adaptor_type: "HTTPExport", mapping_json: { fields: new Array(23).fill({}) } });
    expect(chips.map((c) => c.slot)).toEqual(["input_filter", "response_mapping", "hooks", "transform"]);
    expect(chips[1]).toMatchObject({ state: "configured", label: "⇄ response · 23 fields" });
  });
  it("a single-copy hook shows ×1, not a letter", () => {
    const chips = affordanceChips({ ...base, attachments: [{ ...hook, script_copies_count: 1, script_versions_count: 1, script_version_letter: null, script_content_diverged: false }] });
    expect(chips[3]).toMatchObject({ label: "HK preMap", copiesCount: 1, versionLetter: null, diverged: false });
  });
});

describe("countRules — 'and'/'or' count their members, anything else non-empty is 1, a {rules} object unwraps", () => {
  it("null and an empty list are 0 rules", () => {
    expect(countRules(null)).toBe(0);
    expect(countRules([])).toBe(0);
  });
  it("a single non-and/or expression is 1 rule", () => {
    expect(countRules(["notequals", ["string", ["extract", "x"]], "y"])).toBe(1);
  });
  it("'and'/'or' count their members", () => {
    expect(countRules(["and", ["equals", "a", "b"], ["equals", "c", "d"]])).toBe(2);
    expect(countRules(["or", ["equals", "a", "b"], ["equals", "c", "d"], ["equals", "e", "f"]])).toBe(3);
  });
  it("a {rules: [...]} object unwraps to the same count", () => {
    expect(countRules({ rules: ["and", ["equals", "a", "b"], ["equals", "c", "d"]] })).toBe(2);
    expect(countRules({ rules: [] })).toBe(0);
  });
});
