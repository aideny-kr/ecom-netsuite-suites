"use client";

import { useCallback, useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { FileSpreadsheet, Download, ClipboardCopy, Check, ChevronRight } from "lucide-react";
import type { FinancialReportData } from "@/lib/chat-stream";
import {
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface FinancialReportProps {
  reportType: string;
  period: string;
  columns: string[];
  rows: Record<string, any>[];
  summary: Record<string, any>;
}

interface SectionDef {
  key: string;
  label: string;
  subtotalKey: string;
}

interface ComputedRowDef {
  afterSection: string;
  label: string;
  summaryKey: string;
}

// ---------------------------------------------------------------------------
// Section definitions
// ---------------------------------------------------------------------------

const INCOME_SECTIONS: SectionDef[] = [
  { key: "1-Revenue", label: "Revenue", subtotalKey: "total_revenue" },
  { key: "2-Other Income", label: "Other Income", subtotalKey: "total_other_income" },
  { key: "3-COGS", label: "Cost of Goods Sold", subtotalKey: "total_cogs" },
  { key: "4-Operating Expense", label: "Operating Expenses", subtotalKey: "total_operating_expense" },
  { key: "5-Other Expense", label: "Other Expenses", subtotalKey: "total_other_expense" },
];

const INCOME_COMPUTED: ComputedRowDef[] = [
  { afterSection: "3-COGS", label: "Gross Profit", summaryKey: "gross_profit" },
  { afterSection: "4-Operating Expense", label: "Operating Income", summaryKey: "operating_income" },
  { afterSection: "5-Other Expense", label: "Net Income", summaryKey: "net_income" },
];

const BALANCE_SECTIONS: SectionDef[] = [
  { key: "1-Assets", label: "Assets", subtotalKey: "total_assets" },
  { key: "2-Liabilities", label: "Liabilities", subtotalKey: "total_liabilities" },
  { key: "3-Equity", label: "Equity", subtotalKey: "total_equity" },
];

const BALANCE_COMPUTED: ComputedRowDef[] = [
  { afterSection: "1-Assets", label: "Total Assets", summaryKey: "total_assets" },
  { afterSection: "3-Equity", label: "Total Liabilities & Equity", summaryKey: "total_liabilities_and_equity" },
];

// ---------------------------------------------------------------------------
// Number formatting
// ---------------------------------------------------------------------------

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "decimal",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function formatAmount(value: number | null | undefined): string {
  if (value == null || isNaN(value)) return "--";
  const abs = Math.abs(value);
  const formatted = currencyFormatter.format(abs);
  if (value < 0) return `($${formatted})`;
  return `$${formatted}`;
}

function isNegative(value: number | null | undefined): boolean {
  return value != null && value < 0;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isTrend(reportType: string): boolean {
  return reportType.endsWith("_trend");
}

function isBalanceSheet(reportType: string): boolean {
  return reportType.toLowerCase().includes("balance_sheet");
}

function getSections(reportType: string): {
  sections: SectionDef[];
  computed: ComputedRowDef[];
} {
  if (isBalanceSheet(reportType)) {
    return { sections: BALANCE_SECTIONS, computed: BALANCE_COMPUTED };
  }
  return { sections: INCOME_SECTIONS, computed: INCOME_COMPUTED };
}

function reportLabel(reportType: string): string {
  const base = reportType.replace(/_trend$/, "");
  const words = base.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  return isTrend(reportType) ? `${words} (Trend)` : words;
}

/** Group rows by section key, preserving order. */
function groupBySection(rows: Record<string, any>[]): Map<string, Record<string, any>[]> {
  const map = new Map<string, Record<string, any>[]>();
  for (const row of rows) {
    const section = (row.section as string) ?? "Uncategorized";
    if (!map.has(section)) map.set(section, []);
    map.get(section)!.push(row);
  }
  return map;
}

/**
 * Pivot trend rows: from flat (one row per account+period) to
 * one row per account with a column per period.
 */
function pivotTrendRows(
  rows: Record<string, any>[],
): { pivoted: Record<string, any>[]; periods: string[] } {
  const periodSet = new Set<string>();
  const accountMap = new Map<string, Record<string, any>>();

  for (const row of rows) {
    const key = `${row.acctnumber}::${row.acctname}::${row.section}`;
    const period = row.periodname as string;
    periodSet.add(period);

    if (!accountMap.has(key)) {
      accountMap.set(key, {
        acctnumber: row.acctnumber,
        acctname: row.acctname,
        section: row.section,
      });
    }
    accountMap.get(key)![period] = Number(row.amount) || 0;
  }

  const periods = Array.from(periodSet);
  const pivoted = Array.from(accountMap.values());
  return { pivoted, periods };
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function SectionHeader({
  label,
  colSpan,
  collapsed,
  onToggle,
}: {
  label: string;
  colSpan: number;
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  return (
    <TableRow className="hover:bg-transparent">
      <TableCell
        colSpan={colSpan}
        className="bg-muted/30 px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground cursor-pointer select-none"
        onClick={onToggle}
      >
        <span className="inline-flex items-center gap-1">
          <ChevronRight
            className={cn(
              "h-3 w-3 transition-transform",
              !collapsed && "rotate-90",
            )}
          />
          {label}
        </span>
      </TableCell>
    </TableRow>
  );
}

function SubtotalRow({
  label,
  amounts,
  colSpan,
  isGrandTotal,
}: {
  label: string;
  amounts: (number | null | undefined)[];
  colSpan: number;
  isGrandTotal?: boolean;
}) {
  const base = isGrandTotal
    ? "font-bold bg-muted/50 border-t-2 hover:bg-muted/50"
    : "font-semibold border-t hover:bg-transparent";

  return (
    <TableRow className={cn(base)}>
      {/* Empty acct# cell */}
      <TableCell className="px-3 py-1.5 text-[13px]" />
      {/* Label */}
      <TableCell className="px-3 py-1.5 text-[13px] font-semibold">{label}</TableCell>
      {/* Amount cells */}
      {amounts.map((amt, i) => (
        <TableCell
          key={i}
          className={cn(
            "px-3 py-1.5 text-[13px] text-right tabular-nums",
            isNegative(amt) && "text-red-400 dark:text-red-400 text-red-600",
          )}
        >
          {formatAmount(amt ?? null)}
        </TableCell>
      ))}
      {/* Fill remaining columns if needed */}
      {amounts.length === 0 &&
        Array.from({ length: Math.max(0, colSpan - 2) }).map((_, i) => (
          <TableCell key={`empty-${i}`} className="px-3 py-1.5" />
        ))}
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Single-period table
// ---------------------------------------------------------------------------

function SinglePeriodTable({
  rows,
  summary,
  sections,
  computed,
  collapsedSections,
  onToggle,
}: {
  rows: Record<string, any>[];
  summary: Record<string, any>;
  sections: SectionDef[];
  computed: ComputedRowDef[];
  collapsedSections: Set<string>;
  onToggle: (key: string) => void;
}) {
  const grouped = useMemo(() => groupBySection(rows), [rows]);
  const colSpan = 3;

  return (
    <table className="w-full caption-bottom text-sm">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="sticky top-0 z-10 bg-muted/50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur w-[80px]">
            Acct #
          </TableHead>
          <TableHead className="sticky top-0 z-10 bg-muted/50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur">
            Account Name
          </TableHead>
          <TableHead className="sticky top-0 z-10 bg-muted/50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur text-right w-[140px]">
            Amount
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sections.map((sec) => {
          const sectionRows = grouped.get(sec.key) ?? [];
          const subtotal = summary[sec.subtotalKey] as number | undefined;
          const computedAfter = computed.filter((c) => c.afterSection === sec.key);

          return (
            <SectionBlock
              key={sec.key}
              sectionKey={sec.key}
              label={sec.label}
              colSpan={colSpan}
              collapsedSections={collapsedSections}
              onToggle={onToggle}
            >
              {sectionRows.map((row, idx) => {
                const amt = Number(row.amount) || 0;
                return (
                  <TableRow key={`${sec.key}-${idx}`} className="hover:bg-muted/20">
                    <TableCell className="px-3 py-1.5 text-[13px] text-muted-foreground tabular-nums">
                      {row.acctnumber ?? ""}
                    </TableCell>
                    <TableCell className="px-3 py-1.5 text-[13px]">
                      {row.acctname ?? ""}
                    </TableCell>
                    <TableCell
                      className={cn(
                        "px-3 py-1.5 text-[13px] text-right tabular-nums",
                        isNegative(amt) && "text-red-400 dark:text-red-400 text-red-600",
                      )}
                    >
                      {formatAmount(amt)}
                    </TableCell>
                  </TableRow>
                );
              })}
              {subtotal != null && (
                <SubtotalRow
                  label={`Total ${sec.label}`}
                  amounts={[subtotal]}
                  colSpan={colSpan}
                />
              )}
              {computedAfter.map((cr) => {
                const val = computeSummaryValue(cr, summary);
                return (
                  <SubtotalRow
                    key={cr.summaryKey}
                    label={cr.label}
                    amounts={[val]}
                    colSpan={colSpan}
                    isGrandTotal
                  />
                );
              })}
            </SectionBlock>
          );
        })}
      </TableBody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Trend / multi-period table
// ---------------------------------------------------------------------------

function TrendTable({
  rows,
  summary,
  sections,
  computed,
  collapsedSections,
  onToggle,
}: {
  rows: Record<string, any>[];
  summary: Record<string, any>;
  sections: SectionDef[];
  computed: ComputedRowDef[];
  collapsedSections: Set<string>;
  onToggle: (key: string) => void;
}) {
  const { pivoted, periods } = useMemo(() => pivotTrendRows(rows), [rows]);
  const grouped = useMemo(() => groupBySection(pivoted), [pivoted]);
  const colSpan = 2 + periods.length;
  const byPeriod = (summary.by_period ?? {}) as Record<string, Record<string, any>>;

  return (
    <table className="w-max min-w-full caption-bottom text-sm">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="sticky top-0 z-10 bg-muted/50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur w-[80px]">
            Acct #
          </TableHead>
          <TableHead className="sticky top-0 z-10 bg-muted/50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur">
            Account Name
          </TableHead>
          {periods.map((p) => (
            <TableHead
              key={p}
              className="sticky top-0 z-10 bg-muted/50 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur text-right w-[130px]"
            >
              {p}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {sections.map((sec) => {
          const sectionRows = grouped.get(sec.key) ?? [];
          const computedAfter = computed.filter((c) => c.afterSection === sec.key);

          return (
            <SectionBlock
              key={sec.key}
              sectionKey={sec.key}
              label={sec.label}
              colSpan={colSpan}
              collapsedSections={collapsedSections}
              onToggle={onToggle}
            >
              {sectionRows.map((row, idx) => (
                <TableRow key={`${sec.key}-${idx}`} className="hover:bg-muted/20">
                  <TableCell className="px-3 py-1.5 text-[13px] text-muted-foreground tabular-nums">
                    {row.acctnumber ?? ""}
                  </TableCell>
                  <TableCell className="px-3 py-1.5 text-[13px]">
                    {row.acctname ?? ""}
                  </TableCell>
                  {periods.map((p) => {
                    const amt = (row[p] as number) ?? null;
                    return (
                      <TableCell
                        key={p}
                        className={cn(
                          "px-3 py-1.5 text-[13px] text-right tabular-nums",
                          isNegative(amt) && "text-red-400 dark:text-red-400 text-red-600",
                        )}
                      >
                        {formatAmount(amt)}
                      </TableCell>
                    );
                  })}
                </TableRow>
              ))}
              {/* Section subtotal per period */}
              <SubtotalRow
                label={`Total ${sec.label}`}
                amounts={periods.map((p) => (byPeriod[p]?.[sec.subtotalKey] as number) ?? null)}
                colSpan={colSpan}
              />
              {/* Computed grand totals */}
              {computedAfter.map((cr) => (
                <SubtotalRow
                  key={cr.summaryKey}
                  label={cr.label}
                  amounts={periods.map((p) => computeSummaryValueForPeriod(cr, byPeriod[p] ?? {}))}
                  colSpan={colSpan}
                  isGrandTotal
                />
              ))}
            </SectionBlock>
          );
        })}
      </TableBody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Computed value helpers
// ---------------------------------------------------------------------------

function computeSummaryValue(
  cr: ComputedRowDef,
  summary: Record<string, any>,
): number | null {
  const val = summary[cr.summaryKey];
  if (val != null) return Number(val);

  // Fallback computation
  if (cr.summaryKey === "gross_profit") {
    const rev = Number(summary.total_revenue ?? 0);
    const cogs = Number(summary.total_cogs ?? 0);
    return rev - cogs;
  }
  if (cr.summaryKey === "operating_income") {
    const gp = Number(summary.gross_profit ?? computeSummaryValue(
      { afterSection: "", label: "", summaryKey: "gross_profit" },
      summary,
    ) ?? 0);
    const opex = Number(summary.total_operating_expense ?? 0);
    return gp - opex;
  }
  return null;
}

function computeSummaryValueForPeriod(
  cr: ComputedRowDef,
  periodSummary: Record<string, any>,
): number | null {
  return computeSummaryValue(cr, periodSummary);
}

// ---------------------------------------------------------------------------
// Fragment wrapper (React 18 — no string keys on Fragment)
// ---------------------------------------------------------------------------

function SectionBlock({
  sectionKey,
  label,
  colSpan,
  collapsedSections,
  onToggle,
  children,
}: {
  sectionKey: string;
  label: string;
  colSpan: number;
  collapsedSections: Set<string>;
  onToggle: (key: string) => void;
  children: React.ReactNode;
}) {
  const collapsed = collapsedSections.has(sectionKey);
  return (
    <>
      <SectionHeader
        label={label}
        colSpan={colSpan}
        collapsed={collapsed}
        onToggle={() => onToggle(sectionKey)}
      />
      {!collapsed && children}
    </>
  );
}

// ---------------------------------------------------------------------------
// Export helpers
// ---------------------------------------------------------------------------

function buildTsvRows(
  rows: Record<string, any>[],
  summary: Record<string, any>,
  sections: SectionDef[],
  computed: ComputedRowDef[],
  trend: boolean,
  periods: string[],
): string[][] {
  const lines: string[][] = [];

  // Header
  if (trend) {
    lines.push(["Acct #", "Account Name", ...periods]);
  } else {
    lines.push(["Acct #", "Account Name", "Amount"]);
  }

  const grouped = groupBySection(trend ? pivotTrendRows(rows).pivoted : rows);

  for (const sec of sections) {
    lines.push([sec.label]);
    const sectionRows = grouped.get(sec.key) ?? [];
    for (const row of sectionRows) {
      if (trend) {
        lines.push([
          row.acctnumber ?? "",
          row.acctname ?? "",
          ...periods.map((p) => String(row[p] ?? "")),
        ]);
      } else {
        lines.push([row.acctnumber ?? "", row.acctname ?? "", String(row.amount ?? "")]);
      }
    }
    // Subtotal
    if (trend) {
      const byPeriod = (summary.by_period ?? {}) as Record<string, Record<string, any>>;
      lines.push([
        "",
        `Total ${sec.label}`,
        ...periods.map((p) => String(byPeriod[p]?.[sec.subtotalKey] ?? "")),
      ]);
    } else {
      const subtotal = summary[sec.subtotalKey];
      if (subtotal != null) lines.push(["", `Total ${sec.label}`, String(subtotal)]);
    }
    // Computed
    for (const cr of computed.filter((c) => c.afterSection === sec.key)) {
      if (trend) {
        const byPeriod = (summary.by_period ?? {}) as Record<string, Record<string, any>>;
        lines.push([
          "",
          cr.label,
          ...periods.map((p) => String(computeSummaryValueForPeriod(cr, byPeriod[p] ?? {}) ?? "")),
        ]);
      } else {
        lines.push(["", cr.label, String(computeSummaryValue(cr, summary) ?? "")]);
      }
    }
  }

  return lines;
}

function tsvFromLines(lines: string[][]): string {
  return lines.map((row) => row.join("\t")).join("\n");
}

function csvFromLines(lines: string[][]): string {
  return lines
    .map((row) => row.map((cell) => (cell.includes(",") || cell.includes('"') ? `"${cell.replace(/"/g, '""')}"` : cell)).join(","))
    .join("\n");
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

type FinancialReportUnionProps =
  | FinancialReportProps
  | { data: FinancialReportData };

export function FinancialReport(props: FinancialReportUnionProps) {
  const { reportType, period, columns, rows, summary } =
    "data" in props
      ? {
          reportType: props.data.report_type,
          period: props.data.period,
          columns: props.data.columns,
          rows: props.data.rows,
          summary: props.data.summary,
        }
      : props;

  const { sections, computed } = useMemo(() => getSections(reportType), [reportType]);
  const trend = isTrend(reportType);
  const periods = useMemo(() => (trend ? pivotTrendRows(rows).periods : []), [rows, trend]);
  const [copied, setCopied] = useState(false);
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set());

  const toggleSection = useCallback((key: string) => {
    setCollapsedSections((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const handleCopy = useCallback(() => {
    const lines = buildTsvRows(rows, summary, sections, computed, trend, periods);
    navigator.clipboard.writeText(tsvFromLines(lines));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [rows, summary, sections, computed, trend, periods]);

  const handleDownloadCsv = useCallback(() => {
    const lines = buildTsvRows(rows, summary, sections, computed, trend, periods);
    const csv = csvFromLines(lines);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${reportType}_${period.replace(/[, ]+/g, "_")}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [rows, summary, sections, computed, trend, periods, reportType, period]);

  return (
    <div className="rounded-xl border bg-card shadow-soft overflow-hidden">
      {/* Title bar */}
      <div className="flex items-center gap-2 border-b bg-muted/30 px-4 py-3">
        <FileSpreadsheet className="h-4 w-4 shrink-0 text-primary/70" />
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-semibold text-foreground">
            {reportLabel(reportType)}
          </p>
          <p className="text-[11px] text-muted-foreground">{period}</p>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={handleCopy}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            title="Copy to clipboard (tab-separated for Excel)"
          >
            {copied ? <Check className="h-3 w-3" /> : <ClipboardCopy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            onClick={handleDownloadCsv}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            title="Download as CSV"
          >
            <Download className="h-3 w-3" />
            CSV
          </button>
        </div>
      </div>

      {/* Table container with sticky header scroll */}
      <div className="max-h-[600px] overflow-y-auto scrollbar-thin">
        {trend ? (
          <TrendTable
            rows={rows}
            summary={summary}
            sections={sections}
            computed={computed}
            collapsedSections={collapsedSections}
            onToggle={toggleSection}
          />
        ) : (
          <SinglePeriodTable
            rows={rows}
            summary={summary}
            sections={sections}
            computed={computed}
            collapsedSections={collapsedSections}
            onToggle={toggleSection}
          />
        )}
      </div>
    </div>
  );
}
