"use client";

import { useState } from "react";
import { Network } from "lucide-react";

import { MemoryGraphCanvas } from "@/components/memory/memory-graph-canvas";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useMemoryGraph, type MemoryReviewState } from "@/hooks/use-memory-graph";

const FILTERS: { id: "all" | MemoryReviewState; label: string }[] = [
  { id: "all", label: "All" },
  { id: "confirmed", label: "Confirmed" },
  { id: "pending", label: "Pending" },
];

export default function MemoryPage() {
  const [filter, setFilter] = useState<"all" | MemoryReviewState>("all");

  const { data, isLoading, error } = useMemoryGraph(
    filter === "all" ? undefined : filter,
  );

  const concepts = data?.concepts ?? [];

  return (
    <div className="animate-fade-in space-y-8 p-8">
      <div className="flex items-center gap-3">
        <Network className="h-6 w-6 text-muted-foreground" />
        <div>
          <h1 className="text-2xl font-bold text-foreground">Memory</h1>
          <p className="text-[13px] text-muted-foreground">
            Plain-English concepts the assistant has learned about your business. Only
            confirmed concepts are used when answering questions.
          </p>
        </div>
      </div>

      {/* Filter chips */}
      <div className="flex gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={cn(
              "rounded-full border px-3.5 py-1.5 text-[13px] font-medium transition-colors",
              filter === f.id
                ? "border-primary bg-primary text-primary-foreground"
                : "border-input text-muted-foreground hover:bg-muted/50",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div data-testid="memory-loading" className="space-y-3">
          <Skeleton className="h-[640px] w-full rounded-xl" />
        </div>
      ) : error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-6 text-[13px] text-destructive">
          Failed to load the memory graph. Please try again.
        </div>
      ) : concepts.length === 0 ? (
        <div className="rounded-xl border bg-card p-10 text-center shadow-soft">
          <Network className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-[15px] font-medium text-foreground">
            No concepts yet
          </p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {filter === "all"
              ? "Run the backfill to distill what the assistant has learned into reviewable concepts."
              : `No ${filter} concepts. Try a different filter.`}
          </p>
        </div>
      ) : (
        <MemoryGraphCanvas graph={{ concepts, edges: data?.edges ?? [] }} />
      )}
    </div>
  );
}
