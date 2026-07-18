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

  if (isLoading || !data?.length) return null;

  const selected = data.find((playbook) => playbook.key === selectedKey) ?? null;

  function handleSelect(playbook: PlaybookInfo) {
    setSelectedKey(playbook.key);
    setParamValues({});
  }

  function handleCreate() {
    if (!selected) return;
    composePlaybook.mutate(
      { key: selected.key, params: paramValues },
      { onSuccess: (report) => router.push(`/reports/${report.id}`) },
    );
  }

  return (
    <div className="space-y-2">
      <h3 className="text-[15px] font-medium text-foreground">Playbooks</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        {data.map((playbook) => (
          <div
            key={playbook.key}
            role="button"
            tabIndex={0}
            onClick={() => handleSelect(playbook)}
            className={cn(
              "cursor-pointer rounded-xl border bg-card p-5 shadow-soft transition-colors hover:bg-muted/30",
              selectedKey === playbook.key && "border-primary",
            )}
          >
            <p className="text-[15px] font-medium text-foreground">{playbook.name}</p>
            <p className="mt-0.5 text-[13px] text-muted-foreground">{playbook.description}</p>
          </div>
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
        </div>
      ) : null}
    </div>
  );
}
