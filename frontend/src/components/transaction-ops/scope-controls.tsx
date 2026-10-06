"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import {
  useControlTransactionConfig,
  useScheduleReadiness,
} from "@/hooks/use-transaction-setup";
import { dateLabel, safeError } from "./format";
type Scope = { id: string; enabled: boolean; schedule_enabled: boolean };
export function ScopeControls({ config }: { config: Scope }) {
  const access = useTransactionAccess();
  return access.canManage ? <Controls key={config.id} config={config} /> : null;
}
function Controls({ config }: { config: Scope }) {
  const readiness = useScheduleReadiness(config.id);
  const control = useControlTransactionConfig(),
    [message, setMessage] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function change(enabled: boolean, schedule_enabled: boolean) {
    setMessage("");
    try {
      await control.mutateAsync({ id: config.id, enabled, schedule_enabled });
      if (mounted.current) setMessage("Scope controls saved.");
    } catch (error) {
      if (mounted.current) setMessage(safeError(error));
    }
  }
  return (
    <div className="space-y-3 border-t pt-5">
      <p className="text-[13px] font-medium">Administrator controls</p>
      {readiness.data?.status === "execution_access_unavailable" && (
        <p role="status" className="text-[13px]">
          Scheduled reads are blocked. Check the schedule owner's permissions
          and source connections before resuming.
        </p>
      )}
      {readiness.data?.status === "scope_paused" && (
        <p role="status" className="text-[13px]">
          Enable the scope to check selected accounting context before enabling
          its schedule.
        </p>
      )}
      {readiness.isError && (
        <p role="status" className="text-[13px]">
          Schedule readiness unavailable. Reload before enabling the schedule.
        </p>
      )}
      {readiness.data?.renewal_needed && (
        <p role="status" className="text-[13px]">
          {readiness.data.ready
            ? "Selected accounting context expires soon."
            : "Selected accounting context needs review. Scheduled reads are blocked."}
          {readiness.data.review_by &&
            ` Review by ${dateLabel(readiness.data.review_by)}.`}
          {
            " Pause the schedule and create a successor scope with the reviewed context revision before resuming. Context review does not approve corrections."
          }
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <Button
          type="button"
          variant="outline"
          disabled={control.isPending}
          onClick={() => change(!config.enabled, false)}
        >
          {config.enabled ? "Pause scope" : "Enable scope"}
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={
            control.isPending ||
            !config.enabled ||
            (!config.schedule_enabled &&
              (readiness.isPending ||
                readiness.isError ||
                !readiness.data?.ready))
          }
          onClick={() => change(true, !config.schedule_enabled)}
        >
          {config.schedule_enabled ? "Pause schedule" : "Enable schedule"}
        </Button>
      </div>
      {message && (
        <p role="status" className="text-[13px]">
          {message}
        </p>
      )}
    </div>
  );
}
