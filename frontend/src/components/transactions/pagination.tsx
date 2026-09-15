"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";

export function Pagination({
  offset,
  size,
  total,
  setOffset,
  setSize,
  label = "rows",
  disabled = false,
}: {
  offset: number;
  size: number;
  total?: number;
  setOffset: (value: number) => void;
  setSize: (value: number) => void;
  label?: string;
  disabled?: boolean;
}) {
  const pages = Math.max(1, Math.ceil((total || 0) / size));
  const page = Math.floor(offset / size) + 1;
  useEffect(() => {
    if (total !== undefined && offset > 0 && offset >= total)
      setOffset((pages - 1) * size);
  }, [offset, total, pages, size, setOffset]);
  const numbers = Array.from(new Set([1, page - 1, page, page + 1, pages]))
    .filter((n) => n >= 1 && n <= pages)
    .sort((a, b) => a - b);
  const unavailable = disabled || total === undefined;
  return (
    <nav
      aria-label={`${label} pagination`}
      className="flex flex-wrap items-center justify-between gap-3 text-[13px]"
    >
      <span className="text-muted-foreground">
        {total === undefined
          ? "Loading row count…"
          : `Showing ${total ? offset + 1 : 0}–${Math.min(offset + size, total)} of ${total} ${label}`}
      </span>
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-muted-foreground">
          Rows per page
          <select
            aria-label={`${label} per page`}
            className="h-9 rounded-md border bg-background px-2"
            value={size}
            disabled={disabled}
            onChange={(e) => {
              setSize(Number(e.target.value));
              setOffset(0);
            }}
          >
            {[50, 100, 500].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="outline"
          disabled={unavailable || !offset}
          onClick={() => setOffset(Math.max(0, offset - size))}
        >
          Previous
        </Button>
        {numbers.map((n, index) => (
          <span className="flex items-center gap-2" key={n}>
            {index > 0 && n - numbers[index - 1] > 1 && (
              <span aria-hidden="true">…</span>
            )}
            <Button
              variant="outline"
              aria-label={`Page ${n}`}
              aria-current={n === page ? "page" : undefined}
              className={n === page ? "border-primary text-primary" : ""}
              disabled={unavailable}
              onClick={() => setOffset((n - 1) * size)}
            >
              {n}
            </Button>
          </span>
        ))}
        <Button
          variant="outline"
          disabled={unavailable || page >= pages}
          onClick={() => setOffset(offset + size)}
        >
          Next
        </Button>
      </div>
    </nav>
  );
}
