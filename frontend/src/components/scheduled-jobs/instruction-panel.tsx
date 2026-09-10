"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "Instruction").
 * The plain-language instruction a schedule is compiled from: read-only by
 * default, an "Edit" or "Ask the agent to adjust…" button opens a textarea,
 * Save PATCHes `{instruction}` (`app.api.v1.schedules.update_schedule` —
 * recompiles into `pending_plan_json` on an approved schedule, or replaces
 * `plan_json` directly otherwise; either way the response this panel's own
 * data comes from is the source of truth, not local state).
 *
 * "Ask the agent to adjust…" (v1, per the brief): nothing about a compile's
 * `Clarification` question is persisted server-side — `compile_instruction`
 * creates/changes nothing on that path (see its own docstring) — so there
 * is no stored "last clarification" to fetch. Both buttons open the SAME
 * editor; a clarification only ever appears here as the direct result of
 * THIS session's own failed Save (a 409 whose `detail.clarification` this
 * panel catches and shows above the textarea), which is also why the same
 * `clarification` state must survive a Save→409→Save round-trip without
 * being cleared until the instruction actually saves.
 */

import { useState } from "react";
import type { JSX } from "react";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { useUpdateSchedule } from "@/hooks/use-scheduled-jobs";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

/** The backend's own `readErrorMessage` (`lib/api-client.ts`) stringifies an
 * object `detail` via `JSON.stringify` — so a 409's `.message` here is the
 * literal text `{"clarification":"..."}`, not the question itself. Parsed
 * defensively: any other 409 (or any other status) renders as a plain
 * error instead of a blank clarification banner. */
function parseClarification(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  try {
    const parsed = JSON.parse(err.message);
    return typeof parsed?.clarification === "string" ? parsed.clarification : null;
  } catch {
    return null;
  }
}

export function InstructionPanel({ schedule }: { schedule: ScheduleDetail }): JSX.Element {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(schedule.instruction ?? "");
  const [clarification, setClarification] = useState<string | null>(null);
  const update = useUpdateSchedule(schedule.id);

  function openEditor() {
    setDraft(schedule.instruction ?? "");
    setEditing(true);
  }

  function handleCancel() {
    setEditing(false);
    setClarification(null);
  }

  function handleSave() {
    update.mutate(
      { instruction: draft },
      {
        onSuccess: () => {
          setEditing(false);
          setClarification(null);
        },
        onError: (err: unknown) => {
          const question = parseClarification(err);
          setClarification(question);
        },
      },
    );
  }

  const plainError = update.isError && !clarification ? (update.error as Error | null)?.message : null;

  return (
    <div className="rounded-lg border bg-card">
      <h3 className="flex items-center gap-2 border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        Instruction
        <span className="flex-1" />
        <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={openEditor}>
          Edit
        </Button>
        <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={openEditor}>
          Ask the agent to adjust…
        </Button>
      </h3>
      <div className="p-3">
        {editing ? (
          <div className="space-y-2">
            {clarification && (
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-[12.5px]">
                The agent asked: {clarification}
              </div>
            )}
            <textarea
              className="min-h-[96px] w-full rounded-md border bg-background p-2.5 text-[14px] leading-relaxed"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={update.isPending}
            />
            {plainError && <p className="text-[12px] text-destructive">{plainError}</p>}
            <div className="flex gap-2">
              <Button size="sm" disabled={update.isPending || !draft.trim()} onClick={handleSave}>
                Save
              </Button>
              <Button variant="ghost" size="sm" onClick={handleCancel} disabled={update.isPending}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="min-h-[96px] whitespace-pre-wrap rounded-md border bg-muted/40 px-3 py-2.5 text-[14px] leading-relaxed">
              {schedule.instruction || "No instruction yet."}
            </div>
            <p className="mt-2 text-[11.5px] text-muted-foreground">
              Written by you (or by the chat when you said &quot;schedule this&quot;). This is the source of truth;
              the plan below is compiled from it. Numbers in the output never come from this text.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
