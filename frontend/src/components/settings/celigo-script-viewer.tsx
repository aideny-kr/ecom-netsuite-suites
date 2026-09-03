"use client";

/**
 * Task 10 — script viewer (mockup screen 04), built against Task 8's
 * `GET /celigo/scripts/{id}` (`CeligoScriptOut`,
 * backend/app/api/v1/celigo_flows.py).
 *
 * Task 17 — the presentational half (`CeligoScriptViewerBody`) was extracted
 * so the flow page's script DRAWER (`celigo/celigo-script-drawer.tsx`,
 * mockup screen 4, a right panel over the inspector reached via
 * `&script=<scriptId>`) could reuse the exact same header/pills/table/code/
 * banner instead of re-implementing them.
 *
 * Task 18 — `CeligoScriptViewerDialog` (the centered-dialog wrapper this
 * file used to also export) is deleted along with its last caller,
 * `celigo-flow-map.tsx`: `CeligoScriptViewerBody` below is now this file's
 * only export, reached exclusively through `CeligoScriptDrawer`.
 *
 * ═══ SECURITY CONTRACT ═══
 * `content` is arbitrary third-party JavaScript, written by whoever
 * configured the tenant's Celigo account -- untrusted input, never
 * instructions. It is rendered as inert TEXT via `react-syntax-highlighter`
 * (same pattern `components/workspace/code-viewer.tsx` already uses --
 * content passed as `children`, the library only tokenizes it for display).
 * Never `dangerouslySetInnerHTML`, never `eval`, never `new Function`, never
 * injected into an attribute/href/style, never logged to the console.
 *
 * ═══ N2 (standing decision) ═══
 * The banner below is the exact, project-wide string Global Constraints
 * mandates -- verbatim, not paraphrased. It is now IMPORTED from
 * `components/celigo/shared.tsx` (`N2_SHIELD_TEXT`), the single definition
 * this file and `celigo-step-inspector.tsx`'s Scripts tab both render. The
 * two used to hold hand-kept copies of the literal, on the reasoning that a
 * duplicate is safer than an import that outlives its usefulness -- but two
 * copies of a mandated string is the shape that drifts, and an edit to one
 * would leave the other promising something different about the same
 * content, with nothing to catch it.
 *
 * ═══ THE CORRECTION THAT MATTERS MOST (mockup-spec.md's correction section,
 * which overrides the Screen 04 body) ═══
 * The mockup's attachment table ends with an unconditional collapsing row --
 * "+ 18 further copies, identical source". That copy is only sometimes
 * true: `content_diverged` exists precisely because a clone's content CAN
 * legitimately differ from its original (dedup groups on
 * `COALESCE(source_id, celigo_id)` LINEAGE, not on content equality). Below,
 * the collapse row branches on `content_diverged`, and `content`/
 * `content_hash`/`name` are always rendered as THIS row's own values, never
 * a claimed "canonical" one for the group.
 *
 * `json_path` is an OPAQUE LOCATOR STRING (part of a DB unique key) --
 * rendered verbatim below, never parsed. `site_type` is best-effort
 * (fragile path-segment matching), so the "Where" column shows `json_path`,
 * not `site_type`.
 *
 * That locator comes in two forms, and BOTH must render as-is. A ref found
 * on the flow object is flow-relative (`routers[0].script`); a ref found on
 * an export/import the flow only references by id is prefixed with that
 * object's 24-char Celigo id (`6813b3ce...transform.script`) so two steps in
 * one flow cannot collide on the same path. The prefixed form is the longer
 * one and the reason the cell wraps rather than overflowing -- see the
 * `break-all` below. Still opaque: do not split on the dot to "clean it up",
 * the id is what makes the row identifiable.
 */

import type { CeligoScript, CeligoScriptAttachmentSite } from "@/hooks/use-celigo-flows";
import { N2_SHIELD_TEXT } from "@/components/celigo/shared";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ShieldAlert } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

/** `x ?? fallback` does not catch `""` -- the exact bug shape that shipped
 * for the Celigo card's `account_name` earlier this session. Guard both. */
function displayOr(value: string | null | undefined, fallback = "—"): string {
  return value ? value : fallback;
}

