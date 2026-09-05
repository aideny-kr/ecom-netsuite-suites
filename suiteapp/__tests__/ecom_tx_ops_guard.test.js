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
            copy.body.subtotal = copy.lines.reduce(
              (total, line) => total + line.amount,
              0,
            );
            copy.body.taxtotal = copy.body.taxitem
              ? Math.round(copy.body.subtotal * copy.body.taxrate) / 100
              : copy.lines.reduce(
                  (total, line) =>
                    total +
                    (line.tax1amt === undefined
                      ? line.custcol_fw_vat_amount
                      : line.tax1amt),
                  0,
                );
            copy.body.total =
              Math.round(
                (copy.body.subtotal +
                  copy.body.taxtotal +
                  copy.body.shippingcost) *
                  100,
              ) / 100;
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

function legacyRequest(mode = "aggregate_header") {
  const input = request();
  Object.assign(state.body, {
    custbody_fw_solidus_tax_amount: 18,
    shippingtax1rate: 0,
    shippingtax2rate: 0,
  });
  state.lines[0].istaxable = true;
  if (mode === "aggregate_header") {
    Object.assign(state.body, {
      taxitem: "610",
      taxrate: 19.5652174,
      shippingcost: 0,
      total: 108,
      custbody_fw_solidus_order_total: 108,
      istaxable: true,
    });
    delete state.lines[0].taxcode;
    delete state.lines[0].taxrate1;
    input.after.body_changes.taxrate = "20";
    input.after.body_changes.custbody_fw_solidus_order_total = "120";
    input.after.expected_totals.shippingcost = "0";
    input.after.expected_totals.total = "120";
  } else {
    state.lines[0].tax1amt = 18;
    state.lines[0].taxrate1 = 20.001;
    input.after.line_changes[0].fields.tax1amt = "20";
  }
  const result = restlet.get({
    action: "snapshot",
    record_id: "63",
    reference_field: "tranid",
    tax_mode: mode,
    tax_code_id: "610",
  });
  expect(result).toEqual(expect.objectContaining({ success: true }));
  input.before = result.snapshot;
  input.after.body_changes.custbody_fw_solidus_tax_amount = "20";
  return input;
}

test.each(["aggregate_header", "line_tax_amount"])(
  "legacy %s snapshot binds native and custom tax fields before one save",
  (mode) => {
    const input = legacyRequest(mode);
    expect(saved()).toBe(0);
    expect(input.before.tax_profile).toEqual({ mode, tax_code_id: "610" });
    expect(input.before.custbody_fw_solidus_tax_amount).toBe("18");
    expect(input.before.lines[0].istaxable).toBe(true);
    if (mode === "aggregate_header") {
      expect(input.before.taxrate).toBe("19.5652174");
      expect(input.before.taxitem).toBe("610");
      expect(input.before.lines[0].taxcode).toBeUndefined();
      expect(input.before.lines[0].taxrate1).toBeUndefined();
    } else {
      expect(input.before.lines[0].tax1amt).toBe("18");
      expect(input.before.lines[0].taxrate1).toBe("20.001");
    }
    expect(restlet.post(input).status).toBe("saved");
    expect(saved()).toBe(1);
    expect(state.body.custbody_fw_solidus_tax_amount).toBe(20);
    if (mode === "line_tax_amount") {
      expect(state.lines[0].tax1amt).toBe(20);
      expect(state.lines[0].taxrate1).toBe(20.001);
    } else {
      expect(state.body.taxrate).toBe(20);
    }
  },
);

test.each([
  [
    "aggregate_header",
    () => {
      state.body.taxrate = 21;
    },
  ],
  [
    "aggregate_header",
    () => {
      state.body.taxitem = "611";
    },
  ],
  [
    "aggregate_header",
    () => {
      state.body.istaxable = false;
    },
  ],
  [
    "aggregate_header",
    () => {
      state.lines[0].istaxable = false;
    },
  ],
  [
    "line_tax_amount",
    () => {
      state.lines[0].tax1amt = 19;
    },
  ],
  [
    "line_tax_amount",
    () => {
      state.lines[0].taxcode = "611";
    },
  ],
  [
    "line_tax_amount",
    () => {
      state.lines[0].taxrate1 = 25;
    },
  ],
  [
    "line_tax_amount",
    () => {
      state.body.custbody_fw_solidus_tax_amount = 19;
    },
  ],
])("legacy %s native drift rejects before any save", (mode, change) => {
  const input = legacyRequest(mode);
  change();
  expect(restlet.post(input).status).toBe("rejected");
  expect(saved()).toBe(0);
});

