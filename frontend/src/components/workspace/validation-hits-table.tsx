"use client";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ValidationHit } from "@/lib/types";

interface Props {
  hits: ValidationHit[];
}

const severityStyles: Record<ValidationHit["severity"], string> = {
  error: "bg-red-100 text-red-700",
  warning: "bg-amber-100 text-amber-700",
  info: "bg-gray-100 text-gray-700",
  parser_error: "bg-orange-100 text-orange-700",
};

function formatLocation(file: string | null, line: number | null): string {
  if (!file) return "—";
  return line ? `${file}:${line}` : file;
}

export function ValidationHitsTable({ hits }: Props) {
  if (hits.length === 0) {
    return (
      <p className="text-[11px] italic text-muted-foreground">
        No validate hits
      </p>
    );
  }

  return (
    <div className="space-y-1">
      {hits.map((hit) => (
        <div
          key={hit.id}
          className="grid grid-cols-[max-content_max-content_max-content_1fr] items-start gap-2 rounded border bg-card px-2 py-1.5 text-[11px]"
        >
          <Badge
            data-testid="severity-badge"
            variant="secondary"
            className={cn("text-[10px]", severityStyles[hit.severity])}
          >
            {hit.severity}
          </Badge>
          <span className="font-mono text-muted-foreground">
            {formatLocation(hit.file_path, hit.line)}
          </span>
          <span className="font-mono">{hit.code ?? "—"}</span>
          <span className="text-foreground">{hit.message}</span>
        </div>
      ))}
    </div>
  );
}
