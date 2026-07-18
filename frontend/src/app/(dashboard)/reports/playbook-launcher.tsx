"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { usePlaybooks, useComposePlaybook, type PlaybookInfo } from "@/hooks/use-reports";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export function PlaybookLauncher() {
  const { data, isLoading } = usePlaybooks();
  const composePlaybook = useComposePlaybook();
  const router = useRouter();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  if (isLoading || !data?.length) return null;

  const selected = data.find((playbook) => playbook.key === selectedKey) ?? null;

  function handleSelect(playbook: PlaybookInfo) {
    setSelectedKey(playbook.key);
    setParamValues({});
    setActionMsg(null);
  }

  function handleCreate() {
    if (!selected) return;
    setActionMsg(null);
    composePlaybook.mutate(
      { key: selected.key, params: paramValues },
      {
        onSuccess: (report) => router.push(`/reports/${report.id}`),
        // The backend's detail strings are user-facing (400 malformed period, 502 no
        // NetSuite connection) — surface them instead of failing silently.
        onError: (e: Error) => setActionMsg(e.message || "Couldn't create report"),
      },
    );
  }

  return (
    <div className="space-y-2">
      <h3 className="text-[15px] font-medium text-foreground">Playbooks</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        {data.map((playbook) => (
          <button
            key={playbook.key}
            type="button"
            onClick={() => handleSelect(playbook)}
            className={cn(
              "block w-full rounded-xl border bg-card p-5 text-left shadow-soft transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
              selectedKey === playbook.key && "border-primary",
            )}
          >
            <span className="block text-[15px] font-medium text-foreground">{playbook.name}</span>
            <span className="mt-0.5 block text-[13px] text-muted-foreground">{playbook.description}</span>
          </button>
        ))}
      </div>

      {selected ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border bg-card p-5 shadow-soft">
          {selected.params.map((param) => (
            <Input
              key={param.key}
              placeholder={param.example}
              aria-label={param.label}
              value={paramValues[param.key] ?? ""}
              onChange={(event) =>
                setParamValues((prev) => ({ ...prev, [param.key]: event.target.value }))
              }
              className="max-w-xs"
            />
          ))}
          <Button onClick={handleCreate} disabled={composePlaybook.isPending}>
            Create report
          </Button>
          {actionMsg && <span className="text-[13px] text-destructive">{actionMsg}</span>}
        </div>
      ) : null}
    </div>
  );
}
