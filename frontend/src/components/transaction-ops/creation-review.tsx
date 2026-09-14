import { Fragment } from "react";
import { EvidenceJson } from "./evidence";
import { objectValue } from "./format";
import type { JsonObject } from "./types";

const text = (value: unknown) =>
  typeof value === "string" ||
  (typeof value === "number" && Number.isInteger(value))
    ? String(value)
    : "Not provided";
const rows = (value: unknown) =>
  Array.isArray(value) ? value.map(objectValue) : [];

export function CreationReview({ after }: { after: JsonObject }) {
  const input = objectValue(after.input),
    preview = objectValue(after.preview);
  const record = objectValue(preview.record),
    body = objectValue(record.body);
  const metadata = objectValue(preview.metadata),
    customer = objectValue(metadata.customer);
  const currency = objectValue(input.currency),
    totals = objectValue(input.expected_totals);
  const code = text(currency.symbol),
    lines = rows(input.lines),
    items = rows(metadata.items);
  return (
    <div className="min-w-0 max-w-full space-y-5 text-[13px]">
      <div className="rounded-lg border p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h4 className="font-semibold">Proposed order</h4>
            <p className="mt-2">
              {input.order_status === "A"
                ? "Pending approval"
                : text(input.order_status)}
            </p>
            {input.order_status === "A" && (
              <p className="text-muted-foreground">
                NetSuite approval remains required.
              </p>
            )}
          </div>
          <div>
            <p className="text-muted-foreground">Proposed order total</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">
              {code} {text(totals.total)}
            </p>
          </div>
        </div>
        <dl className="mt-4 grid gap-4 sm:grid-cols-3">
          <div>
            <dt className="text-muted-foreground">Transaction date</dt>
            <dd className="mt-1">{text(input.transaction_date)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Native exchange rate</dt>
            <dd className="mt-1 font-mono">{text(body.exchangerate)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">
              Native currency · decimal places
            </dt>
            <dd className="mt-1">
              {text(body.currency)} · {text(currency.precision)}
            </dd>
          </div>
        </dl>
      </div>
      <div>
        <h4 className="mb-3 font-semibold">Lines to create · {code}</h4>
        <div className="overflow-x-auto">
          <table className="w-full text-[13px] tabular-nums">
            <caption className="sr-only">
              Exact source and native quantities and transaction amounts
            </caption>
            <thead>
              <tr className="border-b text-right text-muted-foreground">
                {[
                  "Framework SKU",
                  "NetSuite SKU",
                  "Source quantity",
                  "Multiplier",
                  "Native quantity",
                  "Unit rate",
                  "Net",
                  "VAT",
                ].map((label, index) => (
                  <th
                    key={label}
                    className={`p-3 font-medium whitespace-nowrap ${index < 2 ? "text-left" : "text-right"}`}
                  >
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {lines.map((line, index) => {
                const matches = items.filter(
                  (item) => item.sku === line.netsuite_sku,
                );
                const item = matches.length === 1 ? matches[0] : {};
                return (
                  <Fragment key={`${text(line.source_line_id)}-${index}`}>
                    <tr>
                      <td className="p-3 align-top">
                        <p className="break-all">{text(line.source_sku)}</p>
                        <p className="text-xs text-muted-foreground">
                          Source line {text(line.source_line_id)}
                        </p>
                      </td>
                      <td className="p-3 align-top">
                        <p className="break-all">{text(line.netsuite_sku)}</p>
                        <p className="text-xs text-muted-foreground">
                          Item {text(item.id)} · Units {text(item.units)}
                        </p>
                      </td>
                      {[
                        line.source_quantity,
                        line.quantity_multiplier,
                        line.quantity,
                        line.rate,
                        line.amount,
                        line.tax_amount,
                      ].map((value, i) => (
                        <td
                          key={i}
                          className="p-3 text-right align-top font-mono whitespace-nowrap"
                        >
                          {i === 1 ? "×" : ""}
                          {text(value)}
                        </td>
                      ))}
                    </tr>
                    <tr className="border-b">
                      <td
                        colSpan={8}
                        className="px-3 pb-3 text-xs text-muted-foreground"
                      >
                        <p className="break-words">
                          Location {text(line.location_id)} · Inventory-owning
                          subsidiary {text(line.inventory_subsidiary_id)} · Tax
                          code {text(line.tax_code_id)}
                        </p>
                        <p className="mt-1 break-all">
                          Inventory IDs:{" "}
                          {Array.isArray(line.inventory_unit_ids)
                            ? line.inventory_unit_ids.map(text).join(", ")
                            : "Not provided"}
                        </p>
                        {line.source_parent_id !== null && (
                          <p className="mt-1">
                            Source parent line {text(line.source_parent_id)}
                          </p>
                        )}
                      </td>
                    </tr>
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-muted-foreground sm:hidden">
          Scroll the table to view all exact line values.
        </p>
        <dl className="mt-4 grid gap-4 sm:grid-cols-3">
          {[
            ["Subtotal", totals.subtotal],
            ["VAT", totals.taxtotal],
            ["Shipping", totals.shippingcost],
          ].map(([label, amount]) => (
            <div key={text(label)}>
              <dt className="text-muted-foreground">{text(label)}</dt>
              <dd className="mt-1 font-mono">
                {code} {text(amount)}
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="rounded-lg border p-4">
        <h4 className="font-semibold">Customer and delivery</h4>
        <p className="mt-2 break-all text-muted-foreground">
          {text(input.customer_email)} · Native customer {text(customer.id)}
        </p>
        <div className="mt-4 grid gap-5 sm:grid-cols-2">
          {[
            ["Bill to", "billing_address"],
            ["Ship to", "shipping_address"],
          ].map(([label, key]) => {
            const address = objectValue(input[key]);
            return (
              <div key={key}>
                <h5 className="mb-2 font-medium">{label}</h5>
                {[
                  ["addressee"],
                  ["attention"],
                  ["addr1"],
                  ["addr2"],
                  ["city", "state", "zip"],
                  ["country"],
                  ["addrphone"],
                ].map((fields, i) => {
                  const content = fields
                    .map((field) =>
                      address[field] === "" ? "" : text(address[field]),
                    )
                    .filter(Boolean)
                    .join(" ");
                  return content ? (
                    <p key={i} className="break-words">
                      {content}
                    </p>
                  ) : null;
                })}
              </div>
            );
          })}
        </div>
        <p className="mt-4 text-muted-foreground">
          Shipping method {text(input.shipping_method_id)} · Custom form{" "}
          {text(body.customform)} · Terms{" "}
          {body.terms === null ? "none" : text(body.terms)}
        </p>
      </div>
      <p className="text-muted-foreground">
        Approval permits one creation attempt after the source and native draft
        are checked again. Independent reads must establish the approved outcome
        before it is marked verified.
      </p>
      <EvidenceJson value={after} label="Full native creation details" />
    </div>
  );
}