/** `flow_step_role` is null for a router-level script ref with no owning
 * step -- see `CeligoFlowDetailOut.unassigned_attachments`'s docstring.
 *
 * `role` only separates the flow's one GENERATOR (its source) from every
 * processor, and a processor is either a lookup or a destination — so
 * calling every processor a "Destination" announced the wrong direction of
 * data for every lookup site. An `*Export` adaptor on a processor is Celigo's
 * own marker for a lookup (`NetSuiteDistributedExport`, `HTTPExport`), the
 * same signal `celigo-step-inspector.tsx` and the canvas bubble read. */
function siteLocationLabel(site: CeligoScriptAttachmentSite): string {
  if (!site.flow_step_role) return "Router";
  const kind =
    site.flow_step_role === "generator"
      ? "Source"
      : site.flow_step_adaptor_type?.endsWith("Export")
        ? "Lookup"
        : "Destination";
  return `${kind} · ${displayOr(site.flow_step_adaptor_type, "Unknown adaptor")}`;
}

/** Groups attachment sites by `script_celigo_id` -- the clone/copy identity.
 * Two sites sharing a `script_celigo_id` are two REAL places the exact same
 * script object is wired in (e.g. `transform.script` AND
 * `hooks.preSavePage`) -- both must render as explicit rows. Two sites
 * under DIFFERENT `script_celigo_id`s belong to separate CLONES in the same
 * dedup family -- that's what the mockup's "+N further copies" row
 * summarizes. `CeligoScriptOut` exposes no `celigo_id` field for the row
 * the caller navigated to, so there is no way to single out "this one's own"
 * clone; the first group shown is simply the backend's own return order
 * (sorted by flow name, then `json_path`) unless `currentStepId` (below)
 * names a specific site to prefer instead. */
function groupSitesByCopy(usedBy: CeligoScriptAttachmentSite[]): CeligoScriptAttachmentSite[][] {
  const order: string[] = [];
  const groups = new Map<string, CeligoScriptAttachmentSite[]>();
  for (const site of usedBy) {
    if (!groups.has(site.script_celigo_id)) {
      groups.set(site.script_celigo_id, []);
      order.push(site.script_celigo_id);
    }
    groups.get(site.script_celigo_id)!.push(site);
  }
  return order.map((key) => groups.get(key)!);
}

/** `(chars/1024).toFixed(1)` KB off the script's OWN `content` -- there is
 * no server-sent size field on `CeligoScriptOut` (unlike
 * `CeligoAttachmentOut.script_size_chars`, which `celigo-step-inspector.tsx`
 * reads for the SAME fact at the chip/card level), so this is the only size
 * this view can honestly show. `null` content (not synced) returns `null`
 * so the caller omits the fact rather than printing a fabricated "0.0 KB". */
function formatContentSize(content: string | null): string | null {
  if (!content) return null;
  return `${(content.length / 1024).toFixed(1)} KB`;
}

/** Global Constraints' N2 copy, verbatim -- "shown to you only. Never run
 * here, never sent to the assistant." replaces this file's earlier line,
 * which (wrongly) promised the source would be quoted TO the assistant
 * inside a sealed block. It never is: N2 keeps script content off every
 * chat/tool path entirely, so the banner must not imply otherwise. */
function UntrustedContentBanner() {
  return (
    <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
      <ShieldAlert className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" aria-hidden />
      <p className="text-[12px] text-muted-foreground">{N2_SHIELD_TEXT}</p>
    </div>
  );
}

/**
 * Task 17 -- the re-homed body: header (hook chip + name), the copies/
 * sites-and-flows pills + size, the "Scripts view" affordance, the used-by
 * table, the code panel, and the N2 banner. Presentational only -- loading
 * and error states are the caller's own job (`CeligoScriptDrawer`), since it
 * already gates on its own `useCeligoScript` + `queryState()` before ever
 * reaching this component.
 *
 * `currentStepId`, when given, is the `flow_step_id` of the step the caller
 * actually opened this script FROM. It picks out that one used_by site so
 * the header names THAT site's function (`HK preMap`, not an arbitrary
 * first one) and so the copy group shown explicitly in the table -- the
 * mockup's "this copy: N sites · M flows" -- is the SAME clone attached at
 * that site, not whichever clone the backend happened to return first.
 * Omitted (e.g. a future Scripts-view context with no owning step) falls
 * back to the first used_by entry / first copy group, exactly this
 * component's pre-Task-17 behavior.
 */
