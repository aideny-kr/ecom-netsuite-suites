import { describe, it, expect } from "vitest";
import {
  deriveDataTableIsMetric,
  coerceDataTableData,
  normalizeStreamEvent,
} from "@/lib/chat-stream";

describe("deriveDataTableIsMetric", () => {
  it("returns true for a raw persisted payload carrying suppress_llm_value: true", () => {
    expect(deriveDataTableIsMetric({ suppress_llm_value: true })).toBe(true);
  });

  it("returns true for an already-normalized object that has isMetric: true (idempotent — proves line-311 path safe)", () => {
    expect(deriveDataTableIsMetric({ isMetric: true })).toBe(true);
  });

  it("returns false for a plain SuiteQL payload (no metric flag)", () => {
    expect(deriveDataTableIsMetric({ query: "SELECT * FROM transaction" })).toBe(false);
  });

  it("returns false for a Metric/Value/Unit/Period columns payload when neither flag is set (guards against a columns-heuristic false positive)", () => {
    expect(
      deriveDataTableIsMetric({
        columns: ["Metric", "Value", "Unit", "Period"],
        query: "net_margin",
      }),
    ).toBe(false);
  });

  it("uses strict === true and does NOT coerce a truthy string", () => {
    expect(deriveDataTableIsMetric({ suppress_llm_value: "true" })).toBe(false);
  });
});

describe("coerceDataTableData", () => {
  it("round-trips the EXACT persisted metric payload and yields isMetric: true while preserving columns/rows/query", () => {
    const persistedMetricPayload = {
      columns: ["Metric", "Value", "Unit", "Period"],
      rows: [["Net Margin", "12.3", "percent", "Q1 FY2026"]],
      row_count: 1,
      query: "net_margin",
      truncated: false,
      suppress_llm_value: true,
    };
    const coerced = coerceDataTableData(persistedMetricPayload);
    expect(coerced.isMetric).toBe(true);
    expect(coerced.columns).toEqual(["Metric", "Value", "Unit", "Period"]);
    expect(coerced.rows).toEqual([["Net Margin", "12.3", "percent", "Q1 FY2026"]]);
    expect(coerced.row_count).toBe(1);
    expect(coerced.query).toBe("net_margin");
    expect(coerced.truncated).toBe(false);
  });

  it("yields isMetric: false for a persisted plain-SuiteQL payload (no flag)", () => {
    const persistedSuiteqlPayload = {
      columns: ["tranid", "amount"],
      rows: [["T-001", 500]],
      row_count: 1,
      query: "SELECT tranid, amount FROM transaction FETCH FIRST 1 ROWS ONLY",
      truncated: false,
    };
    const coerced = coerceDataTableData(persistedSuiteqlPayload);
    expect(coerced.isMetric).toBe(false);
    expect(coerced.query).toBe("SELECT tranid, amount FROM transaction FETCH FIRST 1 ROWS ONLY");
  });

  it("is idempotent over an already-normalized object that carries isMetric: true", () => {
    const normalized = {
      columns: ["Metric", "Value"],
      rows: [["Net Margin", "12.3"]],
      row_count: 1,
      query: "net_margin",
      truncated: false,
      isMetric: true,
    };
    const coerced = coerceDataTableData(normalized as unknown as Record<string, unknown>);
    expect(coerced.isMetric).toBe(true);
  });
});

describe("normalizeStreamEvent — live path regression (shared helper)", () => {
  it("sets isMetric: true when payload has suppress_llm_value: true", () => {
    const event = normalizeStreamEvent({
      type: "data_table",
      data: {
        columns: ["Metric", "Value"],
        rows: [["Net Margin", "12.3"]],
        row_count: 1,
        query: "net_margin",
        truncated: false,
        suppress_llm_value: true,
      },
    });
    expect(event?.type).toBe("data_table");
    if (event?.type === "data_table") {
      expect(event.data.isMetric).toBe(true);
    }
  });

  it("sets isMetric: false when suppress_llm_value is absent", () => {
    const event = normalizeStreamEvent({
      type: "data_table",
      data: {
        columns: ["tranid"],
        rows: [["T-001"]],
        row_count: 1,
        query: "SELECT tranid FROM transaction",
        truncated: false,
      },
    });
    if (event?.type === "data_table") {
      expect(event.data.isMetric).toBe(false);
    }
  });
});
