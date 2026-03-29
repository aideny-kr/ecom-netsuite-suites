"use client";

import { useState, useMemo } from "react";
import { ChevronRight, File, Folder, FolderOpen, FolderTree, Layers } from "lucide-react";
import { cn } from "@/lib/utils";
import { parseSuiteScriptMetadata, SCRIPT_TYPE_MAP } from "@/lib/suitescript-parser";
import type { ScriptType } from "@/lib/suitescript-parser";
import type { FileTreeNode } from "@/lib/types";

type ViewMode = "folder" | "script-type";

interface FileTreeProps {
  nodes: FileTreeNode[];
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
  defaultView?: ViewMode;
}

/** Display label for each script type group */
const SCRIPT_TYPE_LABELS: Record<string, string> = {
  UserEventScript: "User Event Scripts",
  ClientScript: "Client Scripts",
  ScheduledScript: "Scheduled Scripts",
  MapReduceScript: "Map/Reduce",
  Suitelet: "Suitelets",
  Restlet: "RESTlets",
  WorkflowActionScript: "Workflow Actions",
  BundleInstallationScript: "Bundle Installation",
  MassUpdateScript: "Mass Update",
  Library: "Libraries",
  Other: "Other",
  Unknown: "Untyped",
};

/** Collect all leaf files from a tree recursively */
function collectFiles(nodes: FileTreeNode[]): FileTreeNode[] {
  const files: FileTreeNode[] = [];
  function walk(list: FileTreeNode[]) {
    for (const node of list) {
      if (node.is_directory && node.children) {
        walk(node.children);
      } else if (!node.is_directory) {
        files.push(node);
      }
    }
  }
  walk(nodes);
  return files;
}

/** Group flat files into virtual script-type folders */
function groupByScriptType(nodes: FileTreeNode[]): FileTreeNode[] {
  const files = collectFiles(nodes);
  const groups = new Map<string, FileTreeNode[]>();

  for (const file of files) {
    // Use script_type from backend if available, otherwise detect from path
    let type = file.script_type || "Unknown";
    if (type === "Unknown" || type === "Other") {
      const meta = parseSuiteScriptMetadata(null, file.path);
      if (meta.scriptType !== "Unknown") {
        type = meta.scriptType;
      }
    }
    if (!groups.has(type)) {
      groups.set(type, []);
    }
    groups.get(type)!.push(file);
  }

  // Sort: known types first (in canonical order), Unknown last
  const order: string[] = [
    "UserEventScript", "ClientScript", "ScheduledScript", "MapReduceScript",
    "Suitelet", "Restlet", "MassUpdateScript", "WorkflowActionScript",
    "BundleInstallationScript", "Library", "Other", "Unknown",
  ];

  const entries = Array.from(groups.entries());
  const sorted = entries.sort(([a]: [string, FileTreeNode[]], [b]: [string, FileTreeNode[]]) => {
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
  });

  return sorted.map(([type, typeFiles]: [string, FileTreeNode[]]) => ({
    id: `group-${type}`,
    name: SCRIPT_TYPE_LABELS[type] || type,
    path: `__group__/${type}`,
    is_directory: true,
    script_type: type,
    children: typeFiles.sort((a, b) => a.name.localeCompare(b.name)),
  }));
}

