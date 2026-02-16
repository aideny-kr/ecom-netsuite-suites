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
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-bold tracking-tight">Audit Log</h2>
        <p className="text-muted-foreground">
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
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : (
        <>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Timestamp</TableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Entity</TableHead>
                  <TableHead>Correlation ID</TableHead>
                  <TableHead>Detail</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.items?.length ? (
                  data.items.map((event) => (
                    <TableRow key={event.id}>
                      <TableCell className="whitespace-nowrap text-xs">
                        {new Date(event.created_at).toLocaleString()}
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">{event.category}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{event.action}</Badge>
                      </TableCell>
                      <TableCell className="text-xs">
                        {event.entity_type
                          ? `${event.entity_type}:${event.entity_id}`
                          : "-"}
                      </TableCell>
                      <TableCell className="max-w-[120px] truncate text-xs font-mono">
                        {event.correlation_id || "-"}
                      </TableCell>
                      <TableCell className="max-w-[200px] truncate text-xs">
                        {event.detail
                          ? JSON.stringify(event.detail)
                          : "-"}
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={6} className="h-24 text-center">
                      No audit events found.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>

          <div className="flex items-center justify-end gap-2">
            <span className="text-sm text-muted-foreground">
              Page {page} of {data?.total_pages || 1}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => p - 1)}
              disabled={page <= 1}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => p + 1)}
              disabled={page >= (data?.total_pages || 1)}
            >
              Next
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
