"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { usePlaybooks, useComposePlaybook, type PlaybookInfo } from "@/hooks/use-reports";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

// Every current playbook recipe (playbooks.py PLAYBOOKS) carries a "period" param, but
// only income_statement's recipe pulls all four comparison sources (r1-r4: current,
// prior, yoy, trailing trend) — balance_sheet/trial_balance stop at current + prior.
// Since the source line can't truthfully claim four sources for every playbook, every
// playbook uses this generic line instead of enumerating sources that don't apply.
const SOURCE_LINE = "Pulling live data from NetSuite — current period, comparisons, and trend";

function ComposingCard({ playbook, period }: { playbook: PlaybookInfo; period: string }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    setElapsed(0);
    const id = setInterval(() => setElapsed((prev) => prev + 1), 1000);
    return () => clearInterval(id);
  }, [playbook.key]);

  const title = period.trim()
    ? `Composing ${playbook.name} · ${period.trim()}`
    : `Composing ${playbook.name}`;

  return (
    <div role="status" aria-live="polite" className="space-y-3 rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-center gap-2">
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary motion-reduce:animate-none" />
        <span className="text-[15px] font-medium text-foreground">{title}</span>
        <span className="ml-auto text-[13px] tabular-nums text-muted-foreground">{elapsed}s elapsed</span>
      </div>

      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className="absolute inset-y-0 w-1/3 animate-[report-sweep_1.4s_ease-in-out_infinite] rounded-full bg-gradient-to-r from-transparent via-primary to-transparent motion-reduce:animate-none" />
      </div>

      <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
        <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-primary motion-reduce:animate-none" />
        <span>{SOURCE_LINE}</span>
      </div>

      <p className="text-[13px] text-muted-foreground">
        {"This usually takes 20–40 seconds. You'll land on the finished report automatically."}
      </p>
    </div>
  );
}

export function PlaybookLauncher() {
  const { data, isLoading } = usePlaybooks();
  const composePlaybook = useComposePlaybook();
  const router = useRouter();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  if (isLoading || !data?.length) return null;

  const selected = data.find((playbook) => playbook.key === selectedKey) ?? null;
  const isPending = composePlaybook.isPending;

  function handleSelect(playbook: PlaybookInfo) {
    if (isPending) return;
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
            disabled={isPending}
            className={cn(
              "block w-full rounded-xl border bg-card p-5 text-left shadow-soft transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-card",
              selectedKey === playbook.key && "border-primary",
            )}
          >
            <span className="block text-[15px] font-medium text-foreground">{playbook.name}</span>
            <span className="mt-0.5 block text-[13px] text-muted-foreground">{playbook.description}</span>
          </button>
        ))}
      </div>

      {selected && isPending ? (
        <ComposingCard playbook={selected} period={paramValues.period ?? ""} />
      ) : selected ? (
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
          <Button onClick={handleCreate} disabled={isPending}>
            Create report
          </Button>
          {actionMsg && <span className="text-[13px] text-destructive">{actionMsg}</span>}
        </div>
      ) : null}
    </div>
  );
}
