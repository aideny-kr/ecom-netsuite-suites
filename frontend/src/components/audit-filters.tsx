"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { X } from "lucide-react";

interface AuditFiltersProps {
  category: string;
  action: string;
  correlationId: string;
  startDate: string;
  endDate: string;
  onCategoryChange: (value: string) => void;
  onActionChange: (value: string) => void;
  onCorrelationIdChange: (value: string) => void;
  onStartDateChange: (value: string) => void;
  onEndDateChange: (value: string) => void;
  onClear: () => void;
}

const categories = [
  "auth",
  "connection",
  "sync",
  "posting",
  "admin",
  "system",
];

const actions = [
  "create",
  "update",
  "delete",
  "login",
  "logout",
  "sync_start",
  "sync_complete",
  "sync_error",
  "post",
  "post_error",
];

export function AuditFilters({
  category,
  action,
  correlationId,
  startDate,
  endDate,
  onCategoryChange,
  onActionChange,
  onCorrelationIdChange,
  onStartDateChange,
  onEndDateChange,
  onClear,
}: AuditFiltersProps) {
  const hasFilters =
    category || action || correlationId || startDate || endDate;

  return (
    <div className="flex flex-wrap items-end gap-3 rounded-xl border bg-card p-4 shadow-soft">
      <div className="space-y-1.5">
        <Label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Category
        </Label>
        <Select value={category} onValueChange={onCategoryChange}>
          <SelectTrigger className="h-9 w-[140px] text-[13px]">
            <SelectValue placeholder="All" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All</SelectItem>
            {categories.map((c) => (
              <SelectItem key={c} value={c}>
                {c}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-1.5">
        <Label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Action
        </Label>
        <Select value={action} onValueChange={onActionChange}>
          <SelectTrigger className="h-9 w-[140px] text-[13px]">
            <SelectValue placeholder="All" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All</SelectItem>
            {actions.map((a) => (
              <SelectItem key={a} value={a}>
                {a}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-1.5">
        <Label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Correlation ID
        </Label>
        <Input
          placeholder="Filter by ID..."
          value={correlationId}
          onChange={(e) => onCorrelationIdChange(e.target.value)}
          className="h-9 w-[200px] text-[13px]"
        />
      </div>

      <div className="space-y-1.5">
        <Label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Start Date
        </Label>
        <Input
          type="date"
          value={startDate}
          onChange={(e) => onStartDateChange(e.target.value)}
          className="h-9 w-[150px] text-[13px]"
        />
      </div>

      <div className="space-y-1.5">
        <Label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          End Date
        </Label>
        <Input
          type="date"
          value={endDate}
          onChange={(e) => onEndDateChange(e.target.value)}
          className="h-9 w-[150px] text-[13px]"
        />
      </div>

      {hasFilters && (
        <Button
          variant="ghost"
          size="sm"
          onClick={onClear}
          className="text-[13px]"
        >
          <X className="mr-1 h-3 w-3" />
          Clear
        </Button>
      )}
    </div>
  );
}
