"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import { useControlTransactionConfig } from "@/hooks/use-transaction-setup";
import { safeError } from "./format";
type Scope = { id: string; enabled: boolean; schedule_enabled: boolean };
export function ScopeControls({ config }: { config: Scope }) {
  const access = useTransactionAccess();
  return access.canManage ? <Controls key={config.id} config={config} /> : null;
}
function Controls({ config }: { config: Scope }) {
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
          disabled={control.isPending || !config.enabled}
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
