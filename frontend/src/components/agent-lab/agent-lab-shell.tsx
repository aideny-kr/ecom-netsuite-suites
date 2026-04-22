"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { BenchmarkTab } from "./benchmark-tab";
import { ExperimentsTab } from "./experiments-tab";
import { PatternsTab } from "./patterns-tab";

type Tab = "benchmark" | "experiment" | "patterns";

export function AgentLabShell() {
  const [tab, setTab] = useState<Tab>("benchmark");

  return (
    <div className="flex flex-col h-full space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-medium">Agent Lab</h1>
          <p className="text-[13px] text-muted-foreground">
            Framework tenant · super-admin only
          </p>
        </div>
      </div>

      <div className="flex gap-1 border-b">
        {(["benchmark", "experiment", "patterns"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cn(
              "px-4 py-2 text-[13px] border-b-2 -mb-px transition-colors",
              tab === t
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t === "benchmark" && "Benchmark (vs-MCP)"}
            {t === "experiment" && "Experiments"}
            {t === "patterns" && "Patterns"}
          </button>
        ))}
      </div>

      <div className="flex-1 min-h-0">
        {tab === "benchmark" && <BenchmarkTab />}
        {tab === "experiment" && <ExperimentsTab />}
        {tab === "patterns" && <PatternsTab />}
      </div>
    </div>
  );
}
