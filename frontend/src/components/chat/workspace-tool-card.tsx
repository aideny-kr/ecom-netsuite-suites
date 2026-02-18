"use client";

import { useState, useMemo } from "react";
import {
  ChevronDown,
  FolderTree,
  FileSearch,
  FileCode,
  Wrench,
  Shield,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolCallStep } from "@/lib/types";

interface WorkspaceToolCardProps {
  step: ToolCallStep;
}

function safeParse(str: string): Record<string, unknown> | null {
  try {
    return JSON.parse(str);
  } catch {
    return null;
  }
}

function getToolMeta(toolName: string) {
  if (toolName === "workspace_list_files")
    return { icon: FolderTree, label: "List Files" };
  if (toolName === "workspace_read_file")
    return { icon: FileCode, label: "Read File" };
  if (toolName === "workspace_search")
    return { icon: FileSearch, label: "Search Files" };
  if (toolName === "workspace_apply_patch")
    return { icon: Shield, label: "Apply Patch" };
  return { icon: Wrench, label: toolName.replace("workspace_", "") };
}

function ListFilesContent({ result }: { result: Record<string, unknown> }) {
  const files = result.files as Array<Record<string, unknown>> | undefined;
  const count = result.row_count as number | undefined;
  return (
    <div className="space-y-0.5">
      <p className="text-muted-foreground">
        {count ?? files?.length ?? "?"} file(s) found
      </p>
    </div>
  );
}

function ReadFileContent({
  params,
  result,
}: {
  params: Record<string, unknown>;
  result: Record<string, unknown>;
}) {
  const path = (result.path ?? params.file_id ?? "") as string;
  const totalLines = result.total_lines as number | undefined;
  const truncated = result.truncated as boolean | undefined;
  const content = (result.content as string) || "";
  const preview = content.split("\n").slice(0, 3).join("\n");

  return (
    <div className="space-y-1">
      <p>
        <code className="rounded bg-muted px-1 py-0.5 text-[11px] font-mono">
          {path}
        </code>
        {totalLines != null && (
          <span className="ml-1 text-muted-foreground">
            ({totalLines} lines{truncated ? ", truncated" : ""})
          </span>
        )}
      </p>
      {preview && (
        <pre className="overflow-x-auto rounded bg-muted/50 p-1.5 text-[10px] leading-[1.5] font-mono max-h-[60px] overflow-hidden text-muted-foreground">
          {preview}
        </pre>
      )}
    </div>
  );
}

function SearchContent({
  params,
  result,
}: {
  params: Record<string, unknown>;
  result: Record<string, unknown>;
}) {
  const query = params.query as string | undefined;
  const results = result.results as Array<Record<string, unknown>> | undefined;
  const count = result.row_count as number | undefined;

  return (
    <div className="space-y-0.5">
      {query && (
        <p>
          Query: <code className="rounded bg-muted px-1 py-0.5 text-[11px]">{query}</code>
        </p>
      )}
      <p className="text-muted-foreground">
        {count ?? results?.length ?? 0} result(s)
      </p>
      {results && results.length > 0 && (
        <div className="space-y-0.5">
          {results.slice(0, 3).map((r, i) => (
            <p key={i} className="truncate text-muted-foreground">
              {(r.path as string) || (r.file_id as string) || ""}
              {r.line_number != null && `:${r.line_number}`}
            </p>
          ))}
          {results.length > 3 && (
            <p className="text-muted-foreground/60">
              ...and {results.length - 3} more
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ApplyPatchContent({ result }: { result: Record<string, unknown> }) {
  const status = result.status as string | undefined;
  const changesetId = result.changeset_id as string | undefined;
  return (
    <div className="space-y-0.5">
      {changesetId && (
        <p>
          Changeset:{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-[11px] font-mono">
            {changesetId.slice(0, 8)}...
          </code>
        </p>
      )}
      {status && <p className="text-muted-foreground">Status: {status}</p>}
    </div>
  );
}

export function WorkspaceToolCard({ step }: WorkspaceToolCardProps) {
  const [open, setOpen] = useState(false);
  const { icon: Icon, label } = getToolMeta(step.tool);
  const result = useMemo(
    () => safeParse(step.result_summary),
    [step.result_summary],
  );

  const renderContent = () => {
    if (!result) {
      return (
        <p className="text-muted-foreground break-all">{step.result_summary}</p>
      );
    }

    if (result.error) {
      return (
        <p className="text-destructive">{result.error as string}</p>
      );
    }

    switch (step.tool) {
      case "workspace_list_files":
        return <ListFilesContent result={result} />;
      case "workspace_read_file":
        return <ReadFileContent params={step.params} result={result} />;
      case "workspace_search":
        return <SearchContent params={step.params} result={result} />;
      case "workspace_apply_patch":
        return <ApplyPatchContent result={result} />;
      default:
        return (
          <p className="text-muted-foreground break-all">
            {step.result_summary.slice(0, 200)}
          </p>
        );
    }
  };

  return (
    <div className="rounded-lg border bg-background/80 text-[12px]">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 transition-colors hover:bg-accent/50"
      >
        <ChevronDown
          className={cn(
            "h-3 w-3 shrink-0 text-muted-foreground transition-transform duration-200",
            !open && "-rotate-90",
          )}
        />
        <Icon className="h-3 w-3 shrink-0 text-muted-foreground" />
        <span className="font-medium">{label}</span>
        {/* Show compact summary in collapsed state */}
        {!open && result && !result.error && (
          <span className="ml-1 truncate text-muted-foreground">
            {step.tool === "workspace_list_files" &&
              `${(result.row_count as number) ?? "?"} files`}
            {step.tool === "workspace_read_file" &&
              `${(result.path as string) ?? ""}`}
            {step.tool === "workspace_search" &&
              `${(result.row_count as number) ?? 0} results`}
          </span>
        )}
        <span className="ml-auto shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
          {step.duration_ms}ms
        </span>
      </button>
      <div
        className={cn(
          "overflow-hidden transition-all duration-200",
          open ? "max-h-[500px]" : "max-h-0",
        )}
      >
        <div className="border-t px-3 py-2">{renderContent()}</div>
      </div>
    </div>
  );
}
