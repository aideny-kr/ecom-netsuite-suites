"use client";

import { createContext, useContext, useState } from "react";
import { ChevronDown, ChevronRight, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Pieces of the data-answer layout: the lead sentence before the cards, the
 * collapsed SQL block, click-to-ask follow-ups and the sources toggle.
 */

const FOLLOWUPS_FENCE = /```followups[^\n]*\n([\s\S]*?)```/;

/** Remove the model's ```followups block and return its lines as suggestions. */
export function extractFollowups(content: string): { text: string; followups: string[] } {
  const match = content.match(FOLLOWUPS_FENCE);
  if (!match) return { text: content, followups: [] };
  const followups = match[1]
    .split("\n")
    .map((line) => line.replace(/^\s*(?:[-*•]|\d+[.)])\s*/, "").trim())
    .filter((line) => line.length > 0 && line.length <= 80)
    .slice(0, 4);
  return { text: content.replace(match[0], "").replace(/\n{3,}/g, "\n\n").trim(), followups };
}

const RAN_QUERY_LABEL =
  /^[ \t]*(?:\*\*|__)?[ \t]*(?:the[ \t]+)?(?:quer(?:y|ies)|sql|suiteql)[ \t]+(?:i[ \t]+)?(?:ran|run|used|executed)\b[ \t]*(?:\([^)\n]{0,20}\))?[ \t]*:?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*\n+(?=[ \t]*```(?:sql|suiteql))/gim;

/** Drop a "Query I ran (SuiteQL):" label that only introduces a SQL block. */
export function stripRanQueryLabels(content: string): string {
  return content.replace(RAN_QUERY_LABEL, "");
}

/** The answer's opening paragraph, shown before the cards, and the rest. */
export function splitLead(content: string): { lead: string; rest: string } {
  const text = content.trim();
  if (!text) return { lead: "", rest: "" };
  const index = text.indexOf("\n\n");
  const first = index === -1 ? text : text.slice(0, index);
  if (/^(#|\||[-*+] |\d+[.)] |```|>)/.test(first.trim()) || first.includes("\n|")) {
    return { lead: "", rest: text };
  }
  return { lead: first, rest: index === -1 ? "" : text.slice(index + 2).trim() };
}

export type ProseSegment = { kind: "prose"; markdown: string } | { kind: "table"; title: string | null; markdown: string };

/**
 * Split the answer text around result cards into prose and tables. A line that is
 * only bold text, directly above a table, becomes that table's title.
 */
export function proseSegments(content: string): ProseSegment[] {
  // Split on blank lines, but keep a fenced code block (which may contain blank lines
  // and lines starting with "|") in one piece.
  const blocks: string[] = [];
  let open: string[] | null = null;
  for (const part of content.split(/\n{2,}/)) {
    const fences = (part.match(/^\s*(```|~~~)/gm) || []).length;
    if (open) {
      open.push(part);
      if (fences % 2 === 1) {
        blocks.push(open.join("\n\n").trim());
        open = null;
      }
    } else if (fences % 2 === 1) {
      open = [part];
    } else if (part.trim()) {
      blocks.push(part.trim());
    }
  }
  if (open) blocks.push(open.join("\n\n").trim());
  const segments: ProseSegment[] = [];
  let prose: string[] = [];
  const flush = () => {
    if (prose.length) segments.push({ kind: "prose", markdown: prose.join("\n\n") });
    prose = [];
  };
  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i];
    const title = block.match(/^\*\*([^*\n]+?)\*\*:?$/);
    const next = blocks[i + 1];
    if (title && next?.startsWith("|")) {
      flush();
      segments.push({ kind: "table", title: title[1].trim(), markdown: next });
      i += 1;
    } else if (block.startsWith("|") && block.includes("\n|")) {
      flush();
      segments.push({ kind: "table", title: null, markdown: block });
    } else {
      prose.push(block);
    }
  }
  flush();
  return segments;
}

/** True inside an answer whose SQL already sits, collapsed, on a result card. */
export const CollapseSqlContext = createContext(false);

export function useCollapseSql(): boolean {
  return useContext(CollapseSqlContext);
}

export function CollapsedSqlBlock({ code, language }: { code: string; language: string }) {
  const [open, setOpen] = useState(false);
  const label = language === "suiteql" || /\bBUILTIN\.|\btransactionline\b/i.test(code) ? "SuiteQL query" : "SQL query";
  return (
    <div data-testid="collapsed-sql" className="overflow-hidden rounded-xl border border-border/60">
      <div className="flex items-center bg-muted/40">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex min-h-10 flex-1 items-center gap-2 px-4 py-2 text-left text-[13px] text-muted-foreground transition-colors hover:text-foreground"
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <span>{label}</span>
          <span className="text-muted-foreground/70">· {open ? "Hide" : "Show query"}</span>
        </button>
        <button
          type="button"
          onClick={() => navigator.clipboard.writeText(code)}
          className="mr-2 flex min-h-9 items-center gap-1 rounded-md px-2 text-[12px] text-muted-foreground hover:text-foreground"
          aria-label="Copy query"
        >
          <Copy className="h-3 w-3" />
        </button>
      </div>
      {open && (
        <pre className="m-0 overflow-x-auto whitespace-pre-wrap border-t border-border/60 bg-background/60 px-4 py-3 font-mono text-[12px] leading-relaxed text-foreground/85">
          {code}
        </pre>
      )}
    </div>
  );
}

export function FollowUpChips({
  items,
  onPick,
  disabled = false,
}: {
  items: string[];
  onPick?: (text: string) => void;
  disabled?: boolean;
}) {
  if (items.length === 0 || !onPick) return null;
  return (
    <div data-testid="follow-up-chips" className="flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item}
          type="button"
          disabled={disabled}
          onClick={() => onPick(item)}
          className="min-h-9 rounded-full border border-border bg-card px-3.5 py-1.5 text-[13px] text-foreground/85 transition-colors hover:bg-muted disabled:opacity-50"
        >
          {item}
        </button>
      ))}
    </div>
  );
}

export interface SourceItem {
  title: string;
  snippet?: string;
}

export function SourcesToggle({
  sources,
  open,
  onToggle,
}: {
  sources: SourceItem[];
  open: boolean;
  onToggle: () => void;
}) {
  if (sources.length === 0) return null;
  return (
    <button
      type="button"
      data-testid="sources-toggle"
      aria-expanded={open}
      onClick={onToggle}
      className="min-h-9 rounded-md border border-border px-2.5 py-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
    >
      {open ? "Hide sources" : `Sources (${sources.length})`}
    </button>
  );
}

export function SourcesList({ sources }: { sources: SourceItem[] }) {
  return (
    <ul data-testid="sources-list" className={cn("m-0 flex list-none flex-col gap-1 p-0 pl-1 text-[13px] text-muted-foreground")}>
      {sources.map((source, index) => (
        <li key={`${source.title}-${index}`} title={source.snippet}>
          {source.title}
        </li>
      ))}
    </ul>
  );
}
