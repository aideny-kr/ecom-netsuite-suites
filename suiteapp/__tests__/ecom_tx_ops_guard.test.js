const fs = require("node:fs"),
  vm = require("node:vm"),
  path = require("node:path");
const sourcePath = path.join(
  __dirname,
  "../src/FileCabinet/SuiteScripts/ecom_tx_ops_guard.js",
);
const clone = (value) =>
  value instanceof Date
    ? new Date(value.getTime())
    : Array.isArray(value)
      ? value.map(clone)
      : value && typeof value === "object"
        ? Object.fromEntries(
            Object.entries(value).map(([key, item]) => [key, clone(item)]),
          )
        : value;
const initial = () => ({
  id: "63",
  body: {
    tranid: "R123456789-EU",
    entity: "40",
    subsidiary: "3",
    currency: "4",
    trandate: new Date("2026-09-04T07:00:00Z"),
    lastmodifieddate: new Date("2026-09-04T12:00:00Z"),
    exchangerate: 1.1,
    orderstatus: "B",
    total: 110,
    subtotal: 90,
    taxtotal: 18,
    shippingcost: 2,
    handlingcost: 0,
    discounttotal: 0,
    custbody_fw_solidus_order_total: 110,
    tobeemailed: false,
    tobefaxed: false,
    getauth: false,
    paypalprocess: false,
  },
  lines: [
    {
      line: "7",
      lineuniquekey: "12345",
      item: "600",
      quantity: 2,
      quantityfulfilled: 0,
      quantitybilled: 0,
      isclosed: false,
      rate: 45,
      amount: 90,
      custcol_fw_solidus_line_id: "11",
      custcol_fw_vat_amount: 18,
      taxcode: "610",
      taxrate1: 20,
    },
  ],
});
let state,
  record,
  query,
  runtime,
  log,
  restlet,
  loaded,
  periodRows,
  calls,
  saveHook;
