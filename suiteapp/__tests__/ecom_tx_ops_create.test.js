const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const fixture = require("./fixtures/transaction_create_input.json");
const clone = (value) =>
  value instanceof Date
    ? new Date(value)
    : Array.isArray(value)
      ? value.map(clone)
      : value && typeof value === "object"
        ? Object.fromEntries(
            Object.entries(value).map(([k, v]) => [k, clone(v)]),
          )
        : value;
let data,
  record,
  query,
  runtime,
  restlet,
  drafts,
  saved,
  reads,
  enabled,
  draftHook,
  saveHook;
function rec(body = {}, lines = [], type = "salesorder") {
  const addresses = { billingaddress: {}, shippingaddress: {} };
  let current;
  const obj = {
    type,
    id: body.id,
    getValue: ({ fieldId }) => body[fieldId],
    setValue: jest.fn(({ fieldId, value }) => {
      body[fieldId] = value;
    }),
    getFields: () => Object.keys(body),
    getField: ({ fieldId }) => ({
      isMandatory: [
        "entity",
        "subsidiary",
        "currency",
        "trandate",
        "tranid",
      ].includes(fieldId),
    }),
    getSublists: () => (type === "customer" ? ["currency"] : ["item"]),
    getLineCount: ({ sublistId }) =>
      sublistId === "currency" ? 1 : sublistId === "item" ? lines.length : 0,
    getSublistValue: ({ sublistId, fieldId, line }) =>
      sublistId === "currency" ? "4" : lines[line][fieldId],
    getSublistText: ({ fieldId, line }) =>
      fieldId === "units" ? "Each" : lines[line][fieldId],
    selectNewLine: jest.fn(() => {
      current = {
        units: "1",
        quantityfulfilled: 0,
        quantitybilled: 0,
        isclosed: false,
        createwo: false,
        createpo: "",
        taxrate1: 0,
        istaxable: true,
      };
    }),
    setCurrentSublistValue: jest.fn(({ fieldId, value }) => {
      current[fieldId] = value;
    }),
    getCurrentSublistValue: ({ fieldId }) => current[fieldId],
    getCurrentSublistField: () => ({ isMandatory: false }),
    hasCurrentSublistSubrecord: () => false,
    commitLine: jest.fn(() => {
      lines.push(current);
      current = undefined;
      body.subtotal = lines.reduce((sum, l) => sum + l.amount, 0);
      body.taxtotal = body.taxitem
        ? Math.round(body.subtotal * body.taxrate) / 100
        : lines.reduce((sum, l) => sum + (l.tax1amt || 0), 0);
      body.total = body.subtotal + body.taxtotal + (body.shippingcost || 0);
      if (draftHook) draftHook(body, lines);
    }),
    getSubrecord: ({ fieldId }) => ({
      setValue: jest.fn(({ fieldId: key, value }) => {
        addresses[fieldId][key] = value;
      }),
      getValue: ({ fieldId: key }) => addresses[fieldId][key],
    }),
    save: jest.fn((options) => {
      saved.push({ body: clone(body), lines: clone(lines), options });
      if (saveHook) saveHook(body, lines);
      obj.id = "63";
      body.lastmodifieddate = new Date();
      data.persisted = obj;
      return "63";
    }),
  };
  return obj;
}
beforeEach(() => {
  enabled = false;
  drafts = [];
  saved = [];
  reads = [];
  draftHook = null;
  saveHook = null;
  data = {
    absence: [],
    customers: [
      {
        id: "40",
        email: "buyer@example.invalid",
        subsidiary: "3",
        currency: "4",
        isinactive: "F",
        creditholdoverride: "AUTO",
        creditlimit: null,
      },
    ],
    currencies: [
      { id: "4", symbol: "EUR", currencyprecision: "2", isinactive: "F" },
    ],
    items: [
      {
        id: "600",
        itemid: "NATIVE-1",
        itemtype: "InvtPart",
        isinactive: "F",
        unitstype: "1",
        saleunit: "1",
      },
    ],
    units: [
      {
        internalid: "1",
        unitstype: "1",
        conversionrate: "1",
        unitname: "Each",
      },
    ],
    locations: [
      { id: "4", name: "Warehouse A", subsidiary: "3", isinactive: "F" },
    ],
    relationships: [],
    periods: [
      {
        id: "99",
        closed: "F",
        alllocked: "F",
        arlocked: "F",
        aplocked: "F",
        isadjust: "F",
      },
    ],
  };
  query = {
    runSuiteQL: jest.fn((options) => {
      reads.push(options);
      const sql = options.query.toLowerCase();
      const rows = sql.includes("from accountingperiod")
        ? data.periods
        : sql.includes("from customersubsidiaryrelationship")
          ? data.relationships
          : sql.includes("from unitstypeuom")
            ? data.units
            : sql.includes("from customer")
              ? data.customers
              : sql.includes("from currency")
                ? data.currencies
                : sql.includes("from location")
                  ? data.locations
                  : sql.includes("from item")
                    ? data.items
                    : sql.includes("from transaction")
                      ? data.absence
                      : null;
      if (rows === null) throw Error("Unexpected metadata query");
      return { asMappedResults: () => clone(rows) };
    }),
  };
  record = {
    create: jest.fn(() => {
      const draft = rec({
        customform: "1",
        terms: null,
        exchangerate: 1.1,
        orderstatus: "A",
        shippingcost: 0,
        handlingcost: 0,
        discounttotal: 0,
        subtotal: 0,
        taxtotal: 0,
        total: 0,
        shippingtax1rate: 0,
        shippingtax2rate: 0,
        tobeemailed: false,
        tobefaxed: false,
        getauth: false,
        paypalprocess: false,
      });
      drafts.push(draft);
      return draft;
    }),
    load: jest.fn(({ type, id }) => {
      if (type === "salesorder") return data.persisted;
      if (type === "customer")
        return rec(
          {
            id,
            email: data.customers[0].email,
            subsidiary: data.customers[0].subsidiary,
            currency: "4",
            isinactive: false,
            creditholdoverride: "AUTO",
            creditlimit: null,
          },
          [],
          "customer",
        );
      if (type === "currency")
        return rec(
          { id, symbol: "EUR", currencyprecision: 2, isinactive: false },
          [],
          "currency",
        );
      if (type === "inventoryitem")
        return rec(
          {
            id,
            itemid: "NATIVE-1",
            isinactive: false,
            unitstype: "1",
            saleunit: "1",
          },
          [],
          "inventoryitem",
        );
      if (type === "shipitem")
        return rec({ id, isinactive: false }, [], "shipitem");
      throw Error("Unexpected metadata load");
    }),
  };
  runtime = {
    accountId: "6738075_SB1",
    getCurrentScript: () => ({
      getRemainingUsage: () => 1000,
      getParameter: ({ name }) =>
        name === "custscript_ecom_tx_create_enabled" ? enabled : false,
    }),
  };
  const modules = {
    "N/record": record,
    "N/query": query,
    "N/runtime": runtime,
    "N/log": { audit: jest.fn(), error: jest.fn() },
  };
  const createPath = path.join(
    __dirname,
    "../src/FileCabinet/SuiteScripts/ecom_tx_ops_create.js",
  );
  if (fs.existsSync(createPath))
    vm.runInNewContext(fs.readFileSync(createPath, "utf8"), {
      Date,
      define: (deps, factory) => {
        modules["./ecom_tx_ops_create"] = factory(
          ...deps.map((name) => modules[name]),
        );
      },
    });
  vm.runInNewContext(
    fs.readFileSync(
      path.join(
        __dirname,
        "../src/FileCabinet/SuiteScripts/ecom_tx_ops_guard.js",
      ),
      "utf8",
    ),
    {
      Date,
      define: (deps, factory) => {
        restlet = factory(...deps.map((name) => modules[name]));
      },
    },
  );
});
function preview(input = clone(fixture)) {
  return restlet.post({
    schema_version: 1,
    action: "preview_create",
    account_id: input.account_id,
    input,
  });
}
function approved(input = clone(fixture)) {
  const result = preview(input);
  expect(result.success).toBe(true);
  return {
    schema_version: 1,
    action: "sync_missing_order",
    account_id: input.account_id,
    work_key: "b".repeat(64),
    approval_expires_at: new Date(Date.now() + 600000).toISOString(),
    before: { missing: true, order_reference: input.order_reference },
    after: { input: clone(input), preview: result.preview },
  };
}
test("read-only native draft preview works while create writes remain disabled", () => {
  const result = preview();
  expect(result.success).toBe(true);
  expect(result.create_enabled).toBe(false);
  expect(result.preview).toEqual(expect.any(Object));
  expect(record.create).toHaveBeenCalledTimes(1);
  expect(saved).toHaveLength(0);
});
test("native preview matches the shared Python transport contract fixture", () => {
  expect(preview().preview).toEqual(
    require("./fixtures/transaction_create_preview.json"),
  );
  expect(saved).toHaveLength(0);
});
test("a duplicate full reference anywhere in the account prevents even draft construction", () => {
  data.absence = [{ id: "999" }];
  expect(preview()).toEqual(expect.objectContaining({ success: false }));
  expect(record.create).not.toHaveBeenCalled();
  expect(saved).toHaveLength(0);
});
test.each(["customer", "currency", "unit", "location"])(
  "unknown or conflicting %s metadata prevents a save",
  (kind) => {
    if (kind === "customer") data.customers.push(clone(data.customers[0]));
    if (kind === "currency") data.currencies[0].currencyprecision = "0";
    if (kind === "unit") data.units[0].conversionrate = "12";
    if (kind === "location") data.locations[0].subsidiary = "2";
    expect(preview()).toEqual(expect.objectContaining({ success: false }));
    expect(saved).toHaveLength(0);
  },
);
test("disabled native creation cannot save a valid approved draft", () => {
  const request = approved();
  expect(restlet.post(request)).toEqual(
    expect.objectContaining({ success: false }),
  );
  expect(saved).toHaveLength(0);
});
test("exact approved creation saves once and remains unverified pending platform reads", () => {
  enabled = true;
  const request = approved();
  const result = restlet.post(request);
  expect(result).toEqual(
    expect.objectContaining({
      success: true,
      status: "saved",
      record_id: "63",
      verified: false,
      work_key: request.work_key,
    }),
  );
  expect(saved).toHaveLength(1);
  expect(saved[0].body).toEqual(
    expect.objectContaining({
      orderstatus: "A",
      externalid: fixture.external_id,
      tranid: fixture.order_reference,
      custbody_ecom_tx_ops_work_key: request.work_key,
    }),
  );
  expect(saved[0].options.ignoreMandatoryFields).toBe(false);
});
test("changed native draft after approval stops before save", () => {
  enabled = true;
  const request = approved();
  data.customers[0].id = "41";
  expect(restlet.post(request)).toEqual(
    expect.objectContaining({ success: false }),
  );
  expect(saved).toHaveLength(0);
});
test("a save exception is unknown and the guard does not retry", () => {
  enabled = true;
  const request = approved();
  saveHook = () => {
    throw Error("Provider detail must not escape");
  };
  const result = restlet.post(request);
  expect(result).toEqual(
    expect.objectContaining({
      success: false,
      status: "unknown",
      verified: false,
    }),
  );
  expect(JSON.stringify(result)).not.toContain("Provider detail");
  expect(saved).toHaveLength(1);
});

