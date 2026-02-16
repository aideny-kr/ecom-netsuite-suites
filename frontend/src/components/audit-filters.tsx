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
    <div className="flex flex-wrap items-end gap-4">
      <div className="space-y-1">
        <Label className="text-xs">Category</Label>
        <Select value={category} onValueChange={onCategoryChange}>
          <SelectTrigger className="h-9 w-[140px]">
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

      <div className="space-y-1">
        <Label className="text-xs">Action</Label>
        <Select value={action} onValueChange={onActionChange}>
          <SelectTrigger className="h-9 w-[140px]">
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

      <div className="space-y-1">
        <Label className="text-xs">Correlation ID</Label>
        <Input
          placeholder="Filter by ID..."
          value={correlationId}
          onChange={(e) => onCorrelationIdChange(e.target.value)}
          className="h-9 w-[200px]"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-xs">Start Date</Label>
        <Input
          type="date"
          value={startDate}
          onChange={(e) => onStartDateChange(e.target.value)}
          className="h-9 w-[150px]"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-xs">End Date</Label>
        <Input
          type="date"
          value={endDate}
          onChange={(e) => onEndDateChange(e.target.value)}
          className="h-9 w-[150px]"
        />
      </div>

      {hasFilters && (
        <Button variant="ghost" size="sm" onClick={onClear}>
          <X className="mr-1 h-3 w-3" />
          Clear
        </Button>
      )}
    </div>
  );
}
