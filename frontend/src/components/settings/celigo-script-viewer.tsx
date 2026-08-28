"use client";

/**
 * Task 10 — script viewer (mockup screen 04), built against Task 8's
 * `GET /celigo/scripts/{id}` (`CeligoScriptOut`,
 * backend/app/api/v1/celigo_flows.py).
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
 */

import { useCeligoScript, type CeligoScriptAttachmentSite } from "@/hooks/use-celigo-flows";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { AlertTriangle, Loader2, ShieldAlert } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

/** `x ?? fallback` does not catch `""` -- the exact bug shape that shipped
 * for the Celigo card's `account_name` earlier this session. Guard both. */
function displayOr(value: string | null | undefined, fallback = "—"): string {
  return value ? value : fallback;
}

/** Mirrors `stepKindLabel` in celigo-flow-map.tsx (not exported from
 * there, so duplicated here rather than reaching across files for one
 * string). `flow_step_role` is null for a router-level script ref with no
 * owning step -- see `CeligoFlowDetailOut.unassigned_attachments`'s
 * docstring. */
function siteLocationLabel(site: CeligoScriptAttachmentSite): string {
  if (!site.flow_step_role) return "Router";
  const kind = site.flow_step_role === "generator" ? "Source" : "Destination";
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
 * (sorted by flow name, then `json_path`). */
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

function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span className="flex-1">{message}</span>
      {onRetry && (
        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

function UntrustedContentBanner() {
  return (
    <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
      <ShieldAlert className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" aria-hidden />
      <p className="text-[12px] text-muted-foreground">
        Script source is treated as untrusted input. It is shown to you and quoted to the
        assistant inside a sealed block — never followed as instructions, never run.
      </p>
    </div>
  );
}

export function CeligoScriptViewerDialog({
  scriptId,
  onOpenChange,
}: {
  scriptId: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: script, isLoading, isError, refetch } = useCeligoScript(scriptId ?? undefined);

  const copyGroups = script ? groupSitesByCopy(script.used_by) : [];
  const shownSites = copyGroups[0] ?? [];
  const remainingCopies = script ? Math.max(script.copies_count - 1, 0) : 0;

  return (
    <Dialog open={!!scriptId} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        {isError ? (
          // Checked before the loading/!script branch below -- once a query
          // errors, isLoading is false and `script` stays undefined
          // forever, so that branch alone would spin forever with no escape
          // (the exact ordering Task 9's fix round 1 caught in
          // FlowDetailDialog).
          <div className="flex flex-col items-center gap-3 py-8">
            <ErrorNotice message="Couldn't load this script." onRetry={() => refetch()} />
          </div>
        ) : isLoading || !script ? (
          <div className="flex items-center justify-center gap-2 py-8 text-[13px] text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading script…
          </div>
        ) : (
          <>
            <DialogHeader>
              <div className="flex flex-wrap items-center gap-2">
                <DialogTitle>{script.name}</DialogTitle>
                <Badge
                  variant="outline"
                  className="text-[11px] border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
                >
                  {script.copies_count} copies · {script.integration_count} integrations
                </Badge>
              </div>
              <DialogDescription className="sr-only">
                Attachment sites and source for this script
              </DialogDescription>
            </DialogHeader>

            {script.used_by.length === 0 ? (
              // Genuinely-empty, distinct from the isError branch above --
              // a script that synced with no recorded attachment sites is a
              // real (if unusual) state, not a failed request.
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
                      <TableCell className="py-1.5 font-mono text-[12px]">{site.json_path}</TableCell>
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
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