test.each([0, -1])(
  "a native FX rate of %s cannot produce an approval draft",
  (rate) => {
    draftHook = (body) => {
      body.exchangerate = rate;
    };
    expect(preview()).toEqual(expect.objectContaining({ success: false }));
    expect(saved).toHaveLength(0);
  },
);

test.each([
  ["custom_form_id", "customform"],
  ["terms_id", "terms"],
])(
  "an explicit %s cannot be changed by native sourcing",
  (inputField, nativeField) => {
    const input = clone(fixture);
    input[inputField] = "1";
    draftHook = (body) => {
      body[nativeField] = "2";
    };
    expect(preview(input)).toEqual(expect.objectContaining({ success: false }));
    expect(saved).toHaveLength(0);
  },
);

test("an inherited discount item remains unsupported even when its current amount is zero", () => {
  draftHook = (body) => {
    body.discountitem = "99";
  };
  expect(preview()).toEqual(expect.objectContaining({ success: false }));
  expect(saved).toHaveLength(0);
});

test("customer credit limits changing between metadata reads block the draft", () => {
  const original = record.load.getMockImplementation();
  record.load.mockImplementation((args) => {
    const result = original(args);
    if (args.type === "customer")
      result.setValue({ fieldId: "creditlimit", value: 100 });
    return result;
  });
  expect(preview()).toEqual(expect.objectContaining({ success: false }));
  expect(saved).toHaveLength(0);
});

