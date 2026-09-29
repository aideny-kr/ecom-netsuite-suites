"use client";

import { Loader2 } from "lucide-react";
import type { PreparationProgressData } from "@/lib/chat-stream";

/** What the agent has checked so far while preparing a group of corrections. Every number is
 * the server's own count, streamed as each order finishes; the model never states them. */
export function PreparationProgress({ data }: { data: PreparationProgressData }) {
  const total = Math.max(data.total, 1);
  const aside = data.set_aside.reduce((sum, item) => sum + item.count, 0);
  const readyPct = (data.ready / total) * 100;
  const asidePct = (aside / total) * 100;
  return (
    <div
      role="status"
      aria-label="Group preparation progress"
      className="space-y-3 rounded-xl border bg-card p-4"
    >
      <div className="flex items-center gap-2 text-[13px]">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground motion-reduce:animate-none" aria-hidden />
        <span>
          Checking orders for exact fixes ·{" "}
          <strong className="tabular-nums">
            {data.checked} of {data.total}
          </strong>{" "}
          checked
        </span>
      </div>
      <div
        className="flex h-1.5 overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={`${data.ready} fixes ready, ${aside} set aside, ${Math.max(data.total - data.checked, 0)} still to check`}
      >
        <span className="h-full bg-emerald-600" style={{ width: `${readyPct}%` }} />
        <span className="h-full bg-muted-foreground/40" style={{ width: `${asidePct}%` }} />
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-muted-foreground">
        <span>
          <strong className="tabular-nums text-foreground">{data.ready}</strong> fixes ready
        </span>
        {aside > 0 && (
          <span>
            <strong className="tabular-nums text-foreground">{aside}</strong> set aside:{" "}
            {data.set_aside.map((item) => `${item.count} ${item.label}`).join(" · ")}
          </span>
        )}
        {data.now.length > 0 && (
          <span>
            Now checking <span className="font-mono tabular-nums">{data.now.join(", ")}</span>
          </span>
        )}
      </div>
    </div>
  );
}