test.each([
  [
    "aggregate_header",
    (input) => {
      input.after.body_changes.custbody_fw_solidus_tax_amount = "21";
    },
  ],
  [
    "aggregate_header",
    (input) => {
      input.after.body_changes.taxrate = "0";
    },
  ],
  [
    "aggregate_header",
    (input) => {
      input.after.body_changes.taxrate = "1001";
    },
  ],
  [
    "aggregate_header",
    (input) => {
      input.after.line_changes[0].fields.taxrate1 = "20";
    },
  ],
  [
    "aggregate_header",
    (input) => {
      input.after.line_changes[0].fields.tax1amt = "20";
    },
  ],
  [
    "line_tax_amount",
    (input) => {
      input.after.line_changes[0].fields.tax1amt = "19";
    },
  ],
  [
    "line_tax_amount",
    (input) => {
      delete input.after.line_changes[0].fields.tax1amt;
    },
  ],
  [
    "line_tax_amount",
    (input) => {
      input.after.line_changes[0].fields.taxrate1 = "20";
    },
  ],
  [
    "line_tax_amount",
    (input) => {
      input.after.body_changes.taxrate = "20";
    },
  ],
])(
  "legacy %s disallows inconsistent native tax or writes for another mode",
  (mode, change) => {
    const input = legacyRequest(mode);
    change(input);
    expect(restlet.post(input).status).toBe("rejected");
    expect(saved()).toBe(0);
  },
);

test.each(["aggregate_header", "line_tax_amount"])(
  "legacy %s rejects taxed shipping and unproven line taxability",
  (mode) => {
    const input = legacyRequest(mode);
    const shipping = state.body.shippingcost;
    state.body.shippingcost = 2;
    input.before.shippingcost = "2";
    state.body.shippingtax1rate = 20;
    input.before.shippingtax1rate = "20";
    expect(restlet.post(input).status).toBe("rejected");
    state.body.shippingcost = shipping;
    input.before.shippingcost = String(shipping);
    state.body.shippingtax1rate = 0;
    input.before.shippingtax1rate = "0";
    state.lines[0].istaxable = false;
    input.before.lines[0].istaxable = false;
    expect(restlet.post(input).status).toBe("rejected");
    expect(saved()).toBe(0);
  },
);

test.each([
  [
    "aggregate_header",
    (copy) => {
      copy.body.taxrate = 19;
    },
  ],
  [
    "aggregate_header",
    (copy) => {
      copy.body.custbody_fw_solidus_tax_amount = 21;
    },
  ],
  [
    "line_tax_amount",
    (copy) => {
      copy.lines[0].tax1amt = 19;
    },
  ],
  [
    "line_tax_amount",
    (copy) => {
      copy.lines[0].taxrate1 = 30;
    },
  ],
])(
  "legacy %s save-time native override stays unknown even with matching totals",
  (mode, mutate) => {
    const input = legacyRequest(mode);
    saveHook = mutate;
    expect(restlet.post(input).status).toBe("unknown");
    expect(saved()).toBe(1);
  },
);

test("aggregate header refuses nonzero shipping even when separate rates are zero", () => {
  const input = legacyRequest("aggregate_header");
  state.body.shippingcost = 2;
  input.before.shippingcost = "2";
  input.after.expected_totals.shippingcost = "2";
  input.after.expected_totals.total = "122";
  input.after.body_changes.custbody_fw_solidus_order_total = "122";
  expect(restlet.post(input).status).toBe("rejected");
  expect(saved()).toBe(0);
});

test("aggregate header rate must calculate from the approved taxable line subtotal", () => {
  const input = legacyRequest("aggregate_header");
  input.after.body_changes.taxrate = "19";
  expect(restlet.post(input).status).toBe("rejected");
  expect(saved()).toBe(0);
});

test.each([
  [4062, 428.58, "10.5509601"],
  [3, 1, "33.3333333"],
  [6, 1, "16.6666667"],
  [800, 0.01, "0.00125"],
])(
  "aggregate rate uses exact bounded seven-place rounding for subtotal %s",
  (subtotal, tax, rate) => {
    const input = legacyRequest("aggregate_header");
    const total = String(Math.round((subtotal + tax) * 100) / 100);
    Object.assign(input.after.line_changes[0].fields, {
      amount: String(subtotal),
      rate: String(subtotal / 2),
      custcol_fw_vat_amount: String(tax),
    });
    Object.assign(input.after.expected_totals, {
      subtotal: String(subtotal),
      taxtotal: String(tax),
      total,
    });
    Object.assign(input.after.body_changes, {
      taxrate: rate,
      custbody_fw_solidus_tax_amount: String(tax),
      custbody_fw_solidus_order_total: total,
    });
    expect(restlet.post(input).status).toBe("saved");
    expect(saved()).toBe(1);
  },
);