test("independent created-order reads retain work attribution and never create another draft", () => {
  enabled = true;
  const request = approved();
  expect(restlet.post(request).status).toBe("saved");
  enabled = false;
  const draftCount = record.create.mock.calls.length;
  const result = restlet.get({
    action: "created_snapshot",
    record_id: "63",
    line_count: "1",
    tax_mode: "line_tax_amount",
    tax_code_id: "610",
    inventory_mode: "line_location",
  });
  expect(result).toEqual(
    expect.objectContaining({
      success: true,
      creation: expect.objectContaining({
        record_id: "63",
        work_key: request.work_key,
        record: request.after.preview.record,
      }),
    }),
  );
  expect(record.create).toHaveBeenCalledTimes(draftCount);
  expect(saved).toHaveLength(1);
});

test("a matching order without the work key cannot establish creation attribution", () => {
  enabled = true;
  const request = approved();
  expect(restlet.post(request).status).toBe("saved");
  data.persisted.setValue({
    fieldId: "custbody_ecom_tx_ops_work_key",
    value: "",
  });
  expect(
    restlet.get({
      action: "created_snapshot",
      record_id: "63",
      line_count: "1",
      tax_mode: "line_tax_amount",
      tax_code_id: "610",
      inventory_mode: "line_location",
    }).success,
  ).toBe(false);
  expect(saved).toHaveLength(1);
});