export function CeligoScriptViewerBody({
  script,
  currentStepId,
}: {
  script: CeligoScript;
  currentStepId?: string | null;
}): JSX.Element {
  const copyGroups = groupSitesByCopy(script.used_by);
  const currentSite =
    (currentStepId ? script.used_by.find((s) => s.flow_step_id === currentStepId) : undefined) ??
    script.used_by[0];
  const shownSites =
    copyGroups.find((g) => g[0]?.script_celigo_id === currentSite?.script_celigo_id) ?? copyGroups[0] ?? [];
  const remainingCopies = Math.max(script.copies_count - 1, 0);

  const headerFunctionName = displayOr(currentSite?.function_name, "hook");
  const flowCount = new Set(shownSites.map((s) => s.flow_id)).size;
  const sizeLabel = formatContentSize(script.content);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            variant="outline"
            className="border-blue-500/50 bg-blue-500/10 text-[11px] text-blue-700 dark:text-blue-400"
          >
            {`HK ${headerFunctionName}`}
          </Badge>
          <span className="font-mono text-[13px] font-semibold">{script.name}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            variant="outline"
            className="text-[11px] border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
          >
            {`${script.copies_count} copies${script.content_diverged ? " · diverged" : ""}`}
          </Badge>
          <Badge variant="outline" className="border-border bg-muted text-[11px] text-muted-foreground">
            {/* Each half pluralises on its OWN count: a single-site script
                read "1 sites · 1 flows". */}
            {`${shownSites.length} site${shownSites.length === 1 ? "" : "s"} · ${flowCount} flow${flowCount === 1 ? "" : "s"}`}
          </Badge>
          {sizeLabel && <span className="text-[11px] tabular-nums text-muted-foreground">{sizeLabel}</span>}
        </div>
      </div>

      {/* "Scripts view" -- the full clone family (every site, every copy,
          the diff) lives in the integration page's Scripts tab
          (`celigo-integration-page.tsx`'s `ScriptsTab`), which today only
          says "the Scripts view ships separately" -- there is no built,
          navigable destination for this link yet (no task in this plan
          builds one; out of scope per the plan's Deferred table). Rendered
          as inert text, not a button or anchor, so this surface never
          implies an affordance it can't back. */}
      <div className="flex items-center justify-end border-b pb-2">
        <span className="text-[12px] font-medium text-muted-foreground">Scripts view ↗</span>
      </div>

      {script.used_by.length === 0 ? (
        // Genuinely-empty, distinct from the isError branch the caller
        // handles above -- a script that synced with no recorded
        // attachment sites is a real (if unusual) state, not a failed
        // request.
        <p className="py-2 text-[12px] text-muted-foreground">
          No attachment sites recorded for this script.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="h-8 text-[11px]">Attached to</TableHead>
              <TableHead className="h-8 text-[11px]">Where</TableHead>
              <TableHead className="h-8 text-[11px]">Function</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shownSites.map((site, i) => (
              <TableRow key={`${site.script_celigo_id}-${site.json_path}-${i}`}>
                <TableCell className="py-1.5">
                  <p className="text-[13px]">{site.flow_name}</p>
                  <p className="text-[11px] text-muted-foreground">{siteLocationLabel(site)}</p>
                </TableCell>
                <TableCell className="py-1.5 font-mono text-[12px] break-all">{site.json_path}</TableCell>
                <TableCell className="py-1.5 font-mono text-[12px]">
                  {displayOr(site.function_name)}
                </TableCell>
              </TableRow>
            ))}
            {remainingCopies > 0 && (
              <TableRow>
                <TableCell colSpan={3} className="py-1.5 text-[12px] text-muted-foreground">
                  {script.content_diverged
                    ? `+ ${remainingCopies} further copies — content differs across copies; the source below is only this copy's own version, not a canonical one for the group.`
                    : `+ ${remainingCopies} further copies, identical source`}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}

      <div className="space-y-2">
        {script.content_diverged && (
          <p className="text-[12px] text-amber-600">
            This script&apos;s copies have diverged — the source below is this specific copy&apos;s
            own version, not a single canonical version for the group.
          </p>
        )}
        <div className="overflow-hidden rounded-lg border">
          <SyntaxHighlighter
            language="javascript"
            style={oneDark}
            showLineNumbers
            wrapLongLines
            customStyle={{ margin: 0, padding: "1rem", fontSize: "12px", maxHeight: "320px" }}
          >
            {script.content ? script.content : "// No source recorded for this script."}
          </SyntaxHighlighter>
        </div>
      </div>

      <UntrustedContentBanner />
    </div>
  );
}
