"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { apiClient } from "@/lib/api-client";
import { transactionAmount } from "@/components/transactions/format";

interface RowDetailDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  row: Record<string, unknown> | null;
  tableName: string;
}

export function RowDetailDrawer({ open, onOpenChange, row, tableName }: RowDetailDrawerProps) {
  const [relatedLines, setRelatedLines] = useState<Record<string, unknown>[]>([]);
  const [loadingRelated, setLoadingRelated] = useState(false);
  const [relatedError, setRelatedError] = useState(false);
  const [relatedTotal, setRelatedTotal] = useState(0);

  useEffect(() => {
    let current = true;
    setRelatedLines([]);
    setRelatedError(false);
    setRelatedTotal(0);
    if (open && row && tableName === "payouts" && row.id) {
      setLoadingRelated(true);
      apiClient.get<{ items: Record<string, unknown>[]; total: number }>(`/api/v1/tables/payout_lines?payout_id=${encodeURIComponent(String(row.id))}&page_size=50`)
        .then(data => { if (current) { setRelatedLines(data.items); setRelatedTotal(data.total); } })
        .catch(() => { if (current) setRelatedError(true); })
        .finally(() => { if (current) setLoadingRelated(false); });
    } else {
      setLoadingRelated(false);
    }
    return () => { current = false; };
  }, [open, row, tableName]);

  if (!row) return null;

  const formatLabel = (key: string) =>
    key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());

  const formatValue = (value: unknown): string => {
    if (value === null || value === undefined) return "-";
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  };

  const skipFields = new Set(["raw_data", "tenant_id", "dedupe_key", "source_connection_id"]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-lg">Transaction details</DialogTitle>
          <DialogDescription className="text-[13px]">
            {formatLabel(tableName)} record
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto -mx-6 px-6 scrollbar-thin">
          <div className="space-y-3">
            {Object.entries(row)
              .filter(([key]) => !skipFields.has(key))
              .map(([key, value]) => (
                <div key={key} className="flex flex-col gap-1 rounded-lg bg-muted/30 px-3 py-2.5">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {formatLabel(key)}
                  </span>
                  <span className="text-[13px] break-all">
                    {key === "status" ? (
                      <Badge variant="outline" className="text-[11px]">
                        {String(value)}
                      </Badge>
                    ) : (
                      formatValue(value)
                    )}
                  </span>
                </div>
              ))}
          </div>

          {tableName === "payouts" ? (
            <>
              <hr className="my-4 border-border" />
              <div>
                <h4 className="text-[13px] font-semibold mb-2">Payout Lines</h4>
                {relatedError ? (
                  <p role="alert" className="text-[12px] text-destructive">Payout lines could not be loaded. Reopen this transaction to retry.</p>
                ) : loadingRelated ? (
                  <p className="text-[12px] text-muted-foreground">Loading...</p>
                ) : relatedLines.length === 0 ? (
                  <p className="text-[12px] text-muted-foreground">No payout lines found.</p>
                ) : (
                  <div className="space-y-2">
                    {relatedLines.map((line, i) => (
                      <div key={i} className="rounded-lg border p-3 text-[12px] space-y-1">
                        <div className="flex justify-between">
                          <span className="font-medium">{String(line.line_type || line.type || "Line")}</span>
                          <span className="tabular-nums">{transactionAmount(line.amount, line.currency)} {String(line.currency || "")}</span>
                        </div>
                        {line.description ? (
                          <p className="text-muted-foreground">{String(line.description)}</p>
                        ) : null}
                        {line.related_order_id ? (
                          <p className="text-muted-foreground">Order: {String(line.related_order_id)}</p>
                        ) : null}
                      </div>
                    ))}
                  </div>
                )}
                {relatedTotal > relatedLines.length && !relatedError && !loadingRelated && (
                  <Link className="mt-3 block text-[13px] text-primary underline" href={`/tables/payout_lines?payout_id=${encodeURIComponent(String(row.id))}`}>
                    View all {relatedTotal} payout lines
                  </Link>
                )}
              </div>
            </>
          ) : null}

          {tableName === "payout_lines" && row.related_order_id ? (
            <>
              <hr className="my-4 border-border" />
              <div className="text-[13px]">
                <span className="text-muted-foreground">Related Order: </span>
                <span className="font-mono">{String(row.related_order_id)}</span>
              </div>
            </>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
