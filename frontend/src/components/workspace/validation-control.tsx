"use client";

import { useEffect, useId, useState } from "react";
import { Shield } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConnections } from "@/hooks/use-connections";
import { useTriggerValidate } from "@/hooks/use-runs";

/** Selection is a request only. The server rechecks project and target authority. */
export function ValidationControl({ changesetId, disabled = false, retry = false }: {
  changesetId: string;
  disabled?: boolean;
  retry?: boolean;
}) {
  const id = useId();
  const [choice, setChoice] = useState<{ id: string; account: string } | null>(null);
  const connections = useConnections();
  const validation = useTriggerValidate();
  const resetValidation = validation.reset;
  const targets = (connections.data ?? []).filter(c => c.provider === "netsuite");
  const account = (metadata: Record<string, unknown> | null) =>
    typeof metadata?.account_id === "string" ? metadata.account_id : "";
  const usable = (status: string, accountId: string) =>
    ["active", "healthy"].includes(status) && /^[0-9]+[-_]sb[0-9]+$/i.test(accountId);
  const selected = targets.find(c => c.id === choice?.id && account(c.metadata_json) === choice.account && usable(c.status, account(c.metadata_json)));
  const unavailable = disabled || connections.isPending || connections.isFetching || connections.isError || !selected;
  useEffect(() => {
    if (choice && !selected) {
      setChoice(null);
      if (!validation.isPending) resetValidation();
    }
  }, [choice, selected, resetValidation, validation.isPending]);

  return <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
    <label htmlFor={id} className="block text-[12px] font-medium">Validation sandbox connection</label>
    <select id={id} value={selected?.id ?? ""}
      disabled={disabled || connections.isPending || connections.isFetching || connections.isError || validation.isPending}
      onChange={event => {
        const target = targets.find(c => c.id === event.target.value);
        setChoice(target ? { id: target.id, account: account(target.metadata_json) } : null);
        validation.reset();
      }}
      aria-describedby={`${id}-help`}
      className="w-full min-w-0 rounded-md border bg-background px-2 py-2 text-[12px]">
      <option value="">Choose a sandbox connection</option>
      {targets.map(c => {
        const accountId = account(c.metadata_json);
        const enabled = usable(c.status, accountId);
        return <option key={c.id} value={c.id} disabled={!enabled}>
          {c.label} · {accountId || "Account identity unavailable"}{!enabled ? " · Unavailable" : ""}
        </option>;
      })}
    </select>
    <p id={`${id}-help`} className="text-[11px] text-muted-foreground">
      {retry ? "Creates a new validation run with the selected connection. " : ""}
      The server verifies current project and sandbox access. Validation does not authorize deployment.
    </p>
    {connections.isPending && <p role="status" className="text-[11px]">Loading connections…</p>}
    {connections.isFetching && !connections.isPending && <p role="status" className="text-[11px]">Refreshing connections…</p>}
    {connections.isError && <div className="text-[11px]">
      <p role="alert">Could not load connections. Try again before validating.</p>
      <button type="button" onClick={() => { void connections.refetch(); }} className="text-primary underline">Reload connections</button>
    </div>}
    {!connections.isPending && !connections.isError && !targets.some(c => usable(c.status, account(c.metadata_json))) &&
      <p className="text-[11px] text-muted-foreground">No active sandbox connection with an account identity is available. Manage connections in Settings.</p>}
    {disabled && <p className="text-[11px] text-muted-foreground">Approve this changeset before validation.</p>}
    <Button size="sm" variant="outline" className="h-7 text-[12px]"
      disabled={unavailable || validation.isPending}
      onClick={() => { if (selected) validation.mutate({ changesetId, connectionId: selected.id }); }}>
      <Shield className="mr-1 h-3 w-3" />
      {validation.isPending ? "Requesting validation…" : retry ? "Run validation again" : "Validate"}
    </Button>
    {validation.error && <p role="alert" className="break-words text-[12px] text-destructive">{validation.error.message}</p>}
    {validation.data && <p role="status" className="text-[11px] text-muted-foreground">Validation request saved. See run history for results.</p>}
  </div>;
}