export function FileTree({ nodes, onFileSelect, selectedFileId, defaultView }: FileTreeProps) {
  const [viewMode, setViewMode] = useState<ViewMode>(defaultView || "folder");

  const displayNodes = useMemo(
    () => (viewMode === "script-type" ? groupByScriptType(nodes) : nodes),
    [nodes, viewMode],
  );

  return (
    <div className="text-[12px]" data-testid="file-tree">
      {/* View toggle */}
      <div className="flex items-center gap-1 px-2 pb-1.5 mb-1 border-b border-border/50">
        <span className="text-[10px] text-muted-foreground/60 uppercase tracking-wider mr-auto">View</span>
        <button
          onClick={() => setViewMode("folder")}
          className={cn(
            "p-1 rounded transition-colors",
            viewMode === "folder"
              ? "bg-primary/10 text-primary"
              : "text-muted-foreground/50 hover:text-muted-foreground",
          )}
          title="Folder view"
        >
          <FolderTree className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => setViewMode("script-type")}
          className={cn(
            "p-1 rounded transition-colors",
            viewMode === "script-type"
              ? "bg-primary/10 text-primary"
              : "text-muted-foreground/50 hover:text-muted-foreground",
          )}
          title="Script type view"
        >
          <Layers className="h-3.5 w-3.5" />
        </button>
      </div>

      {displayNodes.map((node) => (
        <TreeNode
          key={node.id}
          node={node}
          depth={0}
          onFileSelect={onFileSelect}
          selectedFileId={selectedFileId}
        />
      ))}
    </div>
  );
}

function TreeNode({
  node,
  depth,
  onFileSelect,
  selectedFileId,
}: {
  node: FileTreeNode;
  depth: number;
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
}) {
  const [expanded, setExpanded] = useState(depth < 1);
  const isSelected = node.id === selectedFileId;

  if (node.is_directory) {
    const childCount = node.children?.length || 0;

    // Script type badge for group headers
    const isGroupHeader = node.path.startsWith("__group__/");
    const groupType = isGroupHeader ? (node.script_type as ScriptType) : null;
    const groupMeta = groupType && groupType in SCRIPT_TYPE_MAP
      ? SCRIPT_TYPE_MAP[groupType as keyof typeof SCRIPT_TYPE_MAP]
      : null;

    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex w-full items-center gap-1.5 rounded-md px-2 py-[3px] hover:bg-accent/60 transition-colors group"
          style={{ paddingLeft: `${depth * 14 + 6}px` }}
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 shrink-0 text-muted-foreground/60 transition-transform duration-150",
              expanded && "rotate-90",
            )}
          />
          {isGroupHeader && groupMeta ? (
            <span className={cn(
              "inline-flex items-center justify-center rounded px-1 py-px text-[8px] font-bold leading-none border shrink-0 min-w-[22px]",
              groupMeta.color,
            )}>
              {groupMeta.short}
            </span>
          ) : expanded ? (
            <FolderOpen className="h-3.5 w-3.5 shrink-0 text-amber-500/80" />
          ) : (
            <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500/80" />
          )}
          <span className="truncate font-medium text-foreground/80">{node.name}</span>
          <span className="ml-auto text-[9px] tabular-nums text-muted-foreground/50 opacity-0 group-hover:opacity-100 transition-opacity">
            {childCount}
          </span>
        </button>
        {expanded && node.children && (
          <div>
            {node.children.map((child) => (
              <TreeNode
                key={child.id}
                node={child}
                depth={depth + 1}
                onFileSelect={onFileSelect}
                selectedFileId={selectedFileId}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  // Parse script metadata from file path (lightweight, no content needed)
  const metadata = parseSuiteScriptMetadata(null, node.path);
  const isScript = metadata.scriptType !== "Unknown";

  return (
    <button
      onClick={() => onFileSelect(node.id, node.path)}
      className={cn(
        "flex w-full items-center gap-1.5 rounded-md px-2 py-[3px] transition-colors",
        isSelected
          ? "bg-primary/10 text-foreground font-medium ring-1 ring-primary/20"
          : "hover:bg-accent/60 text-foreground/70 hover:text-foreground",
      )}
      style={{ paddingLeft: `${depth * 14 + 22}px` }}
    >
      {isScript ? (
        <span className={cn(
          "inline-flex items-center justify-center rounded px-1 py-px text-[8px] font-bold leading-none border shrink-0 min-w-[22px]",
          metadata.color,
        )}>
          {metadata.scriptTypeShort}
        </span>
      ) : (
        <File className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
      )}
      <span className="truncate">{node.name}</span>
    </button>
  );
}
