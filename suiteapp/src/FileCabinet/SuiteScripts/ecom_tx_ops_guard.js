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
  function snapshot(order, field) {
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
    BODY_MONEY.forEach((key) => {
      data[key] = decimal(get(key));
    });
    const count = order.getLineCount({ sublistId: "item" });
    if (!Number.isInteger(count) || count < 1 || count > 500)
      fail("unsupported_line_count");
    const seen = new Set();
    data.lines = Array.from({ length: count }, (_, line) => {
      const item = {},
        value = (fieldId) =>
          order.getSublistValue({ sublistId: "item", line, fieldId });
      LINE_IDS.forEach((key) => {
        item[key] = identifier(value(key));
      });
      LINE_MONEY.forEach((key) => {
        item[key] = decimal(value(key));
      });
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
      keys(input, ["action", "record_id", "reference_field"]);
      if (input.action !== "snapshot") fail("unsupported_action");
      const order = record.load({
        type: "salesorder",
        id: identifier(input.record_id),
        isDynamic: false,
      });
      const data = snapshot(order, referenceField(input.reference_field));
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
      );
      if (!same(current, input.before)) fail("evidence_changed");
      safeState(order, current);
      keys(input.after, ["body_changes", "line_changes", "expected_totals"]);
      keys(input.after.body_changes, BODY_WRITES, []);
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
        keys(change.fields, LINE_WRITES, []);
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
