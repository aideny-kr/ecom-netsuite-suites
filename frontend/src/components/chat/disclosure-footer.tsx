"use client";

import { cn } from "@/lib/utils";

export interface DisclosureData {
  source: "netsuite" | "bigquery" | "shopify" | "stripe" | "drive" | string;
  interpretation: string;
  date_range: string;
  implicit_filters: string[];
}

interface Props {
  data: DisclosureData | null;
  className?: string;
}

const SOURCE_LABELS: Record<string, string> = {
  netsuite: "NetSuite",
  bigquery: "BigQuery",
  shopify: "Shopify",
  stripe: "Stripe",
  drive: "Google Drive",
};

export function DisclosureFooter({ data, className }: Props) {
  if (!data) return null;

  const label = SOURCE_LABELS[data.source] ?? data.source;
  const parts = [
    `From ${label}`,
    data.interpretation,
    data.date_range,
    ...data.implicit_filters,
  ].filter(Boolean);

  return (
    <div
      className={cn(
        "text-[12px] italic text-muted-foreground leading-relaxed mt-1",
        className
      )}
    >
      {parts.join(" · ")}
    </div>
  );
}
