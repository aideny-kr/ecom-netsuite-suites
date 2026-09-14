/**
 * Task 16 — `KeyValueOrJson`, `FilterPanel`, and `FieldMappingPanel`, copied
 * out of `components/settings/celigo-flow-map.tsx` (Task 9) into their own
 * file so the new step inspector (`celigo-step-inspector.tsx`) can use them
 * without importing a module Task 18 deletes. A genuine COPY, not a move —
 * the old file (and its test) keep their own private originals verbatim
 * until Task 18 retires them, same "reproduce, don't import a doomed
 * dependency" reasoning `shared.tsx`'s top docstring already applies to
 * `formatRelativeTime`/`ErrorNotice`.
 *
 * `FieldMappingPanel` carries the one real change this task asks for: the
 * heading now reads "Response mapping · N fields" (this data IS the
 * response a lookup/destination step read back from Celigo on its last
 * sync — the old plain "Field mapping" heading overclaimed what it was),
 * plus a permanent muted line naming the gap the plan's Deferred table
 * states outright: the import's own NetSuite field mapping never comes
 * through the sync, so this panel can only ever show the response side.
 * Both changes fire whenever the panel renders at all (i.e. whenever
 * `mapping_json` is truthy) — the panel's early return on no mapping is
 * itself unchanged.
 */

import type { CeligoFlowStep } from "@/hooks/use-celigo-flows";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

function isFlatScalarObject(value: unknown): value is Record<string, string | number | boolean | null> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((v) => v === null || typeof v !== "object")
  );
}

/** `mapping_json`/`responseMapping`'s CONFIRMED live shape (sanitizer.py):
 * `{fields: [{extract, generate}]}` -- `generate` is the destination
 * NetSuite field name, `extract` the source expression/value. */
function hasMappingFieldsShape(
  value: Record<string, unknown>,
): value is { fields: Array<{ extract?: unknown; generate?: unknown }> } {
  return Array.isArray(value.fields);
}

export function KeyValueOrJson({ value }: { value: unknown }) {
  if (isFlatScalarObject(value)) {
    return (
      <Table>
        <TableBody>
          {Object.entries(value).map(([k, v]) => (
            <TableRow key={k}>
              <TableCell className="py-1.5 font-mono text-[12px] text-muted-foreground">{k}</TableCell>
              <TableCell className="py-1.5 font-mono text-[12px]">{v === null ? "—" : String(v)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }
  // Nested/array shape (e.g. filter's `expression.rules`) -- read the real
  // structure rather than mis-flattening it into rows that imply a meaning
  // it doesn't have.
  return (
    <pre className="max-h-48 overflow-auto rounded-lg border bg-muted/30 p-2 text-[11px] font-mono whitespace-pre-wrap break-words">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function FilterPanel({ step }: { step: CeligoFlowStep }) {
  if (!step.filter_json) return null;
  return (
    <div className="rounded-lg border p-3">
      <p className="text-[13px] font-medium">Filter</p>
      <p className="mt-0.5 text-[12px] text-muted-foreground">
        Determines which records this step processes — the reason a record can go through unmatched.
      </p>
      <div className="mt-2">
        <KeyValueOrJson value={step.filter_json} />
      </div>
    </div>
  );
}

export function FieldMappingPanel({ step }: { step: CeligoFlowStep }) {
  if (!step.mapping_json) return null;
  const mapping = step.mapping_json;
  const mappingHasFieldsShape =
    typeof mapping === "object" && mapping !== null && !Array.isArray(mapping) && hasMappingFieldsShape(mapping);
  const fieldCount = mappingHasFieldsShape ? mapping.fields.length : null;
  return (
    <div className="rounded-lg border p-3">
      <p className="text-[13px] font-medium">
        {fieldCount === null ? "Response mapping" : `Response mapping · ${fieldCount} field${fieldCount === 1 ? "" : "s"}`}
      </p>
      <p className="mt-0.5 text-[12px] text-muted-foreground">NetSuite field mapping · not synced</p>
      <div className="mt-2">
        {mappingHasFieldsShape ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="h-8 text-[11px]">NetSuite field</TableHead>
                <TableHead className="h-8 text-[11px]">Value</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {mapping.fields.map((f, i) => (
                <TableRow key={i}>
                  <TableCell className="py-1.5 font-mono text-[12px] text-muted-foreground">
                    {f.generate == null ? "—" : String(f.generate)}
                  </TableCell>
                  <TableCell className="py-1.5 font-mono text-[12px]">
                    {f.extract == null ? "—" : String(f.extract)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <KeyValueOrJson value={mapping} />
        )}
      </div>
    </div>
  );
}
