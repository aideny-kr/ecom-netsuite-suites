export type MappingRow = Record<string, string>;
export type ScopeDraft = ReturnType<typeof emptyDraft>;
export function emptyDraft() {
  return {
    name: "",
    sourceId: "",
    connectionId: "",
    targetId: "",
    account: "",
    subsidiary: "",
    reference: "",
    currencies: [] as MappingRow[],
    taxes: [] as MappingRow[],
    entities: [] as MappingRow[],
    taxRounding: "",
    propose: false,
    schedule: false,
    interval: "60",
    orders: "100",
    calls: "100",
    deadline: "900",
  };
}
export interface ConfigInput {
  name: string;
  source_step_id: string;
  netsuite_connection_id: string;
  netsuite_account_id: string;
  subsidiary_id: string;
  target_step_id: string | null;
  record_type: "salesorder";
  schedule_enabled: boolean;
  interval_minutes: number;
  max_orders: number;
  max_api_calls: number;
  deadline_seconds: number;
  mapping_json: {
    action_mode: "detect_only" | "propose_actions";
    reference_field: string;
    currency_minor_units: Record<string, number>;
    business_entity_subsidiaries: Record<string, string>;
    tax_rules: Record<
      string,
      {
        rate: string;
        included: boolean;
        rounding: "half_up" | "half_even";
        netsuite_tax_id: string;
      }
    >;
    netsuite_tax_rounding: "half_up" | "half_even" | null;
  };
}
function fail(message: string): never {
  throw new Error(message);
}
function integer(value: string, min: number, max: number, label: string) {
  if (!/^\d+$/.test(value) || Number(value) < min || Number(value) > max)
    fail(`${label} must be a whole number from ${min} to ${max}.`);
  return Number(value);
}
function active(rows: MappingRow[], key: string, label: string) {
  const clean = rows
    .map((row) =>
      Object.fromEntries(Object.entries(row).map(([k, v]) => [k, v.trim()])),
    )
    .filter((row) => Object.values(row).some(Boolean));
  if (clean.length > 500) fail(`Too many ${label} rows.`);
  const seen = new Set<string>();
  for (const row of clean) {
    if (!row[key] || seen.has(row[key]))
      fail(`${label} keys must be present and unique.`);
    seen.add(row[key]);
  }
  return clean;
}
export function buildConfigInput(draft: ScopeDraft): ConfigInput {
  const name = draft.name.trim(),
    account = draft.account.trim(),
    subsidiary = draft.subsidiary.trim(),
    reference = draft.reference.trim();
  if (!name || name.length > 255 || !draft.sourceId || !draft.connectionId)
    fail("Choose a source and destination and name the scope.");
  if (!/^\d+(?:[_-](?:SB\d+|RP))?$/i.test(account))
    fail(
      "Enter the exact NetSuite account ID, including its sandbox suffix when applicable.",
    );
  if (!/^\d{1,30}$/.test(subsidiary))
    fail("Enter the explicit destination subsidiary ID.");
  if (!/^(tranid|otherrefnum|externalid|custbody_[a-z0-9_]+)$/.test(reference))
    fail("Choose a supported exact order-reference field.");
  const currencies = active(draft.currencies, "code", "Currency").map((row) => {
    if (!/^[A-Z]{3}$/.test(row.code))
      fail("Currency codes must use three uppercase ISO letters.");
    return [
      row.code,
      integer(row.places, 0, 6, "Currency decimal places"),
    ] as const;
  });
  const entities = active(draft.entities, "entity", "Business entity").map(
    (row) => {
      if (row.entity.length > 255 || !/^\d{1,30}$/.test(row.subsidiary))
        fail("Every business entity needs an explicit subsidiary ID.");
      return [row.entity, row.subsidiary] as const;
    },
  );
  const taxes = active(draft.taxes, "source", "Tax rule").map((row) => {
    if (!/^\d{1,30}$/.test(row.source) || !/^\d{1,30}$/.test(row.destination))
      fail("Tax rules require explicit source and NetSuite tax IDs.");
    if (!/^(?:[0-9](?:\.\d{1,12})?|10(?:\.0{1,12})?)$/.test(row.rate))
      fail(
        "Enter each tax rate as an exact fraction from 0 to 10, for example 0.20.",
      );
    if (!["included", "additional"].includes(row.basis))
      fail("Choose whether each tax is included or additional.");
    if (row.rounding !== "half_up" && row.rounding !== "half_even")
      fail("Choose an explicit rounding policy for each tax rule.");
    return [
      row.source,
      {
        rate: row.rate,
        included: row.basis === "included",
        rounding: row.rounding,
        netsuite_tax_id: row.destination,
      },
    ] as const;
  });
  if (
    draft.taxRounding &&
    draft.taxRounding !== "half_up" &&
    draft.taxRounding !== "half_even"
  )
    fail("Choose a supported NetSuite tax rounding policy.");
  return {
    name,
    source_step_id: draft.sourceId,
    netsuite_connection_id: draft.connectionId,
    netsuite_account_id: account,
    subsidiary_id: subsidiary,
    target_step_id: draft.targetId || null,
    record_type: "salesorder",
    schedule_enabled: draft.schedule,
    interval_minutes: integer(draft.interval, 5, 10080, "Schedule interval"),
    max_orders: integer(draft.orders, 1, 10000, "Order limit"),
    max_api_calls: integer(draft.calls, 1, 2000, "API call limit"),
    deadline_seconds: integer(draft.deadline, 30, 3600, "Run deadline"),
    mapping_json: {
      action_mode: draft.propose ? "propose_actions" : "detect_only",
      reference_field: reference,
      currency_minor_units: Object.fromEntries(currencies),
      business_entity_subsidiaries: Object.fromEntries(entities),
      tax_rules: Object.fromEntries(taxes),
      netsuite_tax_rounding:
        draft.taxRounding === "half_up"
          ? "half_up"
          : draft.taxRounding === "half_even"
            ? "half_even"
            : null,
    },
  };
}
