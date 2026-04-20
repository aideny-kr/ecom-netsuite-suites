"use client";

import { useQuery } from "@tanstack/react-query";
import { listPatterns } from "@/lib/agent-lab";

export function PatternsTab() {
  const { data: patterns, isLoading, error } = useQuery({
    queryKey: ["agent-lab-patterns"],
    queryFn: listPatterns,
  });

  if (isLoading) return <div className="p-5 text-[13px] text-muted-foreground">Loading…</div>;
  if (error) return <div className="p-5 text-[13px] text-destructive">Failed to load patterns.</div>;
  if (!patterns || patterns.length === 0) {
    return (
      <div className="p-5 text-[13px] text-muted-foreground">
        No patterns stored yet. Patterns are promoted from the experiment loop (see Experiments tab).
      </div>
    );
  }

  return (
    <div className="p-5 overflow-x-auto">
      <table className="w-full text-[13px]">
        <thead className="border-b text-[11px] uppercase text-muted-foreground">
          <tr>
            <th className="px-2 py-2 text-left">#</th>
            <th className="px-2 py-2 text-left">Question</th>
            <th className="px-2 py-2 text-right">Uses</th>
            <th className="px-2 py-2 text-left">Last used</th>
            <th className="px-2 py-2 text-left">Age</th>
          </tr>
        </thead>
        <tbody>
          {patterns.map((p, i) => {
            const neverUsed = !p.last_used_at;
            return (
              <tr
                key={p.id}
                className={neverUsed ? "border-b bg-yellow-50 hover:bg-yellow-100" : "border-b hover:bg-accent/50"}
              >
                <td className="px-2 py-2">{i + 1}</td>
                <td className="px-2 py-2">{p.user_question}</td>
                <td className="px-2 py-2 text-right">{p.success_count}</td>
                <td className="px-2 py-2">
                  {p.last_used_at ? new Date(p.last_used_at).toLocaleDateString() : <span className="text-yellow-700">never</span>}
                </td>
                <td className="px-2 py-2 text-muted-foreground">
                  {p.created_at
                    ? `${Math.floor(
                        (Date.now() - new Date(p.created_at).getTime()) / 86400000
                      )}d`
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="mt-3 flex justify-between text-[11px] text-muted-foreground">
        <span>{patterns.length} patterns total · sorted by last_used_at DESC NULLS LAST</span>
        <span>Sum of uses: {patterns.reduce((sum, p) => sum + p.success_count, 0)}</span>
      </div>
    </div>
  );
}
