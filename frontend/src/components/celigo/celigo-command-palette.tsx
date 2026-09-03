"use client";

/**
 * Task 11 — ⌘K command palette over every integration and flow (mockup:
 * "⌘K is a real palette over all 122 flows, each result carrying its health
 * dot and integration"; "the palette searches names only"). Data is entirely
 * off `useCeligoIntegrations()` — `flow_schedules` already lists every flow,
 * so this needs no extra request per integration.
 *
 * Opens on the window event `celigo:command-k`, dispatched by the workspace
 * page (Task 9) whenever the Celigo surface is active. Escape closes via
 * Radix Dialog's own default (`DismissableLayer`'s document-level listener) —
 * no palette-specific handling needed here.
 *
 * `Command.Dialog` is deliberately not used: `Command` renders inside the
 * app's own Radix `Dialog`/`DialogContent` so the palette gets the same
 * overlay/animation/focus-trap chrome as every other dialog on this surface,
 * rather than cmdk's own unstyled portal.
 *
 * Read-only, names only (N2): every row shows a `name` field straight off
 * the API — never a script's `content`, never a step, never anything that
 * would need the script viewer's inert-rendering path.
 */

import { useEffect, useMemo, useState } from "react";
import { Command } from "cmdk";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  useCeligoIntegrations,
  useCeligoSyncStatus,
  type CeligoIntegration,
} from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { stallState, type StallState } from "./schedule";
import { useCeligoRoute } from "./celigo-route";
import { cn } from "@/lib/utils";

/** Every dot the palette can show groups into one of three tones — the same
 * three-tone vocabulary `Pill`/`SchedulePill` use elsewhere on this surface,
 * just as a bare dot rather than a labelled pill (a palette row has no room
 * for "stalled? 2 runs missed" prose). `data-state` still carries the RAW
 * `StallState.state` value (never just the tone) so a caller — a test, a
 * future feature — can tell "paused" from "on_demand" from "unknown"
 * without re-deriving it from the tone. */
const DOT_TONE_CLASS: Record<"ok" | "warn" | "mute", string> = {
  ok: "bg-green-500",
  warn: "bg-amber-500",
  mute: "bg-muted-foreground/40",
};

function healthTone(state: StallState["state"]): "ok" | "warn" | "mute" {
  if (state === "on_time") return "ok";
  if (state === "stalled") return "warn";
  return "mute";
}

/** A stable reference for "no data yet" -- `data ?? []` would allocate a new
 * array every render, which would make the `flows` useMemo below recompute
 * on every render regardless of whether the data actually changed (same
 * pattern/reasoning as celigo-integrations-page.tsx's own constant of this
 * name). */
const NO_INTEGRATIONS: CeligoIntegration[] = [];

function HealthDot({ stall }: { stall: StallState }): JSX.Element {
  return (
    <span
      aria-hidden
      data-state={stall.state}
      className={cn("h-1.5 w-1.5 shrink-0 rounded-full", DOT_TONE_CLASS[healthTone(stall.state)])}
    />
  );
}

interface FlowResult {
  id: string;
  name: string;
  integrationId: string;
  integrationName: string;
  stall: StallState;
}

/** The palette's own item chrome — shared by both groups so a row looks the
 * same whether it's an integration or a flow. */
function ResultRow({ children }: { children: React.ReactNode }): JSX.Element {
  return <div className="flex min-w-0 flex-1 items-center gap-2">{children}</div>;
}

export function CeligoCommandPalette(): JSX.Element {
  const [open, setOpen] = useState(false);
  const integrationsQuery = useCeligoIntegrations();
  const syncStatusQuery = useCeligoSyncStatus();
  const route = useCeligoRoute();

  useEffect(() => {
    function onCommandK() {
      setOpen(true);
    }
    window.addEventListener("celigo:command-k", onCommandK);
    return () => window.removeEventListener("celigo:command-k", onCommandK);
  }, []);

  const integrationsState = queryState(integrationsQuery);
  const syncStatusState = queryState(syncStatusQuery);
  // A pending/errored sync-status query must never be read as "confirmed no
  // sync" (see celigo-integrations-page.tsx's own `lastSyncedAt` derivation)
  // — every flow's stall dot would otherwise silently mislabel "unknown" as
  // a settled fact.
  const lastSyncedAt = syncStatusState === "success" ? syncStatusQuery.data?.last_synced_at ?? null : null;
  const integrations = integrationsState === "success" ? integrationsQuery.data ?? NO_INTEGRATIONS : NO_INTEGRATIONS;

  const flows = useMemo<FlowResult[]>(() => {
    const out: FlowResult[] = [];
    for (const integration of integrations) {
      for (const fs of integration.flow_schedules) {
        out.push({
          id: fs.id,
          name: fs.name,
          integrationId: integration.id,
          integrationName: integration.name,
          stall: stallState({
            schedule: fs.schedule,
            disabled: fs.disabled,
            lastExecutedAt: fs.last_executed_at,
            lastSyncedAt,
          }),
        });
      }
    }
    return out;
  }, [integrations, lastSyncedAt]);

  function selectIntegration(id: string) {
    setOpen(false);
    route.go.integration(id);
  }

  function selectFlow(id: string) {
    setOpen(false);
    route.go.flow(id);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-lg gap-0 overflow-hidden p-0">
        <DialogTitle className="sr-only">Search integrations & flows</DialogTitle>
        <Command loop className="flex flex-col">
          <Command.Input
            autoFocus
            placeholder="Search integrations & flows"
            className="w-full border-b bg-transparent px-4 py-3 text-[14px] outline-none placeholder:text-muted-foreground"
          />
          <Command.List className="max-h-80 overflow-y-auto p-1.5">
            {integrationsState === "pending" || syncStatusState === "pending" ? (
              <Command.Loading className="px-3 py-6 text-center text-[13px] text-muted-foreground">
                Loading…
              </Command.Loading>
            ) : integrationsState === "error" ? (
              <div className="px-3 py-6 text-center text-[13px] text-muted-foreground">
                Couldn&rsquo;t load integrations.
              </div>
            ) : (
              <>
                <Command.Empty className="px-3 py-6 text-center text-[13px] text-muted-foreground">
                  No matches.
                </Command.Empty>
                <Command.Group
                  heading="Integrations"
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-muted-foreground"
                >
                  {integrations.map((integration) => (
                    <Command.Item
                      key={integration.id}
                      value={`integration:${integration.name}`}
                      onSelect={() => selectIntegration(integration.id)}
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] aria-selected:bg-muted"
                    >
                      <ResultRow>
                        <span className="truncate">{integration.name}</span>
                      </ResultRow>
                    </Command.Item>
                  ))}
                </Command.Group>
                <Command.Group
                  heading="Flows"
                  className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-muted-foreground"
                >
                  {flows.map((flow) => (
                    <Command.Item
                      key={flow.id}
                      value={`flow:${flow.name}`}
                      onSelect={() => selectFlow(flow.id)}
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] aria-selected:bg-muted"
                    >
                      <ResultRow>
                        <HealthDot stall={flow.stall} />
                        <span className="min-w-0 flex-1 truncate">{flow.name}</span>
                        <span className="shrink-0 truncate text-[11px] text-muted-foreground">
                          {flow.integrationName}
                        </span>
                      </ResultRow>
                    </Command.Item>
                  ))}
                </Command.Group>
              </>
            )}
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
