"use client";

import { useCallback, useMemo, useState } from "react";
import { Check, ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ResultCardColumn, ResultCardData } from "@/lib/chat-stream";
import { useExcelExport } from "@/hooks/use-excel-export";

// ---------------------------------------------------------------------------
// Formatting — display only; every figure arrives computed from the server.
// ---------------------------------------------------------------------------

function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) return Number(value);
  return null;
}

function currencyFormatter(currency: string | null | undefined, digits = 2) {
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency || "USD",
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  } catch {
    return new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
}

export function formatCardValue(value: unknown, column: Pick<ResultCardColumn, "format" | "currency">): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = toNumber(value);
  switch (column.format) {
    case "integer":
      return n === null ? String(value) : Math.round(n).toLocaleString("en-US");
    case "number":
      return n === null ? String(value) : n.toLocaleString("en-US", { maximumFractionDigits: 2 });
    case "currency":
      return n === null ? String(value) : currencyFormatter(column.currency).format(n);
    case "percent":
      return n === null ? String(value) : `${n.toFixed(1)}%`;
    case "delta":
      if (n === null) return String(value);
      if (n === 0) return "—";
      return `${n > 0 ? "+" : "−"}${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
    default:
      return String(value);
  }
}

function formatTile(value: number, format: string, currency?: string | null): { main: string; exact?: string } {
  if (format === "currency") {
    const exact = `${currencyFormatter(currency).format(value)} ${currency || "USD"}`;
    if (Math.abs(value) >= 1_000_000) {
      const compact = currencyFormatter(currency, 2).format(value / 1_000_000);
      return { main: `${compact}M`, exact };
    }
    return { main: currencyFormatter(currency).format(value) };
  }
  return { main: formatCardValue(value, { format: format as ResultCardColumn["format"], currency }) };
}

export function formatAsOf(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "shortGeneric",
  });
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

export function ResultCardTiles({ card }: { card: ResultCardData }) {
  if (!card.tiles || card.tiles.length === 0) return null;
  return (
    <div
      data-testid="result-card-tiles"
      className="grid gap-3"
      style={{ gridTemplateColumns: `repeat(${Math.min(card.tiles.length, 4)}, minmax(0, 1fr))` }}
    >
      {card.tiles.map((tile) => {
        const { main, exact } = formatTile(tile.value, tile.format, tile.currency);
        return (
          <div key={tile.label} className="rounded-[10px] border border-border bg-card px-4 py-3.5">
            <div className="text-[12px] uppercase tracking-[0.04em] text-muted-foreground">{tile.label}</div>
            <div className="text-[26px] font-semibold leading-tight tabular-nums text-foreground">{main}</div>
            {exact && <div className="text-[12px] tabular-nums text-muted-foreground">{exact}</div>}
          </div>
        );
      })}
    </div>
  );
}

function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </svg>
  );
}

function gridTemplate(card: ResultCardData, withShare: boolean): string {
  if (card.kind === "comparison") {
    return ["2fr", ...card.columns.slice(1).map(() => "1fr")].join(" ");
  }
  const parts: string[] = card.columns.map((c, i) => (i === 0 ? "2.2fr" : c.format === "currency" ? "1.6fr" : "1fr"));
  if (withShare) parts.push("2fr");
  return parts.join(" ");
}

function exportRows(card: ResultCardData): { columns: string[]; rows: unknown[][] } {
  const columns = card.columns.map((c) => (c.group && card.kind === "comparison" ? `${c.group} · ${c.label}` : c.label));
  return { columns, rows: card.rows };
}

function CardActions({ card }: { card: ResultCardData }) {
  const [copied, setCopied] = useState(false);
  const { exportToExcel, isExporting } = useExcelExport();
  const fileTitle = `${card.title.replace(/[^\w]+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "results"}`;

  const handleCopy = useCallback(() => {
    const { columns, rows } = exportRows(card);
    const body = rows.map((row) => row.map((v) => v ?? "").join("\t")).join("\n");
    navigator.clipboard.writeText(`${columns.join("\t")}\n${body}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [card]);

  const handleCsv = useCallback(() => {
    const { columns, rows } = exportRows(card);
    const escape = (v: unknown) => {
      const s = String(v ?? "");
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const csv = [columns.map(escape).join(","), ...rows.map((row) => row.map(escape).join(","))].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${fileTitle}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [card, fileTitle]);

  const buttonClass =
    "inline-flex min-h-9 items-center gap-1.5 rounded-md px-2.5 text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";
  return (
    <div className="flex shrink-0 items-center gap-1">
      <button type="button" onClick={handleCopy} className={buttonClass} title="Copy (tab-separated for Excel)">
        {copied && <Check className="h-3.5 w-3.5 text-emerald-500" />}
        {copied ? "Copied" : "Copy"}
      </button>
      <button type="button" onClick={handleCsv} className={buttonClass} title="Download CSV">
        CSV
      </button>
      <button
        type="button"
        disabled={isExporting}
        onClick={() => {
          const { columns, rows } = exportRows(card);
          exportToExcel({ columns, rows: rows as unknown[][], title: fileTitle });
        }}
        className={cn(buttonClass, "disabled:opacity-50")}
        title="Export as Excel"
      >
        {isExporting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
        Excel
      </button>
    </div>
  );
}

function QueryRows({ card }: { card: ResultCardData }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!card.queries || card.queries.length === 0) return null;
  return (
    <div className="border-b border-border">
      <div className="flex flex-wrap bg-muted/40">
        {card.queries.map((query, index) => (
          <button
            key={query.label}
            type="button"
            data-testid="result-card-query-toggle"
            aria-expanded={open === index}
            onClick={() => setOpen((current) => (current === index ? null : index))}
            className="flex min-h-10 items-center gap-2 px-[18px] py-2 text-left text-[13px] text-muted-foreground transition-colors hover:text-foreground"
          >
            {open === index ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            <span>{query.label}</span>
            {card.queries.length === 1 && (
              <span className="text-muted-foreground/70">· {open === index ? "Hide" : "Show query"}</span>
            )}
          </button>
        ))}
      </div>
      {open !== null && card.queries[open] && (
        <pre className="m-0 overflow-x-auto whitespace-pre-wrap border-t border-border bg-background/60 px-[18px] py-3.5 font-mono text-[12px] leading-relaxed text-foreground/85">
          {card.queries[open].text}
        </pre>
      )}
    </div>
  );
}

function CardTable({ card }: { card: ResultCardData }) {
  const [expanded, setExpanded] = useState(false);
  const inlineCheck = card.kind !== "comparison" && card.check?.status === "ok" && !!card.totals;
  const withShare = !!card.share;
  const template = gridTemplate(card, withShare);
  const rowCount = card.rows.length;
  const visible = expanded ? rowCount : Math.min(card.top_n || rowCount, rowCount);
  const shareMax = useMemo(() => (card.share ? Math.max(...card.share.values, 0.0001) : 1), [card.share]);

  const groups = useMemo(() => {
    if (card.kind !== "comparison") return null;
    const spans: { label: string; span: number }[] = [];
    card.columns.slice(1).forEach((column) => {
      const last = spans[spans.length - 1];
      if (last && last.label === column.group) last.span += 1;
      else spans.push({ label: column.group || "", span: 1 });
    });
    return spans;
  }, [card]);

  const headClass = "text-[12px] uppercase tracking-[0.04em] text-muted-foreground";
  return (
    <div className="max-h-[640px] overflow-auto scrollbar-thin">
      {groups && (
        <div className={cn("grid px-[18px] pt-2.5", headClass)} style={{ gridTemplateColumns: template }}>
          <div />
          {groups.map((group, index) => (
            <div
              key={`${group.label}-${index}`}
              className="mx-2 border-b border-border pb-1 text-center"
              style={{ gridColumn: `span ${group.span}` }}
            >
              {group.label}
            </div>
          ))}
        </div>
      )}
      <div
        className={cn("grid border-b border-border px-[18px]", groups ? "pb-2.5 pt-1.5" : "py-2.5", headClass)}
        style={{ gridTemplateColumns: template }}
      >
        {card.columns.map((column) => (
          <div key={column.key} className={column.align === "right" ? "text-right" : ""}>
            {column.label}
          </div>
        ))}
        {withShare && <div className="pl-6">{card.share!.label}</div>}
      </div>

      {card.rows.slice(0, visible).map((row, rowIndex) => {
        const flag = card.row_flags?.[rowIndex] ?? null;
        return (
          <div
            key={rowIndex}
            data-testid="result-card-row"
            data-flag={flag ?? undefined}
            className={cn(
              "grid items-center border-b border-border/60 px-[18px] py-2.5 text-[15px] leading-normal tabular-nums text-foreground",
              flag && "bg-amber-400/[0.07]",
            )}
            style={{ gridTemplateColumns: template }}
          >
            {card.columns.map((column, columnIndex) => {
              const value = row[columnIndex];
              const isDelta = column.format === "delta";
              const nonZero = isDelta && toNumber(value) !== 0 && value !== null;
              return (
                <div
                  key={column.key}
                  className={cn(
                    column.align === "right" && "text-right",
                    isDelta && (nonZero ? "font-semibold text-amber-500 dark:text-amber-400" : "text-muted-foreground/60"),
                  )}
                >
                  {formatCardValue(value, column)}
                </div>
              );
            })}
            {withShare && (
              <div className="flex items-center gap-2.5 pl-6">
                <div className="h-1.5 flex-1 overflow-hidden rounded-[3px] bg-muted">
                  <div
                    className="h-1.5 rounded-[3px] bg-blue-500/80"
                    style={{ width: `${(card.share!.values[rowIndex] / shareMax) * 100}%` }}
                  />
                </div>
                <div className="w-12 text-right text-[13px] text-foreground/80">
                  {card.share!.values[rowIndex].toFixed(1)}%
                </div>
              </div>
            )}
          </div>
        );
      })}

      {rowCount > (card.top_n || rowCount) && (
        <button
          type="button"
          data-testid="result-card-more"
          onClick={() => setExpanded((v) => !v)}
          className="w-full border-b border-border/60 px-[18px] py-2.5 text-left text-[13px] text-blue-500 transition-colors hover:text-blue-400 dark:text-blue-300"
        >
          {expanded ? card.less_label || "Show fewer" : card.more_label || `Show ${rowCount - card.top_n} more`}
        </button>
      )}

      {card.totals && (
        <div
          data-testid="result-card-total"
          className="grid items-center bg-muted/50 px-[18px] py-3 text-[15px] font-semibold leading-normal tabular-nums text-foreground"
          style={{ gridTemplateColumns: template }}
        >
          {card.columns.map((column, index) => (
            <div
              key={column.key}
              className={cn(
                column.align === "right" && "text-right",
                column.format === "delta" && toNumber(card.totals![index]) && "text-amber-500 dark:text-amber-400",
              )}
            >
              {index === 0 ? (
                <span className="inline-flex items-center gap-2">
                  {card.totals_label || "Total"}
                  {inlineCheck && (
                    <span title={card.check!.text} aria-label={card.check!.text} data-testid="result-card-total-check">
                      <CheckIcon className="text-teal-500 dark:text-teal-400" />
                    </span>
                  )}
                </span>
              ) : (
                formatCardValue(card.totals![index], column)
              )}
            </div>
          ))}
          {withShare && <div className="pl-6 font-medium text-foreground/80">100%</div>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

export function ResultCard({ card }: { card: ResultCardData }) {
  const [open, setOpen] = useState(!card.collapsed);
  const asOf = formatAsOf(card.as_of);
  const subtitle = (
    card.kind === "comparison"
      ? [card.subtitle, asOf ? `both as of ${asOf}` : null]
      : [card.source, card.subtitle, asOf ? `as of ${asOf}` : null]
  )
    .filter(Boolean)
    .join(" · ");

  if (card.collapsed) {
    return (
      <div data-testid="result-card" data-kind={card.kind} className="overflow-hidden rounded-xl border border-border bg-card">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex min-h-11 w-full items-center gap-2 px-[18px] py-2.5 text-left text-[14px] text-foreground/90"
        >
          {open ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 text-muted-foreground" />}
          <span className="font-medium">{card.title}</span>
          {card.collapsed_note && <span className="text-muted-foreground">· {card.collapsed_note}</span>}
        </button>
        {open && (
          <div className="border-t border-border">
            <CardTable card={card} />
            {card.no_total_reason && (
              <div className="px-[18px] py-2.5 text-[13px] text-muted-foreground">
                No total: {card.no_total_reason}.
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div data-testid="result-card" data-kind={card.kind} className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="flex items-center justify-between gap-4 border-b border-border px-[18px] py-3.5">
        <div className="min-w-0">
          <div className="text-[15px] font-semibold text-foreground">{card.title}</div>
          {subtitle && <div className="text-[13px] text-muted-foreground">{subtitle}</div>}
        </div>
        <CardActions card={card} />
      </div>
      <QueryRows card={card} />
      <CardTable card={card} />
      {card.check && !(card.kind !== "comparison" && card.check.status === "ok" && card.totals) && (
        <div
          data-testid="result-card-check"
          className="flex items-center gap-2 border-t border-border px-[18px] py-3 text-[13px] text-muted-foreground"
        >
          {card.check.status === "ok" ? (
            <CheckIcon className="shrink-0 text-teal-500 dark:text-teal-400" />
          ) : (
            <span className="shrink-0 text-amber-500">⚠</span>
          )}
          <span>{card.check.text}</span>
        </div>
      )}
      {card.scope && (
        <div data-testid="result-card-scope" className="border-t border-border px-[18px] py-3 text-[13px] text-muted-foreground">
          <span className="font-medium text-foreground/80">Scope</span> · {card.scope}
        </div>
      )}
      {card.truncated && (
        <div className="border-t border-border px-[18px] py-2 text-[12px] text-muted-foreground">
          Partial result: not every row is shown.
        </div>
      )}
    </div>
  );
}

/** A comparison card's computed headline and detail, shown above the answer text. */
export function ResultCardHeadline({ card, mergeDetail = false }: { card: ResultCardData; mergeDetail?: boolean }) {
  if (!card.headline) return null;
  return (
    <div className="flex max-w-[780px] flex-col gap-1.5">
      <p data-testid="result-card-headline" className="m-0 text-[18px] font-semibold leading-snug text-foreground">
        {card.headline}
      </p>
      {card.detail && !mergeDetail && <p className="m-0 text-[15px] leading-relaxed text-foreground/80">{card.detail}</p>}
    </div>
  );
}
