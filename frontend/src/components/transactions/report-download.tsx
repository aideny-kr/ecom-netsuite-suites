"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { apiClient } from "@/lib/api-client";
import { safeError } from "../transaction-ops/format";

export function ReportDownload({
  runIds,
  status,
  search,
  total,
}: {
  runIds: string[];
  status: string;
  search: string;
  total?: number;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function download() {
    if (busy || !runIds.length) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiClient.download(
        "/api/v1/transaction-ops/review-export",
        {
          review_run_ids: runIds,
          ...(status ? { status } : {}),
          search,
        },
      );
      if (!response.headers.get("content-type")?.includes("spreadsheetml"))
        throw new Error("Report download returned an unexpected file.");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download =
        response.headers
          .get("content-disposition")
          ?.match(/filename="([a-z0-9-]+\.xlsx)"/i)?.[1] ||
        "reconciliation.xlsx";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      const message = safeError(err);
      setError(
        message.includes("export_too_large")
          ? "This report exceeds 50,000 orders. Choose a shorter period or narrower filters."
          : `Report download failed. ${message}`,
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-1 text-right">
      <Button
        onClick={download}
        disabled={busy || !runIds.length || total === undefined}
      >
        {busy ? "Preparing Excel…" : "Download Excel"}
      </Button>
      <p className="text-xs text-muted-foreground">
        {total === undefined
          ? "Select a review to export"
          : `Exports all ${total} filtered orders across every page`}
      </p>
      {error && (
        <p role="alert" className="max-w-md text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
