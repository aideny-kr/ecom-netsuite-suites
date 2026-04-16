"use client";

import { ExternalLink, FileSpreadsheet } from "lucide-react";

import type { SheetsLinkData } from "@/lib/chat-stream";

interface SheetsLinkCardProps {
  data: SheetsLinkData;
}

export function SheetsLinkCard({ data }: SheetsLinkCardProps) {
  return (
    <a
      href={data.url}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-3 rounded-xl border bg-card p-4 shadow-soft hover:bg-accent/50 transition-colors"
    >
      <FileSpreadsheet className="h-5 w-5 text-green-600 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-medium text-foreground truncate">
          {data.title || "Google Sheet"}
        </p>
        <p className="text-[13px] text-muted-foreground truncate">{data.url}</p>
      </div>
      <ExternalLink className="h-4 w-4 text-muted-foreground shrink-0" />
    </a>
  );
}
