"use client";

import Link from "next/link";
import { FileBarChart, ArrowRight } from "lucide-react";

import type { ReportReadyData } from "@/lib/chat-stream";

export function ReportReadyCard({ data }: { data: ReportReadyData }) {
  return (
    <Link
      href={`/reports/${data.report_id}`}
      aria-label={`Open report ${data.title}`}
      className="flex items-center gap-3 rounded-xl border bg-card p-4 shadow-soft hover:bg-accent/50 transition-colors"
    >
      <FileBarChart aria-hidden className="h-5 w-5 text-indigo-600 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-medium text-foreground truncate">{data.title}</p>
        <p className="text-[13px] text-muted-foreground truncate">Open report</p>
      </div>
      <ArrowRight aria-hidden className="h-4 w-4 text-muted-foreground shrink-0" />
    </Link>
  );
}
