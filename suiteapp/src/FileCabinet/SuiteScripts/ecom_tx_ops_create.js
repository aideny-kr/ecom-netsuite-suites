/**
 * @NApiVersion 2.1
 * @NModuleScope SameAccount
 * Read-only native draft construction. The guard alone owns the final save.
 */
define(["N/record", "N/query"], (record, query) => {
  const ADDRESS = [
    "country",
    "state",
    "addressee",
    "attention",
    "addr1",
    "addr2",
    "city",
    "zip",
    "addrphone",
  ];
  const MONEY = [
    "subtotal",
    "taxtotal",
    "total",
    "shippingcost",
    "handlingcost",
    "discounttotal",
  ];
  const FLAGS = ["tobeemailed", "tobefaxed", "getauth", "paypalprocess"];
  const ITEM_TYPES = {
    InvtPart: "inventoryitem",
    Assembly: "assemblyitem",
    Service: "serviceitem",
  };
  function api(h) {
    const id = (value) => {
      const result = h.identifier(value);
      if (!/^[1-9][0-9]*$/.test(result)) h.fail("create_identity_unproven");
      return result;
    };
    const text = (value, empty = false) => {
      if (
        typeof value !== "string" ||
        value.length > 255 ||
        (!empty && !value) ||
        value.trim() !== value ||
        /[\x00-\x1f]/.test(value)
      )
        h.fail("create_text_unproven");
      return value;
    };
    const rows = (sql, params, limit = 2) => {
      h.budget(150);
      const result = query
        .runSuiteQL({
          query: sql + " FETCH FIRST " + limit + " ROWS ONLY",
          params,
        })
        .asMappedResults();
      if (!Array.isArray(result) || result.length >= limit)
        h.fail("create_metadata_ambiguous");
      return result;
    };
    const one = (sql, params) => {
      const values = rows(sql, params);
      if (values.length !== 1) h.fail("create_metadata_unavailable");
      return values[0];
    };
    const optionalId = (value) =>
      value === null || value === undefined || value === "" ? null : id(value);
    return { id, text, rows, one, optionalId };
  }
  function absent(reference, h) {
    const found = api(h).rows(
      "SELECT id FROM transaction WHERE LOWER(externalid)=LOWER(?) OR (type='SalesOrd' AND LOWER(tranid)=LOWER(?))",
      [reference, reference],
    );
    if (found.length) h.fail("order_already_exists");
  }
  function validate(input, h) {
    const { id, text } = api(h);
    h.keys(input, [
      "schema_version",
      "account_id",
      "subsidiary_id",
      "order_reference",
      "external_id",
      "order_status",
      "customer_email",
      "currency",
      "transaction_date",
      "billing_address",
      "shipping_address",
      "shipping_method_id",
      "inventory_mode",
      "tax_profile",
      "native_tax_rounding",
      "custom_form_id",
      "terms_id",
      "lines",
      "expected_totals",
    ]);
    if (
      input.schema_version !== 1 ||
      input.order_status !== "A" ||
      !/^R\d{9}(?:-[A-Z0-9]+)?$/.test(input.order_reference) ||
      input.external_id !== input.order_reference
    )
      h.fail("invalid_create_identity");
    id(input.subsidiary_id);
    id(input.shipping_method_id);
    text(input.customer_email);
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(input.customer_email))
      h.fail("create_customer_unproven");
    h.keys(input.currency, ["symbol", "precision"]);
    if (
      !/^[A-Z]{3}$/.test(input.currency.symbol) ||
      !Number.isInteger(input.currency.precision) ||
      input.currency.precision < 0 ||
      input.currency.precision > 6
    )
      h.fail("create_currency_unproven");
    if (!["line_location", "cross_subsidiary"].includes(input.inventory_mode))
      h.fail("create_inventory_scope_unproven");
    h.keys(input.tax_profile, [
      "schema_version",
      "mode",
      "account_id",
      "subsidiary_id",
      "tax_code_id",
    ]);
    if (
      input.tax_profile.schema_version !== 1 ||
      h.account(input.tax_profile.account_id) !== h.account(input.account_id) ||
      input.tax_profile.subsidiary_id !== input.subsidiary_id ||
      !["aggregate_header", "line_tax_amount"].includes(input.tax_profile.mode)
    )
      h.fail("create_tax_profile_unproven");
    id(input.tax_profile.tax_code_id);
    if (
      input.tax_profile.mode === "aggregate_header" &&
      !["half_up", "half_even"].includes(input.native_tax_rounding)
    )
      h.fail("create_tax_rounding_unproven");
    for (const field of ["custom_form_id", "terms_id"])
      if (input[field] !== null) id(input[field]);
    for (const field of ["billing_address", "shipping_address"]) {
      h.keys(input[field], ADDRESS);
      ADDRESS.forEach((key) =>
        text(
          input[field][key],
          !["country", "addressee", "addr1", "city"].includes(key),
        ),
      );
      if (!/^[A-Z]{2}$/.test(input[field].country))
        h.fail("address_country_unproven");
    }
    h.keys(input.expected_totals, MONEY);
    MONEY.forEach((key) => {
      h.writeNumber(input.expected_totals[key]);
      if (h.scaleOf(input.expected_totals[key]) > input.currency.precision)
        h.fail("create_currency_precision");
    });
    if (
      input.expected_totals.handlingcost !== "0" ||
      input.expected_totals.discounttotal !== "0" ||
      (input.tax_profile.mode === "aggregate_header" &&
        input.expected_totals.shippingcost !== "0")
    )
      h.fail("unsupported_create_amounts");
    if (
      !Array.isArray(input.lines) ||
      !input.lines.length ||
      input.lines.length > 100
    )
      h.fail("create_line_budget");
    const sources = new Set(),
      inventory = new Set();
    input.lines.forEach((line) => {
      h.keys(line, [
        "source_line_id",
        "source_parent_id",
        "source_sku",
        "netsuite_sku",
        "source_quantity",
        "quantity_multiplier",
        "quantity",
        "rate",
        "amount",
        "tax_amount",
        "tax_code_id",
        "inventory_unit_ids",
        "location_id",
        "inventory_subsidiary_id",
      ]);
      const sourceId = id(line.source_line_id);
      if (sources.has(sourceId)) h.fail("create_line_ambiguous");
      sources.add(sourceId);
      if (line.source_parent_id !== null) id(line.source_parent_id);
      text(line.source_sku);
      text(line.netsuite_sku);
      id(line.location_id);
      id(line.inventory_subsidiary_id);
      if (
        line.tax_code_id !== input.tax_profile.tax_code_id ||
        (input.inventory_mode === "line_location" &&
          line.inventory_subsidiary_id !== input.subsidiary_id)
      )
        h.fail("create_line_scope_unproven");
      for (const key of [
        "source_quantity",
        "quantity",
        "rate",
        "amount",
        "tax_amount",
      ])
        h.writeNumber(line[key]);
      if (
        !Number.isInteger(line.quantity_multiplier) ||
        line.quantity_multiplier < 1 ||
        line.quantity_multiplier > 1000 ||
        h.product(line.source_quantity, String(line.quantity_multiplier)) !==
          h.decimal(line.quantity) ||
        h.product(line.quantity, line.rate) !== h.decimal(line.amount) ||
        h.writeNumber(line.quantity) <= 0
      )
        h.fail("create_quantity_unproven");
      if (
        h.scaleOf(line.amount) > input.currency.precision ||
        h.scaleOf(line.tax_amount) > input.currency.precision
      )
        h.fail("create_currency_precision");
      if (
        !Array.isArray(line.inventory_unit_ids) ||
        line.inventory_unit_ids.length !== h.writeNumber(line.source_quantity)
      )
        h.fail("create_inventory_unproven");
      line.inventory_unit_ids.forEach((value) => {
        id(value);
        if (inventory.has(value)) h.fail("create_inventory_ambiguous");
        inventory.add(value);
      });
    });
    const totals = input.expected_totals;
    if (
      h.sum(input.lines.map((x) => x.amount)) !== totals.subtotal ||
      h.sum(input.lines.map((x) => x.tax_amount)) !== totals.taxtotal ||
      h.sum([totals.subtotal, totals.taxtotal, totals.shippingcost]) !==
        totals.total
    )
      h.fail("create_totals_inconsistent");
  }
  function metadata(input, h) {
    const { id, rows, one } = api(h);
    const customer = one(
      "SELECT id,email,subsidiary,currency,isinactive,creditholdoverride,creditlimit FROM customer WHERE LOWER(email)=LOWER(?) AND isinactive='F'",
      [input.customer_email],
    );
    if (
      String(customer.email).toLowerCase() !==
        input.customer_email.toLowerCase() ||
      customer.isinactive !== "F" ||
      !["AUTO", "OFF"].includes(customer.creditholdoverride) ||
      (customer.creditholdoverride === "AUTO" &&
        customer.creditlimit != null &&
        h.decimal(customer.creditlimit) !== "0")
    )
      h.fail("create_customer_unproven");
    customer.id = id(customer.id);
    customer.subsidiary = id(customer.subsidiary);
    if (customer.subsidiary !== input.subsidiary_id) {
      const relationship = one(
        "SELECT entity,subsidiary FROM customersubsidiaryrelationship WHERE entity=? AND subsidiary=?",
        [customer.id, input.subsidiary_id],
      );
      if (
        id(relationship.entity) !== customer.id ||
        id(relationship.subsidiary) !== input.subsidiary_id
      )
        h.fail("create_customer_scope_unproven");
    }
    const currency = one(
      "SELECT id,symbol,currencyprecision,isinactive FROM currency WHERE symbol=? AND isinactive='F'",
      [input.currency.symbol],
    );
    if (
      currency.symbol !== input.currency.symbol ||
      currency.isinactive !== "F" ||
      String(currency.currencyprecision) !== String(input.currency.precision)
    )
      h.fail("create_currency_unproven");
    currency.id = id(currency.id);
    h.budget(150);
    const nativeCustomer = record.load({
      type: "customer",
      id: customer.id,
      isDynamic: false,
    });
    const creditLimit = (value) =>
      value == null || value === "" ? null : h.decimal(value);
    if (
      String(nativeCustomer.id) !== customer.id ||
      nativeCustomer.getValue({ fieldId: "isinactive" }) !== false ||
      id(nativeCustomer.getValue({ fieldId: "subsidiary" })) !==
        customer.subsidiary ||
      String(nativeCustomer.getValue({ fieldId: "email" })).toLowerCase() !==
        input.customer_email.toLowerCase() ||
      nativeCustomer.getValue({ fieldId: "creditholdoverride" }) !==
        customer.creditholdoverride ||
      creditLimit(nativeCustomer.getValue({ fieldId: "creditlimit" })) !==
        creditLimit(customer.creditlimit)
    )
      h.fail("create_customer_changed");
    const currencyIds = new Set([id(customer.currency)]),
      count = nativeCustomer.getLineCount({ sublistId: "currency" });
    if (!Number.isInteger(count) || count < 0 || count > 100)
      h.fail("create_customer_currency_unproven");
    for (let line = 0; line < count; line++)
      currencyIds.add(
        id(
          nativeCustomer.getSublistValue({
            sublistId: "currency",
            fieldId: "currency",
            line,
          }),
        ),
      );
    if (!currencyIds.has(currency.id))
      h.fail("create_customer_currency_unproven");
    const skus = [...new Set(input.lines.map((x) => x.netsuite_sku))].sort();
    const items = rows(
      "SELECT id,itemid,isinactive,itemtype,unitstype,saleunit FROM item WHERE itemid IN (" +
        skus.map(() => "?").join(",") +
        ") AND isinactive='F'",
      skus,
      skus.length * 2 + 1,
    );
    const bySku = {};
    items.forEach((item) => {
      if (
        !skus.includes(item.itemid) ||
        bySku[item.itemid] ||
        !ITEM_TYPES[item.itemtype] ||
        item.isinactive !== "F"
      )
        h.fail("create_item_unproven");
      id(item.id);
      id(item.unitstype);
      id(item.saleunit);
      h.budget(150);
      const native = record.load({
        type: ITEM_TYPES[item.itemtype],
        id: item.id,
        isDynamic: false,
      });
      if (
        native.type !== ITEM_TYPES[item.itemtype] ||
        String(native.id) !== String(item.id) ||
        native.getValue({ fieldId: "isinactive" }) !== false ||
        native.getValue({ fieldId: "itemid" }) !== item.itemid ||
        id(native.getValue({ fieldId: "unitstype" })) !== id(item.unitstype) ||
        id(native.getValue({ fieldId: "saleunit" })) !== id(item.saleunit)
      )
        h.fail("create_item_changed");
      bySku[item.itemid] = item;
    });
    if (Object.keys(bySku).length !== skus.length)
      h.fail("create_item_unavailable");
    const unitTypes = [...new Set(items.map((x) => id(x.unitstype)))].sort();
    const units = rows(
      "SELECT internalid,unitstype,conversionrate,unitname FROM unitstypeuom WHERE unitstype IN (" +
        unitTypes.map(() => "?").join(",") +
        ")",
      unitTypes,
      501,
    );
    items.forEach((item) => {
      const matches = units.filter(
        (x) =>
          id(x.unitstype) === id(item.unitstype) &&
          id(x.internalid) === id(item.saleunit),
      );
      if (matches.length !== 1 || h.decimal(matches[0].conversionrate) !== "1")
        h.fail("create_quantity_units_unproven");
    });
    const locationIds = [
      ...new Set(input.lines.map((x) => x.location_id)),
    ].sort();
    const locations = rows(
      "SELECT id,name,subsidiary,isinactive FROM location WHERE id IN (" +
        locationIds.map(() => "?").join(",") +
        ")",
      locationIds,
      locationIds.length * 2 + 1,
    );
    input.lines.forEach((line) => {
      const matches = locations.filter((x) => id(x.id) === line.location_id);
      if (
        matches.length !== 1 ||
        matches[0].isinactive !== "F" ||
        id(matches[0].subsidiary) !== line.inventory_subsidiary_id
      )
        h.fail("create_location_unproven");
    });
    h.budget(150);
    const shipping = record.load({
      type: "shipitem",
      id: input.shipping_method_id,
      isDynamic: false,
    });
    if (
      shipping.type !== "shipitem" ||
      String(shipping.id) !== input.shipping_method_id ||
      shipping.getValue({ fieldId: "isinactive" }) !== false
    )
      h.fail("create_shipping_method_unproven");
    return {
      customer: {
        id: customer.id,
        primary_subsidiary: customer.subsidiary,
        currency_ids: [...currencyIds].sort(),
        credit_hold: customer.creditholdoverride,
        credit_limit:
          customer.creditlimit == null ? null : h.decimal(customer.creditlimit),
      },
      currency: {
        id: currency.id,
        symbol: currency.symbol,
        precision: input.currency.precision,
      },
      items: items
        .map((x) => ({
          id: id(x.id),
          sku: x.itemid,
          type: x.itemtype,
          unitstype: id(x.unitstype),
          units: id(x.saleunit),
        }))
        .sort((a, b) => a.id.localeCompare(b.id)),
      locations: locations
        .map((x) => ({ id: id(x.id), subsidiary: id(x.subsidiary) }))
        .sort((a, b) => a.id.localeCompare(b.id)),
    };
  }
  function calendar(value, h) {
    if (!(value instanceof Date) || !Number.isFinite(value.getTime()))
      h.fail("create_date_unproven");
    return [
      value.getFullYear(),
      String(value.getMonth() + 1).padStart(2, "0"),
      String(value.getDate()).padStart(2, "0"),
    ].join("-");
  }
  function snapshot(order, input, h, unsaved = false) {
    const { id, text, optionalId } = api(h),
      get = (fieldId) => order.getValue({ fieldId });
    const body = {};
    for (const key of [
      "entity",
      "subsidiary",
      "currency",
      "shipmethod",
      "customform",
    ])
      body[key] = id(get(key));
    body.terms = optionalId(get("terms"));
    body.discountitem = optionalId(get("discountitem"));
    if (body.discountitem !== null) h.fail("unsupported_create_discount");
    for (const key of [
      ...MONEY,
      "exchangerate",
      "custbody_fw_solidus_order_total",
      "custbody_fw_solidus_tax_amount",
    ])
      body[key] = h.decimal(get(key));
    body.tranid = text(get("tranid"));
    body.externalid = text(get("externalid"));
    body.trandate = calendar(get("trandate"), h);
    body.orderstatus = get("orderstatus");
    body.iscrosssubtransaction = get("iscrosssubtransaction");
    if (h.writeNumber(body.exchangerate) <= 0)
      h.fail("create_exchange_rate_unproven");
    FLAGS.forEach((key) => {
      body[key] = get(key);
      if (body[key] !== false) h.fail("create_side_effect_enabled");
    });
    if (
      body.orderstatus !== "A" ||
      body.iscrosssubtransaction !==
        (input.inventory_mode === "cross_subsidiary")
    )
      h.fail("create_order_state_changed");
    if (input.tax_profile.mode === "aggregate_header") {
      body.taxitem = id(get("taxitem"));
      body.taxrate = h.decimal(get("taxrate"));
      body.istaxable = get("istaxable");
    }
    const count = order.getLineCount({ sublistId: "item" });
    if (count !== input.lines.length) h.fail("create_line_membership_changed");
    const lines = Array.from({ length: count }, (_, line) => {
      const get = (fieldId) =>
          order.getSublistValue({ sublistId: "item", fieldId, line }),
        row = {};
      for (const key of ["item", "units"]) row[key] = id(get(key));
      for (const key of ["quantity", "rate", "amount", "custcol_fw_vat_amount"])
        row[key] = h.decimal(get(key));
      for (const key of ["quantityfulfilled", "quantitybilled"])
        row[key] =
          unsaved && (get(key) == null || get(key) === "")
            ? "0"
            : h.decimal(get(key));
      row.isclosed = get("isclosed");
      row.createwo = get("createwo");
      row.createpo = get("createpo") == null ? "" : get("createpo");
      row.price = String(get("price"));
      if (
        row.quantityfulfilled !== "0" ||
        row.quantitybilled !== "0" ||
        row.isclosed !== false ||
        row.createwo !== false ||
        row.createpo !== "" ||
        row.price !== "-1"
      )
        h.fail("create_line_state_changed");
      row.inventory_unit_ids = h.inventoryIds(
        get("custcol_fw_inventory_unit_ids"),
      );
      row.original_sku = text(get("custcol_fw_original_ecom_sku"));
      if (input.inventory_mode === "cross_subsidiary") {
        row.inventorylocation = id(get("inventorylocation"));
        row.inventorysubsidiary = id(get("inventorysubsidiary"));
      } else row.location = id(get("location"));
      if (input.tax_profile.mode === "line_tax_amount") {
        row.taxcode = id(get("taxcode"));
        row.taxrate1 = h.decimal(get("taxrate1"));
        row.tax1amt = h.decimal(get("tax1amt"));
      } else row.istaxable = get("istaxable");
      return row;
    });
    const addresses = {};
    for (const [name, field] of [
      ["billing_address", "billingaddress"],
      ["shipping_address", "shippingaddress"],
    ]) {
      const address = order.getSubrecord({ fieldId: field });
      addresses[name] = Object.fromEntries(
        ADDRESS.map((key) => [
          key,
          text(
            address.getValue({ fieldId: key }),
            !["country", "addressee", "addr1", "city"].includes(key),
          ),
        ]),
      );
    }
    return { body, lines, ...addresses };
  }
  function prepare(input, h) {
    validate(input, h);
    absent(input.order_reference, h);
    const meta = metadata(input, h),
      periodId = h.openPeriod(input.transaction_date);
    h.budget(150);
    const order = record.create({ type: "salesorder", isDynamic: true });
    const set = (fieldId, value) => order.setValue({ fieldId, value });
    if (input.custom_form_id !== null) set("customform", input.custom_form_id);
    set("entity", meta.customer.id);
    set("subsidiary", input.subsidiary_id);
    set("currency", meta.currency.id);
    const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(input.transaction_date);
    if (!parts) h.fail("create_date_unproven");
    const date = new Date(
      Number(parts[1]),
      Number(parts[2]) - 1,
      Number(parts[3]),
      12,
    );
    if (calendar(date, h) !== input.transaction_date)
      h.fail("create_date_unproven");
    set("trandate", date);
    set("tranid", input.order_reference);
    set("externalid", input.external_id);
    set("iscrosssubtransaction", input.inventory_mode === "cross_subsidiary");
    set("shipmethod", input.shipping_method_id);
    if (input.terms_id !== null) set("terms", input.terms_id);
    for (const key of FLAGS) set(key, false);
    set("shippingcost", h.writeNumber(input.expected_totals.shippingcost));
    set("handlingcost", 0);
    set(
      "custbody_fw_solidus_order_total",
      h.writeNumber(input.expected_totals.total),
    );
    set(
      "custbody_fw_solidus_tax_amount",
      h.writeNumber(input.expected_totals.taxtotal),
    );
    for (const [name, field] of [
      ["billing_address", "billingaddress"],
      ["shipping_address", "shippingaddress"],
    ]) {
      set(
        field === "billingaddress" ? "billaddresslist" : "shipaddresslist",
        null,
      );
      const address = order.getSubrecord({ fieldId: field });
      ADDRESS.forEach((fieldId) =>
        address.setValue({ fieldId, value: input[name][fieldId] }),
      );
    }
    if (input.tax_profile.mode === "aggregate_header") {
      set("taxitem", input.tax_profile.tax_code_id);
      set("istaxable", true);
      set(
        "taxrate",
        h.writeNumber(
          h.effectiveHeaderRate(
            input.expected_totals.subtotal,
            input.expected_totals.taxtotal,
          ),
        ),
      );
    }
    input.lines.forEach((line) => {
      const item = meta.items.find((x) => x.sku === line.netsuite_sku);
      h.budget(150);
      order.selectNewLine({ sublistId: "item" });
      const set = (fieldId, value) =>
        order.setCurrentSublistValue({ sublistId: "item", fieldId, value });
      set("item", item.id);
      set("units", item.units);
      set("price", -1);
      set("quantity", h.writeNumber(line.quantity));
      if (input.inventory_mode === "cross_subsidiary") {
        set("inventorysubsidiary", line.inventory_subsidiary_id);
        set("inventorylocation", line.location_id);
      } else set("location", line.location_id);
      set("createwo", false);
      set("createpo", "");
      set("isclosed", false);
      set("custcol_fw_inventory_unit_ids", line.inventory_unit_ids.join(","));
      set("custcol_fw_original_ecom_sku", line.source_sku);
      set("custcol_fw_vat_amount", h.writeNumber(line.tax_amount));
      if (input.tax_profile.mode === "line_tax_amount")
        set("taxcode", line.tax_code_id);
      else set("istaxable", true);
      set("rate", h.writeNumber(line.rate));
      set("amount", h.writeNumber(line.amount));
      if (input.tax_profile.mode === "line_tax_amount")
        set("tax1amt", h.writeNumber(line.tax_amount));
      order.commitLine({ sublistId: "item" });
    });
    set("orderstatus", "A");
    order.getFields().forEach((fieldId) => {
      const field = order.getField({ fieldId });
      const value = order.getValue({ fieldId });
      if (
        field &&
        field.isMandatory &&
        (value === null || value === undefined || value === "")
      )
        h.fail("create_mandatory_field_unavailable");
    });
    const projection = snapshot(order, input, h, true),
      body = projection.body;
    if (
      body.tranid !== input.order_reference ||
      body.externalid !== input.external_id ||
      body.entity !== meta.customer.id ||
      body.subsidiary !== input.subsidiary_id ||
      body.currency !== meta.currency.id ||
      body.shipmethod !== input.shipping_method_id ||
      (input.custom_form_id !== null &&
        body.customform !== input.custom_form_id) ||
      (input.terms_id !== null && body.terms !== input.terms_id) ||
      body.trandate !== input.transaction_date ||
      MONEY.some((key) => body[key] !== input.expected_totals[key]) ||
      body.custbody_fw_solidus_order_total !== input.expected_totals.total ||
      body.custbody_fw_solidus_tax_amount !== input.expected_totals.taxtotal ||
      (input.tax_profile.mode === "aggregate_header" &&
        (body.taxitem !== input.tax_profile.tax_code_id ||
          body.istaxable !== true ||
          body.taxrate !==
            h.effectiveHeaderRate(
              input.expected_totals.subtotal,
              input.expected_totals.taxtotal,
            ))) ||
      !h.same(projection.billing_address, input.billing_address) ||
      !h.same(projection.shipping_address, input.shipping_address)
    )
      h.fail("create_draft_mismatch");
    projection.lines.forEach((line, index) => {
      const wanted = input.lines[index],
        item = meta.items.find((x) => x.sku === wanted.netsuite_sku);
      if (
        line.item !== item.id ||
        line.units !== item.units ||
        line.quantity !== wanted.quantity ||
        line.rate !== wanted.rate ||
        line.amount !== wanted.amount ||
        line.custcol_fw_vat_amount !== wanted.tax_amount ||
        line.original_sku !== wanted.source_sku ||
        !h.same(
          line.inventory_unit_ids,
          [...wanted.inventory_unit_ids].sort(),
        ) ||
        (input.inventory_mode === "cross_subsidiary"
          ? line.inventorylocation !== wanted.location_id ||
            line.inventorysubsidiary !== wanted.inventory_subsidiary_id
          : line.location !== wanted.location_id) ||
        (input.tax_profile.mode === "line_tax_amount"
          ? line.taxcode !== wanted.tax_code_id ||
            line.tax1amt !== wanted.tax_amount
          : line.istaxable !== true)
      )
        h.fail("create_draft_line_mismatch");
    });
    return {
      order,
      preview: {
        schema_version: 1,
        metadata: meta,
        period_id: periodId,
        record: projection,
      },
    };
  }
  return { prepare, snapshot, absent };
});
