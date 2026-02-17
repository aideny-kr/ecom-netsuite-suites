"use client";

import { useState } from "react";
import { useAudit } from "@/hooks/use-audit";
import { AuditFilters } from "@/components/audit-filters";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ChevronLeft, ChevronRight } from "lucide-react";

export default function AuditPage() {
  const [page, setPage] = useState(1);
  const [category, setCategory] = useState("");
  const [action, setAction] = useState("");
  const [correlationId, setCorrelationId] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const { data, isLoading } = useAudit({
    page,
    pageSize: 25,
    category: category && category !== "all" ? category : undefined,
    action: action && action !== "all" ? action : undefined,
    correlationId: correlationId || undefined,
    startDate: startDate || undefined,
    endDate: endDate || undefined,
  });

  function clearFilters() {
    setCategory("");
    setAction("");
    setCorrelationId("");
    setStartDate("");
    setEndDate("");
    setPage(1);
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Audit Log</h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Track all actions and events in your account
        </p>
      </div>

      <AuditFilters
        category={category}
        action={action}
        correlationId={correlationId}
        startDate={startDate}
        endDate={endDate}
        onCategoryChange={(v) => {
          setCategory(v);
          setPage(1);
        }}
        onActionChange={(v) => {
          setAction(v);
          setPage(1);
        }}
        onCorrelationIdChange={(v) => {
          setCorrelationId(v);
          setPage(1);
        }}
        onStartDateChange={(v) => {
          setStartDate(v);
          setPage(1);
        }}
        onEndDateChange={(v) => {
          setEndDate(v);
          setPage(1);
        }}
        onClear={clearFilters}
      />

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full rounded-xl" />
          <Skeleton className="h-64 w-full rounded-xl" />
        </div>
      ) : (
        <>
          <div className="overflow-hidden rounded-xl border bg-card shadow-soft">
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/50 hover:bg-muted/50">
                  <TableHead className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Timestamp
                  </TableHead>
                  <TableHead className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Category
                  </TableHead>
                  <TableHead className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Action
                  </TableHead>
                  <TableHead className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Entity
                  </TableHead>
                  <TableHead className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Correlation ID
                  </TableHead>
                  <TableHead className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                    Detail
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.items?.length ? (
                  data.items.map((event) => (
                    <TableRow
                      key={event.id}
                      className="transition-colors hover:bg-muted/30"
                    >
                      <TableCell className="whitespace-nowrap text-[13px] tabular-nums text-muted-foreground">
                        {new Date(event.timestamp).toLocaleString()}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant="secondary"
                          className="text-[11px] font-medium"
                        >
                          {event.category}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className="text-[11px] font-medium"
                        >
                          {event.action}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-[13px]">
                        {event.resource_type
                          ? `${event.resource_type}:${event.resource_id}`
                          : "-"}
                      </TableCell>
                      <TableCell className="max-w-[120px] truncate text-[12px] font-mono text-muted-foreground">
                        {event.correlation_id || "-"}
                      </TableCell>
                      <TableCell className="max-w-[200px] truncate text-[13px] text-muted-foreground">
                        {event.payload
                          ? JSON.stringify(event.payload)
                          : "-"}
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={6} className="h-32 text-center">
                      <p className="text-[15px] font-medium text-muted-foreground">
                        No audit events found
                      </p>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>

          <div className="flex items-center justify-end gap-3">
            <span className="text-[13px] tabular-nums text-muted-foreground">
              Page {page} of {data?.pages || 1}
            </span>
            <div className="flex gap-1">
              <Button
                variant="outline"
                size="icon"
                className="h-8 w-8"
                onClick={() => setPage((p) => p - 1)}
                disabled={page <= 1}
              >
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <Button
                variant="outline"
                size="icon"
                className="h-8 w-8"
                onClick={() => setPage((p) => p + 1)}
                disabled={page >= (data?.pages || 1)}
              >
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