function aggregateInput() {
  const input = clone(fixture);
  input.tax_profile.mode = "aggregate_header";
  input.native_tax_rounding = "half_up";
  return input;
}

test("aggregate-tax and cross-subsidiary creation preserves the approved native routing", () => {
  enabled = true;
  const input = aggregateInput();
  input.inventory_mode = "cross_subsidiary";
  input.lines[0].inventory_subsidiary_id = "2";
  data.locations[0].subsidiary = "2";
  data.customers[0].subsidiary = "1";
  data.relationships = [{ entity: "40", subsidiary: "3" }];
  const request = approved(input);
  expect(restlet.post(request).status).toBe("saved");
  expect(saved).toHaveLength(1);
  expect(saved[0].lines[0]).toEqual(
    expect.objectContaining({
      inventorylocation: "4",
      inventorysubsidiary: "2",
    }),
  );
  expect(saved[0].body).toEqual(
    expect.objectContaining({
      taxitem: "610",
      taxrate: 20,
      iscrosssubtransaction: true,
    }),
  );
});

test.each([
  ["taxitem", "999"],
  ["istaxable", false],
])(
  "native aggregate %s changes cannot be approved even if the amounts still match",
  (field, value) => {
    draftHook = (body) => {
      body[field] = value;
    };
    expect(preview(aggregateInput()).success).toBe(false);
    expect(saved).toHaveLength(0);
  },
);

test.each([
  "missing relationship",
  "held customer",
  "inactive item",
  "serialized item",
  "customer currency",
])("%s prevents native creation", (kind) => {
  if (kind === "missing relationship") data.customers[0].subsidiary = "1";
  if (kind === "held customer") data.customers[0].creditholdoverride = "ON";
  if (kind === "inactive item") data.items[0].isinactive = "T";
  const original = record.load.getMockImplementation();
  if (kind === "serialized item" || kind === "customer currency")
    record.load.mockImplementation((args) => {
      const result = original(args);
      if (kind === "serialized item" && args.type === "inventoryitem")
        result.type = "serializedinventoryitem";
      if (kind === "customer currency" && args.type === "customer")
        result.getSublistValue = () => "1";
      return result;
    });
  if (kind === "customer currency") data.customers[0].currency = "1";
  expect(preview().success).toBe(false);
  expect(saved).toHaveLength(0);
});

test.each([
  "duplicate",
  "closed period",
  "expired approval",
  "work key unavailable",
  "low governance",
])(
  "%s arriving after draft comparison still prevents the sole save",
  (kind) => {
    enabled = true;
    const request = approved();
    const original = record.create.getMockImplementation();
    record.create.mockImplementation((args) => {
      const result = original(args);
      const set = result.setValue.getMockImplementation();
      result.setValue.mockImplementation((value) => {
        if (value.fieldId === "custbody_ecom_tx_ops_work_key") {
          if (kind === "duplicate") data.absence = [{ id: "999" }];
          if (kind === "closed period") data.periods[0].closed = "T";
          if (kind === "expired approval")
            request.approval_expires_at = new Date(
              Date.now() - 1000,
            ).toISOString();
          if (kind === "low governance")
            runtime.getCurrentScript = () => ({ getRemainingUsage: () => 90 });
          if (kind === "work key unavailable") return;
        }
        set(value);
      });
      return result;
    });
    expect(restlet.post(request).success).toBe(false);
    expect(saved).toHaveLength(0);
  },
);

test.each(["state", "amount", "work key"])(
  "save-time %s changes stay unknown and never retry",
  (kind) => {
    enabled = true;
    const request = approved();
    saveHook = (body) => {
      if (kind === "state") body.orderstatus = "B";
      if (kind === "amount") body.total += 1;
      if (kind === "work key")
        body.custbody_ecom_tx_ops_work_key = "a".repeat(64);
    };
    expect(restlet.post(request)).toEqual(
      expect.objectContaining({
        success: false,
        status: "unknown",
        verified: false,
      }),
    );
    expect(saved).toHaveLength(1);
  },
);
