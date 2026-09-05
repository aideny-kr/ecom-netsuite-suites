import { describe, expect, it } from "vitest";
import { buildConfigInput, emptyDraft } from "./setup-input";

function draft() {
  return {
    ...emptyDraft(),
    name: "EU orders",
    sourceId: "source",
    connectionId: "ns",
    account: "6738075_SB1",
    subsidiary: "5",
    reference: "tranid",
  };
}
describe("transaction scope input", () => {
  it("defaults to detection only without an enabled schedule or invented money metadata", () => {
    const value = buildConfigInput(draft());
    expect(value.schedule_enabled).toBe(false);
    expect(value.mapping_json.action_mode).toBe("detect_only");
    expect(value.mapping_json.currency_minor_units).toEqual({});
    expect(value.mapping_json.tax_rules).toEqual({});
    expect(value.mapping_json.business_entity_subsidiaries).toEqual({});
  });
  it("retains exact fractional rates and requires separate action and schedule choices", () => {
    const input = {
      ...draft(),
      propose: true,
      currencies: [{ code: "KWD", places: "3" }],
      taxes: [
        {
          source: "7",
          rate: "0.123456789012",
          basis: "included",
          rounding: "half_even",
          destination: "610",
        },
      ],
      entities: [{ entity: "Framework BV", subsidiary: "5" }],
    };
    const value = buildConfigInput(input);
    expect(value.mapping_json.tax_rules["7"].rate).toBe("0.123456789012");
    expect(value.mapping_json.currency_minor_units.KWD).toBe(3);
    expect(value.mapping_json.action_mode).toBe("propose_actions");
    expect(value.schedule_enabled).toBe(false);
  });
  it.each([
    { account: "6738075/other" },
    { reference: "tranid OR 1=1" },
    { currencies: [{ code: "EUR", places: "" }] },
    {
      currencies: [
        { code: "EUR", places: "2" },
        { code: "EUR", places: "3" },
      ],
    },
    {
      taxes: [
        {
          source: "7",
          rate: "0.2",
          basis: "",
          rounding: "half_up",
          destination: "610",
        },
      ],
    },
    {
      taxes: [
        {
          source: "7",
          rate: "NaN",
          basis: "included",
          rounding: "half_up",
          destination: "610",
        },
      ],
    },
    { entities: [{ entity: "legacy", subsidiary: "" }] },
    { interval: "1" },
  ])("rejects incomplete or ambiguous mappings %j", (change) => {
    expect(() => buildConfigInput({ ...draft(), ...change })).toThrow();
  });
});
