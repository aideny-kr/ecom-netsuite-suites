"use client";

import { ExternalLink, FileText } from "lucide-react";

import type { DocsLinkData } from "@/lib/chat-stream";

interface DocsLinkCardProps {
  data: DocsLinkData;
}

export function DocsLinkCard({ data }: DocsLinkCardProps) {
  return (
    <a
      href={data.url}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open ${data.title} in Google Docs`}
      className="flex items-center gap-3 rounded-xl border bg-card p-4 shadow-soft hover:bg-accent/50 transition-colors"
    >
      <FileText aria-hidden="true" className="h-5 w-5 text-blue-600 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-medium text-foreground truncate">
          {data.title}
        </p>
        {data.shared_with ? (
          <p className="text-[13px] text-muted-foreground truncate">
            Shared with {data.shared_with}
          </p>
        ) : (
          <p className="text-[13px] text-muted-foreground truncate">{data.url}</p>
        )}
      </div>
      <ExternalLink aria-hidden="true" className="h-4 w-4 text-muted-foreground shrink-0" />
    </a>
  );
}