beforeEach(() => {
  state = initial();
  calls = [];
  loaded = [];
  saveHook = null;
  periodRows = [
    {
      id: "99",
      closed: "F",
      alllocked: "F",
      arlocked: "F",
      aplocked: "F",
      isadjust: "F",
    },
  ];
  record = {
    load: jest.fn(({ type, id }) => {
      if (type !== "salesorder" || String(id) !== state.id)
        throw new Error("unexpected record");
      const copy = clone(state),
        obj = {
          id: copy.id,
          getValue: ({ fieldId }) => copy.body[fieldId],
          getLineCount: ({ sublistId }) =>
            sublistId === "item" ? copy.lines.length : 0,
          getSublistValue: ({ fieldId, line }) => copy.lines[line][fieldId],
          setValue: jest.fn(({ fieldId, value }) => {
            calls.push(["body", fieldId]);
            copy.body[fieldId] = value;
          }),
          setSublistValue: jest.fn(({ fieldId, line, value }) => {
            calls.push(["line", fieldId, line]);
            copy.lines[line][fieldId] = value;
          }),
          save: jest.fn((options) => {
            calls.push(["save", options]);
            if (saveHook) saveHook(copy);
            copy.body.subtotal = 100;
            copy.body.taxtotal = 20;
            copy.body.total = 122;
            copy.body.lastmodifieddate = new Date("2026-09-04T12:01:00Z");
            state = clone(copy);
            return 63;
          }),
        };
      loaded.push(obj);
      return obj;
    }),
  };
  query = {
    runSuiteQL: jest.fn(() => {
      calls.push(["period"]);
      return { asMappedResults: () => clone(periodRows) };
    }),
  };
  runtime = {
    accountId: "1234567_SB1",
    getCurrentScript: () => ({
      getRemainingUsage: () => 800,
      getParameter: () => true,
    }),
  };
  log = { audit: jest.fn(), error: jest.fn(), debug: jest.fn() };
  const modules = {
    "N/record": record,
    "N/query": query,
    "N/runtime": runtime,
    "N/log": log,
  };
  vm.runInNewContext(fs.readFileSync(sourcePath, "utf8"), {
    Date,
    define: (deps, factory) => {
      restlet = factory(...deps.map((name) => modules[name]));
    },
  });
});
function snapshot() {
  const result = restlet.get({
    action: "snapshot",
    record_id: "63",
    reference_field: "tranid",
  });
  expect(result).toEqual(expect.objectContaining({ success: true }));
  return result.snapshot;
}
function request() {
  return {
    schema_version: 1,
    action: "correct_amounts",
    account_id: "1234567_SB1",
    work_key: "a".repeat(64),
    approval_expires_at: new Date(Date.now() + 600000).toISOString(),
    before: snapshot(),
    after: {
      body_changes: { custbody_fw_solidus_order_total: "122" },
      line_changes: [
        {
          line: "7",
          fields: { rate: "50", amount: "100", custcol_fw_vat_amount: "20" },
        },
      ],
      expected_totals: {
        total: "122",
        subtotal: "100",
        taxtotal: "20",
        shippingcost: "2",
        discounttotal: "0",
      },
    },
  };
}
function saved() {
  return loaded.reduce((n, obj) => n + obj.save.mock.calls.length, 0);
}
test("read-only snapshot retains scope, native amounts and original line IDs", () => {
  const before = snapshot();
  expect(before).toEqual(
    expect.objectContaining({
      record_id: "63",
      entity: "40",
      currency: "4",
      subsidiary: "3",
      order_reference: "R123456789-EU",
      reference_field: "tranid",
      exchangerate: "1.1",
      total: "110",
      period_id: "99",
      version: "2026-09-04T12:00:00.000Z",
      trandate: "2026-09-04",
    }),
  );
  expect(before.lines[0]).toEqual(
    expect.objectContaining({
      line: "7",
      lineuniquekey: "12345",
      amount: "90",
      quantity: "2",
    }),
  );
  expect(saved()).toBe(0);
  expect(JSON.stringify(before)).not.toMatch(/email|token|address/);
});
test("one save uses line ID 7 at index 0 and rechecks the period last", () => {
  const input = request();
  calls = [];
  const result = restlet.post(input);
  expect(result).toEqual(
    expect.objectContaining({
      success: true,
      schema_version: 1,
      status: "saved",
      record_id: "63",
      verified: false,
    }),
  );
  expect(saved()).toBe(1);
  expect(loaded[1].setSublistValue).toHaveBeenCalledWith({
    sublistId: "item",
    line: 0,
    fieldId: "rate",
    value: 50,
  });
  expect(loaded[1].save).toHaveBeenCalledWith({
    enableSourcing: false,
    ignoreMandatoryFields: false,
  });
  const index = calls.findIndex((c) => c[0] === "save");
  expect(calls[index - 1][0]).toBe("period");
  expect(state.body.currency).toBe("4");
  expect(state.body.exchangerate).toBe(1.1);
  expect(log.audit).toHaveBeenCalled();
});
test.each([
  ["currency", "5"],
  ["subsidiary", "4"],
  ["entity", "41"],
  ["exchangerate", 1.2],
  ["orderstatus", "G"],
  ["lastmodifieddate", new Date("2026-09-04T12:00:01Z")],
  ["total", 111],
])("body drift %s blocks save", (field, value) => {
  const input = request();
  state.body[field] = value;
  expect(restlet.post(input).success).toBe(false);
  expect(saved()).toBe(0);
});
test.each([
  ["quantityfulfilled", 1],
  ["quantitybilled", 1],
  ["isclosed", true],
  ["item", "601"],
  ["lineuniquekey", "999"],
  ["rate", 46],
])("line drift %s blocks save", (field, value) => {
  const input = request();
  state.lines[0][field] = value;
  expect(restlet.post(input).success).toBe(false);
  expect(saved()).toBe(0);
});
test.each(["closed", "alllocked", "arlocked", "aplocked", "isadjust"])(
  "period %s is rechecked before save",
  (flag) => {
    const input = request();
    let reads = 0;
    query.runSuiteQL.mockImplementation(() => ({
      asMappedResults: () => {
        reads++;
        return clone(
          reads === 2 ? [{ ...periodRows[0], [flag]: "T" }] : periodRows,
        );
      },
    }));
    expect(restlet.post(input).success).toBe(false);
    expect(saved()).toBe(0);
  },
);
test.each(
  [
    [],
    [{ id: "99", closed: "F" }],
    [
      {
        id: "99",
        closed: "F",
        alllocked: "F",
        arlocked: "F",
        aplocked: "F",
        isadjust: "F",
      },
      {
        id: "100",
        closed: "F",
        alllocked: "F",
        arlocked: "F",
        aplocked: "F",
        isadjust: "F",
      },
    ],
  ].map((rows) => [rows]),
)("unknown or ambiguous period blocks save", (rows) => {
  const input = request();
  periodRows = rows;
  expect(restlet.post(input).success).toBe(false);
  expect(saved()).toBe(0);
});
test.each([
  (input) => (input.after.body_changes.currency = "5"),
  (input) => (input.after.body_changes.exchangerate = "2"),
  (input) => (input.after.line_changes[0].fields.quantity = "4"),
  (input) => (input.after.line_changes[0].line = "0"),
  (input) => input.after.line_changes.push(clone(input.after.line_changes[0])),
  (input) => (input.after.line_changes[0].fields.rate = 50),
  (input) => (input.after.line_changes[0].fields.rate = "9007199254740993"),
  (input) => (input.after.replaceAll = true),
  (input) => (input.before.lines = []),
  (input) => (input.account_id = "1234567"),
  (input) => (input.approval_expires_at = "2000-01-01T00:00:00Z"),
  (input) => (input.action = "delete"),
])("invalid or widened intent never saves", (mutate) => {
  const input = request();
  mutate(input);
  expect(restlet.post(input).success).toBe(false);
  expect(saved()).toBe(0);
});
test("deployment is read-only until explicitly enabled", () => {
  const input = request();
  runtime.getCurrentScript = () => ({
    getRemainingUsage: () => 800,
    getParameter: () => false,
  });
  expect(
    restlet.get({
      action: "snapshot",
      record_id: "63",
      reference_field: "tranid",
    }).success,
  ).toBe(true);
  expect(restlet.post(input).code).toBe("guard_disabled");
  expect(saved()).toBe(0);
});
test.each(["tobeemailed", "tobefaxed", "getauth", "paypalprocess"])(
  "existing %s side effect flag blocks save",
  (flag) => {
    const input = request();
    state.body[flag] = true;
    expect(restlet.post(input).success).toBe(false);
    expect(saved()).toBe(0);
  },
);
test("save conflict is not retried and provider details stay private", () => {
  const input = request();
  saveHook = () => {
    throw Object.assign(new Error("private customer@example.test"), {
      name: "RCRD_HAS_BEEN_CHANGED",
    });
  };
  const result = restlet.post(input);
  expect(saved()).toBe(1);
  expect(result.status).toBe("unknown");
  expect(result.verified).toBe(false);
  expect(JSON.stringify([result, log.error.mock.calls])).not.toContain(
    "customer@example.test",
  );
});
test("post-save mismatch is unknown without compensation or another save", () => {
  const input = request(),
    original = record.load.getMockImplementation();
  record.load.mockImplementation((options) => {
    const obj = original(options);
    if (loaded.length === 3) {
      const get = obj.getValue;
      obj.getValue = (options) =>
        options.fieldId === "total" ? 123 : get(options);
    }
    return obj;
  });
  const result = restlet.post(input);
  expect(saved()).toBe(1);
  expect(result.status).toBe("unknown");
});
test("insufficient governance budget prevents record load", () => {
  runtime.getCurrentScript = () => ({
    getRemainingUsage: () => 1,
    getParameter: () => true,
  });
  expect(
    restlet.get({
      action: "snapshot",
      record_id: "63",
      reference_field: "tranid",
    }).success,
  ).toBe(false);
  expect(record.load).not.toHaveBeenCalled();
});

test.each(["total", "subtotal", "taxtotal", "shippingcost"])(
  "inconsistent expected %s is rejected before save",
  (field) => {
    const input = request();
    input.after.expected_totals[field] = "999";
    expect(restlet.post(input).status).toBe("rejected");
    expect(saved()).toBe(0);
  },
);
test("a changed line amount must equal the exact approved quantity times rate", () => {
  const input = request();
  input.after.line_changes[0].fields.rate = "51";
  expect(restlet.post(input).status).toBe("rejected");
  expect(saved()).toBe(0);
});
