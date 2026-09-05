/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 *
 * Narrow conditional sales-order updates. The platform owns human approval,
 * persistent spend, and its single-use dispatch ledger. This endpoint compares
 * the whole approved projection on one loaded record, then calls save once.
 * NetSuite's optimistic record locking protects concurrent record updates.
 * Period locks are separate records: checked immediately before save, without
 * claiming an atomic transaction spanning the period and the sales order.
 */
define(["N/record", "N/query", "N/runtime", "N/log"], (
  record,
  query,
  runtime,
  log,
) => {
  const VERSION = 1;
  const TOTALS = [
    "total",
    "subtotal",
    "taxtotal",
    "shippingcost",
    "discounttotal",
  ];
  const BODY_MONEY = [
    ...TOTALS,
    "handlingcost",
    "exchangerate",
    "custbody_fw_solidus_order_total",
  ];
  const LINE_IDS = [
    "line",
    "lineuniquekey",
    "item",
    "custcol_fw_solidus_line_id",
    "taxcode",
  ];
  const LINE_MONEY = [
    "quantity",
    "quantityfulfilled",
    "quantitybilled",
    "rate",
    "amount",
    "custcol_fw_vat_amount",
    "taxrate1",
  ];
  const BODY_WRITES = ["custbody_fw_solidus_order_total", "shippingcost"];
  const LINE_WRITES = ["rate", "amount", "custcol_fw_vat_amount", "taxrate1"];
  const EFFECT_FLAGS = ["tobeemailed", "tobefaxed", "getauth", "paypalprocess"];
  const fail = (code) => {
    const error = new Error(code);
    error.guardCode = code;
    throw error;
  };
  const usage = () => runtime.getCurrentScript().getRemainingUsage();
  const budget = (required) => {
    if (usage() < required) fail("governance_budget");
  };
  const object = (value) =>
    value && typeof value === "object" && !Array.isArray(value);
  function keys(value, allowed, required = allowed) {
    if (
      !object(value) ||
      Object.keys(value).some((key) => !allowed.includes(key)) ||
      required.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
    )
      fail("invalid_intent");
  }
  function identifier(value) {
    if (
      (typeof value !== "string" && typeof value !== "number") ||
      !/^\d{1,30}$/.test(String(value))
    )
      fail("invalid_identifier");
    return String(value);
  }
  function referenceField(value) {
    if (
      typeof value !== "string" ||
      value.length > 100 ||
      !/^(tranid|otherrefnum|externalid|custbody_[a-z0-9_]+)$/.test(value)
    )
      fail("invalid_reference_field");
    return value;
  }
  function account(value) {
    if (typeof value !== "string" || !/^\d+(?:[_-](?:SB\d+|RP))?$/i.test(value))
      fail("account_mismatch");
    return value.toUpperCase().replace("-", "_");
  }
  // No financial arithmetic uses binary floats. Values crossing N/record's
  // numeric API must round-trip to the identical decimal string first.
  function decimal(value) {
    if (typeof value !== "string" && typeof value !== "number")
      fail("invalid_decimal");
    let text = String(value);
    if (typeof value === "number" && /e/i.test(text)) {
      const match = /^(-?)(\d+)(?:\.(\d+))?e([+-]?\d+)$/i.exec(text);
      if (!match || Math.abs(Number(match[4])) > 24) fail("invalid_decimal");
      let digits = match[2] + (match[3] || ""),
        point = match[2].length + Number(match[4]);
      if (point < 0) {
        digits = "0".repeat(-point) + digits;
        point = 0;
      }
      if (point > digits.length) digits += "0".repeat(point - digits.length);
      text =
        match[1] + (digits.slice(0, point) || "0") + "." + digits.slice(point);
    }
    const match = /^(-?)(\d{1,24})(?:\.(\d{0,24}))?$/.exec(text);
    if (!match) fail("invalid_decimal");
    const whole = match[2].replace(/^0+(?=\d)/, ""),
      fraction = (match[3] || "").replace(/0+$/, "");
    if (fraction.length > 12) fail("decimal_precision");
    return (
      (whole === "0" && !fraction ? "" : match[1]) +
      whole +
      (fraction ? "." + fraction : "")
    );
  }
  function writeNumber(value) {
    if (typeof value !== "string") fail("decimal_string_required");
    const canonical = decimal(value),
      result = Number(canonical);
    if (
      canonical.startsWith("-") ||
      !Number.isFinite(result) ||
      decimal(result) !== canonical
    )
      fail("decimal_precision");
    return result;
  }
  function scaleOf(value) {
    return (decimal(value).split(".")[1] || "").length;
  }
  function scaledInteger(value, scale) {
    const parts = decimal(value).split(".");
    const result = Number(parts[0] + (parts[1] || "").padEnd(scale, "0"));
    if (!Number.isSafeInteger(result)) fail("arithmetic_precision");
    return result;
  }
  function fromScaled(value, scale) {
    if (!Number.isSafeInteger(value)) fail("arithmetic_precision");
    const sign = value < 0 ? "-" : "",
      digits = String(Math.abs(value)).padStart(scale + 1, "0");
    return decimal(
      sign +
        (scale ? digits.slice(0, -scale) + "." + digits.slice(-scale) : digits),
    );
  }
  function sum(values) {
    const scale = Math.max(...values.map(scaleOf));
    let result = 0;
    for (const value of values) {
      result += scaledInteger(value, scale);
      if (!Number.isSafeInteger(result)) fail("arithmetic_precision");
    }
    return fromScaled(result, scale);
  }
  function product(left, right) {
    const a = scaleOf(left),
      b = scaleOf(right);
    return fromScaled(scaledInteger(left, a) * scaledInteger(right, b), a + b);
  }
  function effectiveHeaderRate(subtotal, tax) {
    // Half-up percentage to seven places, using integers throughout. Outside
    // the proven arithmetic range, stop before save instead of rounding money.
    const scale = Math.max(scaleOf(subtotal), scaleOf(tax));
    const denominator = scaledInteger(subtotal, scale);
    const numerator = scaledInteger(tax, scale) * 1000000000;
    if (!Number.isSafeInteger(numerator)) fail("arithmetic_precision");
    if (denominator === 0) {
      if (numerator !== 0) fail("legacy_tax_rate_unproven");
      return "0";
    }
    if (denominator < 0 || numerator < 0) fail("legacy_tax_rate_unproven");
    let quotient = Math.floor(numerator / denominator);
    const multiplied = quotient * denominator;
    if (!Number.isSafeInteger(multiplied)) fail("arithmetic_precision");
    let remainder = numerator - multiplied;
    // Correct a floating division that lands on an adjacent integer against
    // its exact safe-integer product before applying the rounding policy.
    if (remainder < 0) {
      quotient -= 1;
      remainder += denominator;
    } else if (remainder >= denominator) {
      quotient += 1;
      remainder -= denominator;
    }
    if (remainder < 0 || remainder >= denominator) fail("arithmetic_precision");
    if (remainder >= denominator - remainder) quotient += 1;
    return fromScaled(quotient, 7);
  }
  function checkArithmetic(current, after) {
    const lines = current.lines.map((line) => {
      const change = after.line_changes.find(
        (change) => String(change.line) === line.line,
      );
      return { ...line, ...(change ? change.fields : {}) };
    });
    if (
      lines.some(
        (line) => product(line.rate, line.quantity) !== decimal(line.amount),
      )
    )
      fail("line_amount_inconsistent");
    // This v1 guard supports explicit line VAT only. An unallocated header
    // tax, shipping tax or discount needs a different reviewed mapping.
    if (
      sum(current.lines.map((line) => line.custcol_fw_vat_amount)) !==
      current.taxtotal
    )
      fail("unsupported_tax_allocation");
    const totals = {
      subtotal: sum(lines.map((line) => line.amount)),
      taxtotal: sum(lines.map((line) => line.custcol_fw_vat_amount)),
      shippingcost: decimal(
        after.body_changes.shippingcost === undefined
          ? current.shippingcost
          : after.body_changes.shippingcost,
      ),
      discounttotal: "0",
    };
    totals.total = sum([totals.subtotal, totals.taxtotal, totals.shippingcost]);
    if (
      TOTALS.some(
        (field) => totals[field] !== decimal(after.expected_totals[field]),
      )
    )
      fail("expected_totals_inconsistent");
    if (current.tax_profile) {
      const body = { ...current, ...after.body_changes };
      if (
        current.custbody_fw_solidus_tax_amount !== current.taxtotal ||
        decimal(body.custbody_fw_solidus_tax_amount) !== totals.taxtotal ||
        ((current.shippingcost !== "0" || totals.shippingcost !== "0") &&
          (current.shippingtax1rate !== "0" ||
            current.shippingtax2rate !== "0"))
      )
        fail("legacy_tax_allocation_unproven");
      if (current.tax_profile.mode === "line_tax_amount") {
        if (
          current.lines.some(
            (line) => line.tax1amt !== line.custcol_fw_vat_amount,
          ) ||
          lines.some(
            (line) =>
              decimal(line.tax1amt) !== decimal(line.custcol_fw_vat_amount),
          )
        )
          fail("legacy_tax_allocation_unproven");
      } else {
        if (current.shippingcost !== "0" || totals.shippingcost !== "0")
          fail("unsupported_aggregate_shipping");
        const rate = writeNumber(body.taxrate);
        if (
          rate > 1000 ||
          (rate === 0) !== (totals.taxtotal === "0") ||
          decimal(body.taxrate) !==
            effectiveHeaderRate(totals.subtotal, totals.taxtotal)
        )
          fail("legacy_tax_rate_unproven");
      }
    }
  }
  function instant(value) {
    if (!(value instanceof Date) || !Number.isFinite(value.getTime()))
      fail("unknown_record_clock");
    return value.toISOString();
  }
  function expiration(value) {
    if (typeof value !== "string" || !/T.*(?:Z|[+-]\d\d:\d\d)$/.test(value))
      fail("approval_expired");
    const remaining = Date.parse(value) - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0 || remaining > 900000)
      fail("approval_expired");
  }
  function openPeriod(date) {
    const rows = query
      .runSuiteQL({
        query:
          "SELECT id, closed, alllocked, arlocked, aplocked, isadjust FROM accountingperiod " +
          "WHERE isyear='F' AND isquarter='F' AND startdate <= TO_DATE(?, 'YYYY-MM-DD') " +
          "AND enddate >= TO_DATE(?, 'YYYY-MM-DD') FETCH FIRST 2 ROWS ONLY",
        params: [date, date],
      })
      .asMappedResults();
    if (
      !Array.isArray(rows) ||
      rows.length !== 1 ||
      ["closed", "alllocked", "arlocked", "aplocked", "isadjust"].some(
        (key) => rows[0][key] !== "F",
      )
    )
      fail("period_unavailable");
    return identifier(rows[0].id);
  }
  function taxProfile(value) {
    if (value === undefined || value === null) return null;
    keys(value, ["mode", "tax_code_id"]);
    if (!["aggregate_header", "line_tax_amount"].includes(value.mode))
      fail("unsupported_tax_profile");
    return { mode: value.mode, tax_code_id: identifier(value.tax_code_id) };
  }
  function lineIdentityMode(value) {
    if (value === undefined) return "source_line_id";
    if (!["source_line_id", "inventory_units"].includes(value))
      fail("unsupported_line_identity");
    return value;
  }
  function inventoryIds(value) {
    if (typeof value !== "string" || value.length > 15500)
      fail("unknown_inventory_identity");
    const ids = value
      .split(",")
      .map((id) => id.trim())
      .sort();
    if (
      ids.length > 500 ||
      ids.some((id) => !/^[1-9][0-9]{0,29}$/.test(id)) ||
      new Set(ids).size !== ids.length
    )
      fail("unknown_inventory_identity");
    return ids;
  }
  function originalSku(value) {
    if (
      typeof value !== "string" ||
      !value.length ||
      value.length > 255 ||
      value !== value.trim() ||
      /[\x00-\x1f]/.test(value)
    )
      fail("unknown_inventory_identity");
    return value;
  }
  function snapshot(
    order,
    field,
    profile = null,
    identityMode = "source_line_id",
  ) {
    const inventoryMode = lineIdentityMode(identityMode) === "inventory_units";
    const get = (fieldId) => order.getValue({ fieldId });
    const ref = get(referenceField(field));
    if (
      typeof ref !== "string" ||
      ref.length > 100 ||
      !/^R\d{9}(?:-[A-Z0-9]+)?$/.test(ref)
    )
      fail("unknown_order_reference");
    const data = {
      record_id: identifier(order.id),
      reference_field: field,
      order_reference: ref,
      version: instant(get("lastmodifieddate")),
      trandate: instant(get("trandate")).slice(0, 10),
      entity: identifier(get("entity")),
      subsidiary: identifier(get("subsidiary")),
      currency: identifier(get("currency")),
      orderstatus: get("orderstatus"),
    };
    if (typeof data.orderstatus !== "string") fail("unknown_order_state");
    if (inventoryMode) data.line_identity_mode = "inventory_units";
    BODY_MONEY.forEach((key) => {
      data[key] = decimal(get(key));
    });
    if (profile) {
      data.tax_profile = profile;
      data.custbody_fw_solidus_tax_amount = decimal(
        get("custbody_fw_solidus_tax_amount"),
      );
      ["shippingtax1rate", "shippingtax2rate"].forEach((key) => {
        const value = get(key);
        data[key] =
          value === undefined || value === null || value === ""
            ? null
            : decimal(value);
      });
      if (profile.mode === "aggregate_header") {
        data.taxitem = identifier(get("taxitem"));
        data.taxrate = decimal(get("taxrate"));
        data.istaxable = get("istaxable");
        if (typeof data.istaxable !== "boolean") fail("unknown_taxability");
      }
    }
    const count = order.getLineCount({ sublistId: "item" });
    if (!Number.isInteger(count) || count < 1 || count > 500)
      fail("unsupported_line_count");
    const seen = new Set();
    const seenInventory = new Set();
    data.lines = Array.from({ length: count }, (_, line) => {
      const item = {},
        value = (fieldId) =>
          order.getSublistValue({ sublistId: "item", line, fieldId });
      LINE_IDS.forEach((key) => {
        if (inventoryMode && key === "custcol_fw_solidus_line_id") return;
        if (profile && profile.mode === "aggregate_header" && key === "taxcode")
          return;
        item[key] = identifier(value(key));
      });
      if (inventoryMode) {
        item.inventory_unit_ids = inventoryIds(
          value("custcol_fw_inventory_unit_ids"),
        );
        item.custcol_fw_original_ecom_sku = originalSku(
          value("custcol_fw_original_ecom_sku"),
        );
        item.inventory_unit_ids.forEach((id) => {
          if (seenInventory.has(id)) fail("ambiguous_inventory_identity");
          seenInventory.add(id);
        });
      }
      LINE_MONEY.forEach((key) => {
        if (
          profile &&
          profile.mode === "aggregate_header" &&
          key === "taxrate1"
        )
          return;
        item[key] = decimal(value(key));
      });
      if (profile) {
        if (profile.mode === "aggregate_header") {
          item.istaxable = value("istaxable");
          if (typeof item.istaxable !== "boolean") fail("unknown_taxability");
        }
        if (profile.mode === "line_tax_amount")
          item.tax1amt = decimal(value("tax1amt"));
      }
      item.isclosed = value("isclosed");
      if (typeof item.isclosed !== "boolean" || seen.has(item.line))
        fail("incomplete_lines");
      seen.add(item.line);
      return item;
    });
    data.period_id = openPeriod(data.trandate);
    return data;
  }
  function same(left, right) {
    if (Array.isArray(left))
      return (
        Array.isArray(right) &&
        left.length === right.length &&
        left.every((value, index) => same(value, right[index]))
      );
    if (object(left))
      return (
        object(right) &&
        Object.keys(left).length === Object.keys(right).length &&
        Object.keys(left).every(
          (key) =>
            Object.prototype.hasOwnProperty.call(right, key) &&
            same(left[key], right[key]),
        )
      );
    return left === right;
  }
  function safeState(order, current) {
    if (
      current.orderstatus !== "B" ||
      current.handlingcost !== "0" ||
      current.discounttotal !== "0"
    )
      fail("unsupported_order_state");
    if (
      current.lines.some(
        (line) =>
          line.isclosed ||
          line.quantityfulfilled !== "0" ||
          line.quantitybilled !== "0",
      )
    )
      fail("fulfilled_or_billed");
    // Existing email/payment flags can cause a second external side effect on save.
    if (EFFECT_FLAGS.some((fieldId) => order.getValue({ fieldId }) !== false))
      fail("record_side_effect_enabled");
    if (current.tax_profile) {
      if (current.tax_profile.mode === "aggregate_header") {
        if (current.lines.some((line) => line.istaxable !== true))
          fail("unknown_taxability");
        if (
          current.istaxable !== true ||
          current.taxitem !== current.tax_profile.tax_code_id
        )
          fail("legacy_tax_code_changed");
      } else if (
        current.lines.some(
          (line) => line.taxcode !== current.tax_profile.tax_code_id,
        )
      ) {
        fail("legacy_tax_code_changed");
      }
    }
  }
  function responseError(error, sent) {
    const code =
      error.guardCode ||
      (sent ? "save_outcome_unknown" : "provider_read_failed");
    log.error({ title: "Transaction guard", details: code });
    return {
      success: false,
      schema_version: VERSION,
      status: sent ? "unknown" : "rejected",
      code,
      verified: false,
      remainingUsage: usage(),
    };
  }
  function get(input) {
    try {
      budget(100);
      keys(
        input,
        [
          "action",
          "record_id",
          "reference_field",
          "tax_mode",
          "tax_code_id",
          "line_identity_mode",
        ],
        ["action", "record_id", "reference_field"],
      );
      if (input.action !== "snapshot") fail("unsupported_action");
      const order = record.load({
        type: "salesorder",
        id: identifier(input.record_id),
        isDynamic: false,
      });
      const profile =
        input.tax_mode !== undefined || input.tax_code_id !== undefined
          ? taxProfile({ mode: input.tax_mode, tax_code_id: input.tax_code_id })
          : null;
      const data = snapshot(
        order,
        referenceField(input.reference_field),
        profile,
        lineIdentityMode(input.line_identity_mode),
      );
      return {
        success: true,
        schema_version: VERSION,
        account_id: runtime.accountId,
        actions_enabled:
          runtime
            .getCurrentScript()
            .getParameter({ name: "custscript_ecom_tx_ops_enabled" }) === true,
        snapshot: data,
        remainingUsage: usage(),
      };
    } catch (error) {
      return responseError(error, false);
    }
  }
  function post(input) {
    let sent = false;
    try {
      budget(200);
      if (
        runtime
          .getCurrentScript()
          .getParameter({ name: "custscript_ecom_tx_ops_enabled" }) !== true
      )
        fail("guard_disabled");
      if (JSON.stringify(input).length > 65536) fail("intent_too_large");
      keys(input, [
        "schema_version",
        "action",
        "account_id",
        "work_key",
        "approval_expires_at",
        "before",
        "after",
      ]);
      if (
        input.schema_version !== VERSION ||
        input.action !== "correct_amounts"
      )
        fail("unsupported_action");
      if (account(input.account_id) !== account(runtime.accountId))
        fail("account_mismatch");
      if (
        typeof input.work_key !== "string" ||
        !/^[a-f0-9]{64}$/.test(input.work_key)
      )
        fail("invalid_work_key");
      expiration(input.approval_expires_at);
      if (!object(input.before)) fail("invalid_intent");
      const order = record.load({
        type: "salesorder",
        id: identifier(input.before.record_id),
        isDynamic: false,
      });
      const current = snapshot(
        order,
        referenceField(input.before.reference_field),
        taxProfile(input.before.tax_profile),
        lineIdentityMode(input.before.line_identity_mode),
      );
      if (!same(current, input.before)) fail("evidence_changed");
      safeState(order, current);
      keys(input.after, ["body_changes", "line_changes", "expected_totals"]);
      const bodyWrites = current.tax_profile
        ? [
            ...BODY_WRITES,
            "custbody_fw_solidus_tax_amount",
            ...(current.tax_profile.mode === "aggregate_header"
              ? ["taxrate"]
              : []),
          ]
        : BODY_WRITES;
      const lineWrites = current.tax_profile
        ? [
            "rate",
            "amount",
            "custcol_fw_vat_amount",
            ...(current.tax_profile.mode === "line_tax_amount"
              ? ["tax1amt"]
              : []),
          ]
        : LINE_WRITES;
      keys(input.after.body_changes, bodyWrites, []);
      keys(input.after.expected_totals, TOTALS);
      Object.values(input.after.expected_totals).forEach(writeNumber);
      if (
        !Array.isArray(input.after.line_changes) ||
        input.after.line_changes.length > current.lines.length
      )
        fail("invalid_line_changes");
      const seen = new Set(),
        changes = [];
      let changed = false;
      for (const change of input.after.line_changes) {
        keys(change, ["line", "fields"]);
        keys(change.fields, lineWrites, []);
        const lineId = identifier(change.line),
          index = current.lines.findIndex((line) => line.line === lineId);
        if (
          index < 0 ||
          seen.has(lineId) ||
          Object.keys(change.fields).length === 0
        )
          fail("invalid_line_changes");
        seen.add(lineId);
        for (const [fieldId, value] of Object.entries(change.fields)) {
          const numeric = writeNumber(value);
          if (fieldId === "taxrate1" && numeric > 1000)
            fail("invalid_tax_rate");
          changed = changed || decimal(value) !== current.lines[index][fieldId];
          changes.push({
            sublistId: "item",
            line: index,
            fieldId,
            value: numeric,
          });
        }
      }
      const body = Object.entries(input.after.body_changes).map(
        ([fieldId, value]) => {
          changed = changed || decimal(value) !== current[fieldId];
          return { fieldId, value: writeNumber(value) };
        },
      );
      if (!changed) fail("no_change");
      checkArithmetic(current, input.after);
      // Validate the complete intent before staging any field changes.
      changes.forEach((change) => order.setSublistValue(change));
      body.forEach((change) => order.setValue(change));
      budget(100);
      expiration(input.approval_expires_at);
      if (openPeriod(current.trandate) !== current.period_id)
        fail("period_changed");
      sent = true;
      const id = identifier(
        order.save({ enableSourcing: false, ignoreMandatoryFields: false }),
      );
      log.audit({
        title: "Transaction guard save",
        details: { work_key: input.work_key, record_id: id },
      });
      const fresh = record.load({ type: "salesorder", id, isDynamic: false });
      if (
        TOTALS.some(
          (fieldId) =>
            decimal(fresh.getValue({ fieldId })) !==
            decimal(input.after.expected_totals[fieldId]),
        )
      )
        fail("post_save_mismatch");
      if (current.tax_profile) {
        const actual = snapshot(
          fresh,
          current.reference_field,
          current.tax_profile,
          lineIdentityMode(current.line_identity_mode),
        );
        const expected = {
          ...current,
          ...input.after.body_changes,
          ...input.after.expected_totals,
          version: actual.version,
          lines: current.lines.map((line) => {
            const change = input.after.line_changes.find(
              (change) => String(change.line) === line.line,
            );
            return { ...line, ...(change ? change.fields : {}) };
          }),
        };
        if (!same(actual, expected)) fail("post_save_native_tax_mismatch");
      }
      // The platform independently re-reads source + NetSuite before it
      // calls the operation verified. A RESTlet receipt cannot do that.
      return {
        success: true,
        schema_version: VERSION,
        status: "saved",
        record_id: id,
        work_key: input.work_key,
        verified: false,
        remainingUsage: usage(),
      };
    } catch (error) {
      return responseError(error, sent);
    }
  }
  return { get, post };
});
