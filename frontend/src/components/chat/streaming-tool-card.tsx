"use client";

import { cn } from "@/lib/utils";
import type { StreamingToolCall } from "@/lib/types";
import { useState } from "react";
import {
  Database,
  Search,
  FileText,
  Table,
  BookOpen,
  Globe,
  Code2,
  Wrench,
  Check,
  X,
  ChevronDown,
  ChevronUp,
  Loader2,
} from "lucide-react";

const TOOL_DISPLAY: Record<string, { label: string; Icon: typeof Database }> = {
  netsuite_suiteql: { label: "SuiteQL Query", Icon: Database },
  ext__ns_runCustomSuiteQL: { label: "SuiteQL Query", Icon: Database },
  bigquery_sql: { label: "BigQuery Query", Icon: Database },
  bigquery_schema: { label: "Schema Discovery", Icon: Search },
  bigquery_cost_estimate: { label: "Cost Estimate", Icon: FileText },
  netsuite_financial_report: { label: "Financial Report", Icon: FileText },
  pivot_query_result: { label: "Pivot Table", Icon: Table },
  rag_search: { label: "Knowledge Search", Icon: BookOpen },
  web_search: { label: "Web Search", Icon: Globe },
  netsuite_get_metadata: { label: "Schema Lookup", Icon: Search },
  workspace_read_file: { label: "Read File", Icon: Code2 },
  workspace_search: { label: "Code Search", Icon: Search },
  workspace_propose_patch: { label: "Propose Change", Icon: Code2 },
};

function getToolDisplay(toolName: string) {
  // Check exact match first
  if (TOOL_DISPLAY[toolName]) return TOOL_DISPLAY[toolName];
  // Strip ext__<uuid>__ prefix for MCP tools (UUID contains hex + underscores)
  const stripped = toolName.replace(/^ext__[a-f0-9]+__/, "");
  if (TOOL_DISPLAY[stripped]) return TOOL_DISPLAY[stripped];
  // Match by suffix — e.g. "ns_runCustomSuiteQL" contains "SuiteQL"
  const lower = stripped.toLowerCase();
  if (lower.includes("suiteql")) return { label: "SuiteQL Query", Icon: Database };
  if (lower.includes("report")) return { label: "Report", Icon: FileText };
  if (lower.includes("search")) return { label: "Saved Search", Icon: Search };
  if (lower.includes("record")) return { label: "Record Operation", Icon: FileText };
  if (lower.includes("metadata")) return { label: "Schema Lookup", Icon: Search };
  return { label: stripped.replace(/^ns_/, "").replace(/_/g, " "), Icon: Wrench };
}

function formatInput(toolInput: Record<string, unknown>): string {
  // Show the most relevant field as preview
  const query = toolInput.query || toolInput.search_query || toolInput.file_path || toolInput.command;
  if (typeof query === "string") {
    return query.length > 120 ? query.slice(0, 120) + "..." : query;
  }
  return JSON.stringify(toolInput).slice(0, 120) + "...";
}

interface StreamingToolCardProps {
  tool: StreamingToolCall;
  isTerminal?: boolean;
}

export function StreamingToolCard({ tool, isTerminal = false }: StreamingToolCardProps) {
  const [expanded, setExpanded] = useState(false);
  const { label, Icon } = getToolDisplay(tool.tool_name);
  const isRunning = tool.status === "running";
  const isError = tool.status === "error";

  return (
    <div
      data-testid="streaming-tool-card"
      className={cn(
        "rounded-lg border overflow-hidden transition-all duration-200",
        isRunning && "border-primary/30 bg-primary/[0.03]",
        tool.status === "complete" && "border-emerald-500/30 bg-emerald-500/[0.03]",
        isError && "border-red-500/30 bg-red-500/[0.03]",
        isTerminal && "rounded-sm",
      )}
    >
      {/* Header */}
      <button
        onClick={() => setExpanded(v => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {/* Status indicator */}
        {isRunning ? (
          <Loader2 className={cn("h-3.5 w-3.5 animate-spin", isTerminal ? "text-[var(--chat-accent)]" : "text-primary")} />
        ) : isError ? (
          <X className="h-3.5 w-3.5 text-red-500" />
        ) : (
          <Check className="h-3.5 w-3.5 text-emerald-500" />
        )}

        {/* Tool icon + name */}
        <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-[12px] font-medium text-foreground">{label}</span>

        {/* Duration badge */}
        {tool.duration_ms != null && (
          <span className="ml-auto text-[10px] tabular-nums text-muted-foreground">
            {tool.duration_ms < 1000 ? `${tool.duration_ms}ms` : `${(tool.duration_ms / 1000).toFixed(1)}s`}
          </span>
        )}

        {/* Expand chevron */}
        {expanded ? (
          <ChevronUp className="h-3 w-3 text-muted-foreground" />
        ) : (
          <ChevronDown className="h-3 w-3 text-muted-foreground" />
        )}
      </button>

      {/* Expandable content */}
      {expanded && (
        <div className="border-t border-border/30 px-3 py-2 text-[11px]">
          {/* Input preview */}
          <pre className="whitespace-pre-wrap break-all text-muted-foreground font-mono">
            {formatInput(tool.tool_input)}
          </pre>
          {/* Result summary */}
          {tool.result_summary && (
            <p className={cn(
              "mt-1.5 font-medium",
              isError ? "text-red-400" : "text-emerald-400",
            )}>
              {tool.result_summary}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
